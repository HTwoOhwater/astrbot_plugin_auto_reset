from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from rollover.adapters import AstrBotAdapter
from rollover.engine import BEIJING, Coordinator, Deferred, Settings
from rollover.state import Journal, fingerprint

SESSION = "qq:GroupMessage:123456"
OTHER = "qq:GroupMessage:654321"
NOW = datetime(2026, 10, 25, 3, 59, tzinfo=BEIJING)


class Locks:
    def __init__(self):
        self.locks = defaultdict(asyncio.Lock)

    @asynccontextmanager
    async def acquire_lock(self, session):
        async with self.locks[session]:
            yield


class Store:
    def __init__(self):
        self.rows = {}
        self.dedupe = {}
        self.failure = None
        self.import_fail = False
        self.after_import = None

    async def add_historical_timeline_events(self, rows):
        if self.import_fail:
            raise OSError("private provider payload must never appear in status")
        result = {}
        for raw in rows:
            key = (raw["event_type"], raw["session_id"], raw["subject_id"], raw["message_id"])
            if key not in self.dedupe:
                rid = "tl-" + fingerprint(key)
                self.dedupe[key] = rid
                self.rows[rid] = {**deepcopy(raw), "id": rid, "summarized_at": ""}
            result[raw["message_id"]] = self.dedupe[key]
        if self.after_import:
            callback, self.after_import = self.after_import, None
            callback()
        return result

    async def unsummarized_timeline_window(self, *, session_id, scope, limit):
        rows = [r for r in self.rows.values() if r["session_id"] == session_id and not r["summarized_at"]]
        return {"total": len(rows), "rows": deepcopy(rows[:limit])}

    async def get_timeline_by_ids(self, ids):
        return {i: deepcopy(self.rows[i]) for i in ids if i in self.rows}

    async def get_summary_failure(self, session):
        return self.failure


class Companion:
    enable_multi_persona_mode = False

    @staticmethod
    def _normalize_group_identity_id(value):
        return str(value).split(":")[-1]

    def __init__(self):
        self._data_lock = asyncio.Lock()
        self.fail_save = False
        self.data = {"groups": {"123456": {
            "name": "test", "relationship": "keep", "recent_messages": [
                {"ts": NOW.timestamp() - 100, "text": "remember my hobby", "sender_id": "alice", "message_id": "111"}],
            "recent_bot_replies": [{"ts": NOW.timestamp() - 99, "text": "okay", "message_id": "112"}],
        }, "654321": {"recent_messages": [{"ts": 1, "text": "untouched"}], "recent_bot_replies": []}}}

    def _save_data_now_sync(self, *, sections):
        assert sections == {"groups"}
        if self.fail_save:
            raise OSError("save_failed")


