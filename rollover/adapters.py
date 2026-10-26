"""Narrow adapter for the source-audited deployment, not a generic plugin API.

Only AstrBot 4.29.1 / MemoryCompanion 1.10.5 / PrivateCompanion 6.7.0
are accepted. Private attributes below are intentional and fail closed when
missing. No plugin database is edited directly.
"""
from __future__ import annotations

import asyncio
import json
import math
import time
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone

from .engine import Deferred
from .state import fingerprint

FIELDS = ("recent_messages", "recent_bot_replies")
LIMIT = 50000


def text_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p["text"] for p in content if isinstance(p, dict)
                         and p.get("type") in {"text", "input_text", "output_text"}
                         and isinstance(p.get("text"), str))
    return ""


def iso_time(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")


class AstrBotAdapter:
    def __init__(self, context, core_version, *, lock_manager=None, registry=None):
        self.context = context
        self.core_version = core_version
        if lock_manager is None:
            from astrbot.core.utils.session_lock import session_lock_manager
            from astrbot.core.utils.active_event_registry import active_event_registry
            lock_manager, registry = session_lock_manager, active_event_registry
        self.locks = lock_manager
        self.registry = registry

    def plugins(self):
        return {m.root_dir_name: m for m in self.context.get_all_stars() if m.activated and m.star_cls is not None}

    async def probe(self, session):
        if self.core_version != "4.29.1":
            raise Deferred("unsupported_astrbot_version")
        plugins = self.plugins()
        for root, expected in [("astrbot_plugin_memory_companion", "1.10.5"),
                               ("astrbot_plugin_private_companion", "6.7.0")]:
            if root not in plugins or str(plugins[root].version).lstrip("v") != expected:
                raise Deferred("unsupported_or_disabled_" + root)
        self.memory = plugins["astrbot_plugin_memory_companion"].star_cls.service
        self.companion = plugins["astrbot_plugin_private_companion"].star_cls
        self.manager = self.context.conversation_manager
        cfg = self.context.get_config(umo=session)
        if cfg.get("agent_runner", {}).get("runner_type", "local") != "local":
            raise Deferred("third_party_runner_unsupported")
        if cfg.get("platform_settings", {}).get("unique_session", False):
            raise Deferred("isolated_group_sessions_unsupported")
        if getattr(self.companion, "enable_multi_persona_mode", False):
            raise Deferred("multi_persona_companion_unsupported")
        for key in ("memory_capture.enabled", "conversation_memory.enabled", "conversation_memory.capture_group_messages",
                    "memory_summary.enabled"):
            if not self.memory.config.bool(key, True):
                raise Deferred("memory_capture_or_summary_disabled")
        ctx = self.memory.session_context_from_bridge({"session_id": session, "scope": "group"})
        if not self.memory._scope_feature_enabled(ctx, "capture"):
            raise Deferred("memory_scope_capture_disabled")
        if not isinstance(getattr(self.companion, "data", None), dict):
            raise Deferred("companion_data_unavailable")
        for name in ("_data_lock", "_save_data_now_sync", "_normalize_group_identity_id"):
            if not hasattr(self.companion, name):
                raise Deferred("companion_storage_contract_missing")
        for name in ("add_historical_timeline_events", "unsummarized_timeline_window", "get_timeline_by_ids", "get_summary_failure"):
            if not callable(getattr(self.memory.store, name, None)):
                raise Deferred("memory_store_contract_missing")
        if not callable(getattr(self.memory, "maybe_summarize_session", None)):
            raise Deferred("memory_summary_contract_missing")
        for name in ("get_curr_conversation_id", "get_conversation", "get_conversations", "switch_conversation"):
            if not callable(getattr(self.manager, name, None)):
                raise Deferred("native_conversation_contract_missing")
        if not callable(getattr(getattr(self.manager, "db", None), "create_conversation", None)):
            raise Deferred("native_creation_contract_missing")
        if not isinstance(getattr(self.registry, "_events", None), dict) or not isinstance(
            getattr(self.registry, "_background_tasks", None), dict
        ):
            raise Deferred("active_event_registry_unavailable")
        core = plugins.get("astrbot")
        if core is None or not hasattr(core.star_cls, "group_chat_context"):
            raise Deferred("native_group_cache_owner_unavailable")
        self.group_cache = core.star_cls.group_chat_context
        if self.group_cache is not None and not all(hasattr(self.group_cache, name)
                                                   for name in ("raw_records", "_record_ids", "_get_lock")):
            raise Deferred("native_group_cache_contract_missing")
        self.group(session)

    def ensure_idle(self, session):
        if self.registry._events.get(session) or any(not t.done() for t in self.registry._background_tasks.get(session, {})):
            raise Deferred("session_busy_retry_later")
        group = self.group(session)
        for key in ("group_episode_running_at", "group_slang_running_at"):
            running = float(group.get(key, 0) or 0)
            if not math.isfinite(running):
                raise Deferred("invalid_companion_background_timestamp")
            if running > 0 and time.time() - running < 600:
                raise Deferred("companion_background_busy_retry_later")

    @asynccontextmanager
    async def guard(self, session):
        self.ensure_idle(session)
        guard = self.locks.acquire_lock(session)
        try:
            await asyncio.wait_for(guard.__aenter__(), timeout=2)
        except TimeoutError:
            raise Deferred("session_lock_busy") from None
        try:
            self.ensure_idle(session)
            yield
        finally:
            await guard.__aexit__(None, None, None)

    async def current(self, session):
        return await self.manager.get_curr_conversation_id(session)

    def group(self, session):
        groups = self.companion.data.get("groups", {})
        if not isinstance(groups, dict):
            raise Deferred("companion_group_contract_missing")
        group_id = session.split(":")[2]
        if group_id not in groups:
            raise Deferred("companion_group_not_observed")
        if any(key != group_id and self.companion._normalize_group_identity_id(key) == group_id for key in groups):
            raise Deferred("companion_group_aliases_require_normalization")
        group = groups[group_id]
        if not isinstance(group, dict) or any(not isinstance(group.get(k, []), list) for k in FIELDS):
            raise Deferred("companion_history_contract_missing")
        return group

    def cache_snapshot(self, session):
        if self.group_cache is None:
            return {"raw": [], "ids": []}
        return {"raw": list(self.group_cache.raw_records.get(session, [])),
                "ids": list(self.group_cache._record_ids.get(session, []))}

    async def snapshot(self, session, now):
        cid = await self.current(session)
        if not cid:
            raise Deferred("no_current_conversation")
        conv = await self.manager.get_conversation(session, cid)
        if conv is None:
            raise Deferred("conversation_unavailable")
        history = json.loads(conv.history or "[]")
        if not isinstance(history, list):
            raise Deferred("invalid_history")
        async with self.companion._data_lock:
            recent = {k: deepcopy(self.group(session).get(k, [])) for k in FIELDS}
        cache = self.cache_snapshot(session)
        # No clock in identity: retries of the same content reuse the snapshot.
        identity = {"session": session, "cid": cid, "history": history, "recent": recent,
                    "native_cache": cache, "persona_id": conv.persona_id}
        return {"identity": identity, "cutoff": now, "updated_at": conv.updated_at,
                "group_name": self.group(session).get("name", "")}

    async def import_memory(self, session, snapshot, job_id):
        source = snapshot["identity"]
        rows = []
        cutoff = snapshot["cutoff"]
        for field in FIELDS:
            for item in source["recent"][field]:
                if not isinstance(item, dict):
                    raise Deferred("invalid_companion_record")
                ts = float(item.get("ts", 0))
                if not math.isfinite(ts) or ts <= 0 or ts > cutoff:
                    raise Deferred("invalid_companion_timestamp")
                text = str(item.get("text", ""))
                if not text:
                    continue
                user = str(item.get("sender_id", "")) if field == "recent_messages" else "self"
                if not user:
                    raise Deferred("unknown_companion_speaker")
                kind = "user_message" if field == "recent_messages" else "bot_response"
                mid = str(item.get("message_id") or "ar-" + fingerprint([session, field, item]))
                rows.extend(self._rows(session, kind, user, text, mid, ts, job_id,
                                       {"speaker_name": item.get("name", ""), "original_message_id": item.get("message_id", "")}))
        # Native histories may have no sender ID or timestamp. Preserve their
        # content as quoted transcripts, never invent a human speaker or claim
        # the archival timestamp is the original utterance time.
        for index, item in enumerate(source["history"]):
            if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
                continue
            text = text_content(item.get("content"))
            if not text:
                continue
            role = item["role"]
            text = f"[原生对话摘录；role={role}；原始时间、发言人未确认，不可据此归属个人事实]\n{text}"
            mid = "ar-native-" + fingerprint([source["cid"], index, item])
            rows.extend(self._rows(session, "conversation_archive", "archive_unknown", text, mid,
                                   cutoff, job_id, {"timestamp_unknown": True, "native_role": role}))
        for index, text in enumerate(source["native_cache"]["raw"]):
            if not isinstance(text, str):
                raise Deferred("native_cache_nontext_record")
            mid = "ar-cache-" + fingerprint([source["cid"], index, text])
            rows.extend(self._rows(session, "conversation_archive", "archive_unknown",
                                   "[群聊临时记录摘录；时间与身份以原文为准]\n" + text,
                                   mid, cutoff, job_id, {"timestamp_unknown": True}))
        if len(rows) > LIMIT:
            raise Deferred("archive_too_large")
        mapping = await self.memory.store.add_historical_timeline_events(rows)
        if not all(row["message_id"] in mapping for row in rows):
            raise Deferred("memory_import_incomplete")
        pending = await self.memory.store.unsummarized_timeline_window(session_id=session, scope="group", limit=LIMIT)
        if int(pending.get("total", 0)) > LIMIT:
            raise Deferred("memory_backlog_too_large")
        return [r["id"] for r in pending["rows"]]

    @staticmethod
    def _rows(session, kind, user, text, mid, ts, job_id, metadata):
        # The destination truncates content at 4000 chars. Split below that
        # boundary, and keep original message IDs for ordinary deduplication.
        chunks = [text[i:i + 3500] for i in range(0, len(text), 3500)]
        for i, chunk in enumerate(chunks):
            message_id = mid if i == 0 and len(mid) <= 120 else "ar-" + fingerprint([mid, i])
            yield {"event_type": kind, "session_id": session, "scope": "group", "subject_id": user,
                   "object_id": session.split(":")[2], "content": chunk, "message_id": message_id,
                   "occurred_at": iso_time(ts), "retention_class": "historical_archive",
                   "import_batch_id": job_id, "source_sequence": i,
                   "metadata": {"source_plugin": "astrbot_plugin_auto_reset", "group_id": session.split(":")[2],
                                "source_message_id": mid, "chunk_index": i, **metadata}}

    async def prepare_new(self, session, snapshot, job_id):
        self.ensure_idle(session)
        latest = await self.snapshot(session, snapshot["cutoff"])
        if latest["identity"] != snapshot["identity"]:
            raise Deferred("conversation_changed_retry_later")
        marker = "[auto_reset:" + job_id + "]"
        # Find an orphan prepared before a crash, before making another one.
        matches = [c for c in await self.manager.get_conversations(session) if c.title == marker]
        if len(matches) > 1:
            raise Deferred("ambiguous_prepared_conversation")
        if matches:
            if json.loads(matches[0].history or "[]") or matches[0].persona_id != snapshot["identity"]["persona_id"]:
                raise Deferred("prepared_conversation_modified")
            return matches[0].cid
        conv = await self.manager.db.create_conversation(user_id=session, platform_id=session.split(":")[0],
                                                       content=[], title=marker, persona_id=snapshot["identity"]["persona_id"])
        return conv.conversation_id

    async def finish(self, session, snapshot, new_cid):
        self.ensure_idle(session)
        current = await self.current(session)
        if current not in {snapshot["identity"]["cid"], new_cid}:
            raise Deferred("conversation_changed_externally")
        # The native lock protects LLM history. Companion and native group cache
        # have their own locks. Capture only the old prefix, retain newer arrivals.
        async with self.companion._data_lock:
            group = self.group(session)
            changes = {}
            for field in FIELDS:
                original = group.get(field, [])
                old = snapshot["identity"]["recent"][field]
                # Compare by timestamps, not prefix: bounded windows can rotate
                # while the durable archive is being imported.
                new = []
                for item in original:
                    if not isinstance(item, dict) or not isinstance(item.get("ts"), (float, int)):
                        raise Deferred("invalid_companion_record")
                    if item["ts"] > snapshot["cutoff"]:
                        new.append(item)
                    elif item not in old:
                        raise Deferred("companion_history_changed_retry_later")
                changes[field] = (original, new)
            cache = self.group_cache
            cache_guard = cache._get_lock(session) if cache is not None else asyncio.Lock()
            async with cache_guard:
                before = self.cache_snapshot(session)
                old_cache = snapshot["identity"]["native_cache"]
                # If this transient buffer changed, retry and archive the new
                # version first. Never delete an unarchived prefix.
                if before != old_cache and current != new_cid:
                    raise Deferred("native_group_cache_changed_retry_later")
                if current != new_cid:
                    latest = await self.manager.get_conversation(session, current)
                    if latest is None or json.loads(latest.history or "[]") != snapshot["identity"]["history"]:
                        raise Deferred("native_history_changed_retry_later")
                self.ensure_idle(session)
                for field, (_, new) in changes.items():
                    if field in group:
                        group[field] = new
                try:
                    # Synchronous durable save: no interleaving observers can
                    # append a record between pruning and taking its snapshot.
                    self.companion._save_data_now_sync(sections={"groups"})
                except BaseException:
                    for field, (original, _) in changes.items():
                        if field in group:
                            group[field] = original
                    raise
                if cache is not None and before == old_cache:
                    cache.raw_records.pop(session, None)
                    cache._record_ids.pop(session, None)
                # Retry this call even after a crash: it repairs the persistent
                # selector if a previous switch only changed the memory cache.
                await self.manager.switch_conversation(session, new_cid)

    async def summarize(self, session, pending_ids):
        rows = await self.memory.store.get_timeline_by_ids(list(pending_ids))
        if any(i not in rows for i in pending_ids):
            raise Deferred("pending_timeline_missing")
        remaining = {i for i in pending_ids if not rows[i].get("summarized_at")}
        if not remaining:
            return set()
        failure = await self.memory.store.get_summary_failure(session)
        if failure:
            metadata = failure.get("metadata", {})
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            if metadata.get("state") in {"evidence_quarantine", "dead_letter"}:
                raise Deferred("memory_evidence_review_required")
        ctx = self.memory.session_context_from_bridge({"session_id": session, "scope": "group"})
        await asyncio.wait_for(self.memory.maybe_summarize_session(ctx, force=True), timeout=240)
        after = await self.memory.store.get_timeline_by_ids(list(remaining))
        if any(i not in after for i in remaining):
            raise Deferred("pending_timeline_missing")
        return {i for i in remaining if not after[i].get("summarized_at")}