class Manager:
    def __init__(self):
        self.session_conversations = {SESSION: "old", OTHER: "other"}
        self.convs = {
            "old": NS(cid="old", conversation_id="old", user_id=SESSION, persona_id="persona-a", title="old title",
                      history=json.dumps([{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]),
                      updated_at=NOW.timestamp() - 90),
            "other": NS(cid="other", user_id=OTHER, history="[]", persona_id=None, title="other", updated_at=0),
        }
        self.created = 0
        self.db = self
        self.crash_after_switch = False

    async def get_curr_conversation_id(self, session):
        return self.session_conversations.get(session)

    async def get_conversation(self, session, cid):
        return self.convs.get(cid)

    async def get_conversations(self, session):
        return [c for c in self.convs.values() if c.user_id == session]

    async def create_conversation(self, *, user_id, platform_id, content, title, persona_id):
        self.created += 1
        cid = f"new-{self.created}"
        conv = NS(cid=cid, conversation_id=cid, user_id=user_id, persona_id=persona_id, title=title,
                  history=json.dumps(content), updated_at=NOW.timestamp())
        self.convs[cid] = conv
        return conv

    async def switch_conversation(self, session, cid):
        self.session_conversations[session] = cid
        if self.crash_after_switch:
            self.crash_after_switch = False
            raise asyncio.CancelledError()


class Context:
    def __init__(self):
        self.conversation_manager = Manager()
        self.store = Store()
        self.companion = Companion()
        self.summary_calls = 0
        self.summary_fail = False
        self.cfg = {"agent_runner": {"runner_type": "local"}, "platform_settings": {"unique_session": False}}
        self.memory_flags = {}
        self.memory = NS(store=self.store, config=NS(bool=lambda key, default: self.memory_flags.get(key, default)),
                         session_context_from_bridge=lambda payload: NS(**payload),
                         _scope_feature_enabled=lambda ctx, feature: True,
                         maybe_summarize_session=self.summarize)
        cache = NS(raw_records=defaultdict(deque), _record_ids=defaultdict(deque), _get_lock=lambda session: asyncio.Lock())
        cache.raw_records[SESSION].append("group message")
        cache._record_ids[SESSION].append("id-1")
        self.stars = [NS(root_dir_name="astrbot_plugin_memory_companion", version="1.10.5", activated=True, star_cls=NS(service=self.memory)),
                      NS(root_dir_name="astrbot_plugin_private_companion", version="6.7.0", activated=True, star_cls=self.companion),
                      NS(root_dir_name="astrbot", activated=True, star_cls=NS(group_chat_context=cache))]

    def get_all_stars(self):
        return self.stars

    def get_config(self, *, umo):
        return self.cfg

    async def summarize(self, ctx, *, force):
        self.summary_calls += 1
        if self.summary_fail:
            raise RuntimeError("summary_failed")
        rows = [r for r in self.store.rows.values() if r["session_id"] == ctx.session_id and not r["summarized_at"]]
        for row in rows[:40]:
            row["summarized_at"] = "done"
        return "generated-id-is-not-a-completion-proof"


class RollOverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ctx = Context()
        self.registry = NS(_events={}, _background_tasks={})
        self.adapter = AstrBotAdapter(self.ctx, "4.29.1", lock_manager=Locks(), registry=self.registry)
        self.now = NOW
        self.journal = Journal(self.temp.name)
        self.coordinator = Coordinator(self.journal, self.adapter, clock=lambda: self.now)
        self.settings = Settings.from_config({"enabled": True, "sessions": [SESSION]})

    def job(self):
        return self.journal.jobs[self.coordinator.key(SESSION, self.settings.day(self.now))]

    async def run_now(self):
        self.coordinator.request_run(self.settings, SESSION)
        await self.coordinator.tick(self.settings)

    def restart(self):
        self.journal = Journal(self.temp.name)
        self.coordinator = Coordinator(self.journal, self.adapter, clock=lambda: self.now)

    async def test_first_enable_arms_next_beijing_boundary(self):
        await self.coordinator.tick(self.settings)
        self.assertEqual(self.ctx.conversation_manager.created, 0)
        self.now += timedelta(minutes=1)
        await self.coordinator.tick(self.settings)
        self.assertEqual(self.ctx.conversation_manager.created, 1)
        self.assertEqual(self.job()["phase"], "done")
        await self.coordinator.tick(self.settings)
        self.assertEqual(self.ctx.conversation_manager.created, 1)

    async def test_allowlist_persona_and_raw_history_preserved(self):
        before_other = deepcopy(self.ctx.companion.data["groups"]["654321"])
        await self.run_now()
        manager = self.ctx.conversation_manager
        self.assertEqual(manager.session_conversations[OTHER], "other")
        self.assertEqual(manager.convs["new-1"].persona_id, "persona-a")
        self.assertEqual(json.loads(manager.convs["old"].history)[0]["content"], "hello")
        self.assertEqual(self.ctx.companion.data["groups"]["654321"], before_other)
        self.assertEqual(self.ctx.companion.data["groups"]["123456"]["relationship"], "keep")
        self.assertEqual(self.ctx.companion.data["groups"]["123456"]["recent_messages"], [])
        snapshot = self.journal.read_archive(self.job()["archive"])
        self.assertEqual(snapshot["identity"]["history"][0]["content"], "hello")

    async def test_import_failure_keeps_context_and_status_has_no_payload(self):
        self.ctx.store.import_fail = True
        await self.run_now()
        self.assertEqual(self.ctx.conversation_manager.created, 0)
        self.assertEqual(self.ctx.conversation_manager.session_conversations[SESSION], "old")
        self.assertTrue(self.ctx.companion.data["groups"]["123456"]["recent_messages"])
        self.assertEqual(self.job()["error"], "OSError")
        self.ctx.store.import_fail = False
        await self.coordinator.tick(self.settings)
        self.assertEqual(self.job()["phase"], "done")

    async def test_summary_failure_retains_raw_and_persistent_retry(self):
        self.ctx.summary_fail = True
        await self.run_now()
        self.assertEqual(self.job()["phase"], "done")
        self.assertTrue(self.job()["pending_ids"])
        self.restart()
        self.ctx.summary_fail = False
        self.coordinator.retry_summaries(self.settings, SESSION)
        await self.coordinator.tick(self.settings)
        self.assertEqual(self.job()["summary_state"], "complete")
        self.assertEqual(self.ctx.conversation_manager.created, 1)

    async def test_busy_foreground_and_background_are_deferred(self):
        event = object()
        self.registry._events[SESSION] = {event}
        await self.run_now()
        self.assertEqual(self.ctx.conversation_manager.created, 0)
        self.registry._events.clear()
        task = asyncio.create_task(asyncio.Event().wait())
        try:
            self.registry._background_tasks[SESSION] = {task: event}
            await self.coordinator.tick(self.settings)
            self.assertEqual(self.ctx.conversation_manager.created, 0)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.registry._background_tasks.clear()
        await self.coordinator.tick(self.settings)
        self.assertEqual(self.ctx.conversation_manager.created, 1)

    async def test_new_native_message_during_import_not_overwritten(self):
        def incoming():
            conv = self.ctx.conversation_manager.convs["old"]
            conv.history = json.dumps(json.loads(conv.history) + [{"role": "user", "content": "new arrival"}])
        self.ctx.store.after_import = incoming
        await self.run_now()
        self.assertEqual(self.ctx.conversation_manager.created, 0)
        self.assertIn("new arrival", self.ctx.conversation_manager.convs["old"].history)
        await self.coordinator.tick(self.settings)
        self.assertEqual(self.job()["phase"], "done")
        self.assertTrue(any("new arrival" in r["content"] for r in self.ctx.store.rows.values()))

    async def test_companion_save_failure_restores_memory_and_retries(self):
        self.ctx.companion.fail_save = True
        await self.run_now()
        self.assertEqual(self.ctx.conversation_manager.session_conversations[SESSION], "old")
        self.assertTrue(self.ctx.companion.data["groups"]["123456"]["recent_messages"])
        self.ctx.companion.fail_save = False
        self.restart()
        await self.coordinator.tick(self.settings)
        self.assertEqual(self.ctx.conversation_manager.created, 1)
        self.assertEqual(self.job()["phase"], "done")

    async def test_crash_after_creation_before_journal_discovers_orphan(self):
        original = self.journal.put
        def fail_prepared(key, job):
            if job.get("phase") == "prepared":
                raise asyncio.CancelledError()
            return original(key, job)
        with patch.object(self.journal, "put", fail_prepared):
            with self.assertRaises(asyncio.CancelledError):
                await self.run_now()
        self.restart()
        await self.coordinator.tick(self.settings)
        self.assertEqual(self.ctx.conversation_manager.created, 1)
        self.assertEqual(self.job()["phase"], "done")

    async def test_crash_after_switch_recovers_without_duplicate(self):
        self.ctx.conversation_manager.crash_after_switch = True
        with self.assertRaises(asyncio.CancelledError):
            await self.run_now()
        self.restart()
        await self.coordinator.tick(self.settings)
        self.assertEqual(self.ctx.conversation_manager.created, 1)
        self.assertEqual(self.job()["phase"], "done")

    async def test_native_manual_switch_is_respected(self):
        self.ctx.store.import_fail = True
        await self.run_now()
        self.ctx.conversation_manager.session_conversations[SESSION] = "other"
        self.ctx.store.import_fail = False
        await self.coordinator.tick(self.settings)
        self.assertEqual(self.job()["phase"], "superseded")
        self.assertEqual(self.ctx.conversation_manager.created, 0)

    async def test_long_content_is_chunked_and_retries_are_deduplicated(self):
        self.ctx.conversation_manager.convs["old"].history = json.dumps([{"role": "user", "content": "x" * 9000}])
        await self.adapter.probe(SESSION)
        snapshot = await self.adapter.snapshot(SESSION, self.now.timestamp())
        await self.adapter.import_memory(SESSION, snapshot, "job")
        before = len(self.ctx.store.rows)
        await self.adapter.import_memory(SESSION, snapshot, "job")
        self.assertEqual(len(self.ctx.store.rows), before)
        self.assertTrue(all(len(row["content"]) <= 3500 for row in self.ctx.store.rows.values()))
        self.assertEqual(sum(r["content"].count("x") for r in self.ctx.store.rows.values()), 9000)

    async def test_more_than_one_summary_batch_remains_pending(self):
        for i in range(81):
            self.ctx.store.rows[f"extra-{i}"] = {"id": f"extra-{i}", "session_id": SESSION, "summarized_at": ""}
        await self.run_now()
        self.assertTrue(self.job()["pending_ids"])
        self.assertEqual(self.ctx.summary_calls, 1)
        for _ in range(3):
            self.coordinator.retry_summaries(self.settings, SESSION)
            await self.coordinator.tick(self.settings)
        self.assertEqual(self.job()["summary_state"], "complete")

    async def test_summary_quarantine_does_not_force_evidence_gate(self):
        self.ctx.store.failure = {"metadata": {"state": "evidence_quarantine"}}
        await self.run_now()
        self.assertTrue(self.job()["pending_ids"])
        self.assertEqual(self.ctx.summary_calls, 0)

    async def test_unsupported_or_disabled_dependencies_block(self):
        self.ctx.stars[0].version = "2.3.1"
        await self.run_now()
        self.assertEqual(self.ctx.conversation_manager.created, 0)
        self.ctx.stars[0].version = "1.10.5"
        self.ctx.memory_flags["memory_capture.enabled"] = False
        await self.coordinator.tick(self.settings)
        self.assertEqual(self.ctx.conversation_manager.created, 0)

    async def test_empty_scope_and_disabled_do_nothing(self):
        for settings in [Settings(), Settings(True, ()), Settings(False, (SESSION,))]:
            await self.coordinator.tick(settings)
        self.assertFalse(self.journal.jobs)

    async def test_companion_background_summary_defers_rollover(self):
        import time
        group = self.ctx.companion.data["groups"]["123456"]
        group["group_episode_running_at"] = time.time()
        await self.run_now()
        self.assertEqual(self.ctx.conversation_manager.created, 0)
        group["group_episode_running_at"] = 0
        await self.coordinator.tick(self.settings)
        self.assertEqual(self.job()["phase"], "done")

    async def test_unknown_companion_layout_and_native_cache_owner_block(self):
        groups = self.ctx.companion.data["groups"]
        groups[SESSION] = deepcopy(groups["123456"])
        await self.run_now()
        self.assertEqual(self.ctx.conversation_manager.created, 0)
        del groups[SESSION]
        self.ctx.stars.pop()
        await self.coordinator.tick(self.settings)
        self.assertEqual(self.ctx.conversation_manager.created, 0)

    async def test_isolated_sessions_multi_persona_and_third_party_runner_block(self):
        for kind in ("isolated", "multi_persona", "third_party"):
            with self.subTest(kind=kind):
                self.ctx.cfg["platform_settings"]["unique_session"] = kind == "isolated"
                self.ctx.cfg["agent_runner"]["runner_type"] = "dify" if kind == "third_party" else "local"
                self.ctx.companion.enable_multi_persona_mode = kind == "multi_persona"
                await self.run_now()
                self.assertEqual(self.ctx.conversation_manager.created, 0)

    async def test_new_companion_arrival_at_finish_is_preserved(self):
        original = self.adapter.prepare_new
        arrival = {"ts": self.now.timestamp() + 1, "text": "new arrival", "sender_id": "alice", "message_id": "113"}
        async def prepare(*args):
            cid = await original(*args)
            self.ctx.companion.data["groups"]["123456"]["recent_messages"].append(arrival)
            return cid
        with patch.object(self.adapter, "prepare_new", prepare):
            await self.run_now()
        self.assertEqual(self.job()["phase"], "done")
        self.assertEqual(self.ctx.companion.data["groups"]["123456"]["recent_messages"], [arrival])

    async def test_summary_pause_and_admin_retry(self):
        self.ctx.summary_fail = True
        await self.run_now()
        for _ in range(2):
            with patch("rollover.engine.time.time", return_value=self.job()["summary_next"] + 1):
                await self.coordinator.tick(self.settings)
        self.assertEqual(self.job()["summary_state"], "paused")
        calls = self.ctx.summary_calls
        with patch("rollover.engine.time.time", return_value=self.job()["summary_next"] + 99999):
            await self.coordinator.tick(self.settings)
        self.assertEqual(self.ctx.summary_calls, calls)
        self.ctx.summary_fail = False
        self.coordinator.retry_summaries(self.settings, SESSION)
        await self.coordinator.tick(self.settings)
        self.assertEqual(self.job()["summary_state"], "complete")
        self.assertEqual(self.job()["summary_error"], "")

    async def test_stopped_for_several_days_only_one_rollover(self):
        await self.coordinator.tick(self.settings)
        self.now += timedelta(days=4, minutes=5)
        self.restart()
        await self.coordinator.tick(self.settings)
        await self.coordinator.tick(self.settings)
        self.assertEqual(self.ctx.conversation_manager.created, 1)

    async def test_overdue_job_and_manual_queue_only_roll_once_today(self):
        self.ctx.store.import_fail = True
        await self.run_now()
        self.now += timedelta(days=2)
        self.coordinator.request_run(self.settings, SESSION)
        self.ctx.store.import_fail = False
        await self.coordinator.tick(self.settings)
        self.assertEqual(self.ctx.conversation_manager.created, 1)
        self.assertEqual(self.job()["phase"], "superseded")
        await self.coordinator.tick(self.settings)
        self.assertEqual(self.ctx.conversation_manager.created, 1)


class StateTests(unittest.TestCase):
    def test_scope_validation(self):
        for s in ["123456", "qq:FriendMessage:123456", "qq:GroupMessage:123456:alice"]:
            with self.assertRaises(ValueError):
                Settings.from_config({"sessions": [s]})
        with self.assertRaises(ValueError):
            Settings.from_config({"sessions": [SESSION, "another:GroupMessage:123456"]})

    def test_schedule_uses_beijing_even_for_utc_clock(self):
        from datetime import timezone
        settings = Settings()
        utc = datetime(2026, 10, 24, 20, 0, tzinfo=timezone.utc)
        self.assertEqual(settings.day(utc), "2026-10-25")

    def test_archive_integrity_and_corrupted_journal(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = Journal(temp)
            name = journal.archive({"hello": "world"})
            (Path(temp) / name).write_text("{}", encoding="utf-8")
            with self.assertRaises(ValueError):
                journal.read_archive(name)
            journal.path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(ValueError):
                Journal(temp)
            journal.path.write_text(json.dumps({"job": {"session": SESSION, "day": "2026-10-25", "phase": "unknown"}}), encoding="utf-8")
            with self.assertRaises(ValueError):
                Journal(temp)

    def test_failed_atomic_write_does_not_advance_memory_ledger(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = Journal(temp)
            with patch.object(journal, "atomic_write", side_effect=OSError()):
                with self.assertRaises(OSError):
                    journal.put("x", {"day": "today", "session": SESSION})
            self.assertEqual(journal.jobs, {})


if __name__ == "__main__":
    unittest.main()
