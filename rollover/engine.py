from __future__ import annotations

import asyncio
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .state import fingerprint

# Beijing schedule without a Windows tzdata dependency; no host timezone use.
BEIJING = timezone(timedelta(hours=8), "Asia/Shanghai")


class Deferred(RuntimeError):
    """A known condition that must preserve the current conversation."""


@dataclass(frozen=True)
class Settings:
    enabled: bool = False
    sessions: tuple[str, ...] = ()
    hour: int = 4
    minute: int = 0

    @classmethod
    def from_config(cls, config):
        raw = config.get("sessions", [])
        if not isinstance(raw, list):
            raise ValueError("sessions_must_be_list")
        sessions = tuple(dict.fromkeys(str(s).strip() for s in raw if str(s).strip()))
        for s in sessions:
            parts = s.split(":")
            if len(parts) != 3 or not parts[0] or parts[1] != "GroupMessage" or not parts[2].isdigit():
                raise ValueError("use_full_shared_group_umo")
        if len({s.split(":")[2] for s in sessions}) != len(sessions):
            raise ValueError("ambiguous_group_scope")
        hour, minute = int(config.get("hour", 4)), int(config.get("minute", 0))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("invalid_time")
        return cls(bool(config.get("enabled", False)), sessions, hour, minute)

    def day(self, now):
        return (now.astimezone(BEIJING) - timedelta(hours=self.hour, minutes=self.minute)).date().isoformat()


class Coordinator:
    def __init__(self, journal, adapter, *, clock=lambda: datetime.now(BEIJING)):
        self.journal = journal
        self.adapter = adapter
        self.clock = clock
        self.lock = asyncio.Lock()

    @staticmethod
    def key(session, day):
        return fingerprint([session, day])

    def request_run(self, settings, session):
        if session not in settings.sessions:
            raise ValueError("session_not_in_allowlist")
        day = settings.day(self.clock())
        key = self.key(session, day)
        if key not in self.journal.jobs:
            self.journal.put(key, {"session": session, "day": day, "phase": "waiting"})

    def retry_summaries(self, settings, session):
        if self.lock.locked():
            raise Deferred("coordinator_busy_retry_later")
        if session not in settings.sessions:
            raise ValueError("session_not_in_allowlist")
        for key, old in list(self.journal.jobs.items()):
            if old["session"] == session and old.get("pending_ids"):
                job = deepcopy(old)
                job.update(summary_attempts=0, summary_next=0, summary_state="pending")
                self.journal.put(key, job)

    async def preview(self, settings):
        checks = []
        for session in settings.sessions:
            try:
                await self.adapter.probe(session)
                checks.append(f"{session}：依赖检查通过")
            except Exception as exc:
                checks.append(f"{session}：{str(exc) if isinstance(exc, Deferred) else type(exc).__name__}")
        pending = [j for j in self.journal.jobs.values() if j["session"] in settings.sessions]
        local = self.clock().astimezone(BEIJING)
        next_run = local.replace(hour=settings.hour, minute=settings.minute, second=0, microsecond=0)
        if local >= next_run:
            next_run += timedelta(days=1)
        states = [f"{j['day']} {j['session']}：{j['phase']}；摘要 {j.get('summary_state', '未排队')}"
                  + (f"；{j['error']}" if j.get("error") else "")
                  + (f"；摘要原因 {j['summary_error']}" if j.get("summary_error") else "") for j in pending[-10:]]
        return "\n".join([
            f"每日换新：{'开启' if settings.enabled else '关闭'}；北京时间 {settings.hour:02d}:{settings.minute:02d}",
            f"下一日界：{next_run:%Y-%m-%d %H:%M}；未完成任务每分钟检查",
            f"指定群：{len(settings.sessions)}；旧对话与原始归档保留",
            *checks, *states,
        ])

    async def tick(self, settings):
        if not settings.enabled or not settings.sessions or self.lock.locked():
            return
        async with self.lock:
            day = settings.day(self.clock())
            for session in settings.sessions:
                baseline = self.key(session, "baseline")
                if baseline not in self.journal.jobs:
                    # First enable arms the next boundary; explicit now can queue.
                    self.journal.put(baseline, {"session": session, "day": day, "phase": "baseline"})
                elif self.journal.jobs[baseline]["day"] != day:
                    key = self.key(session, day)
                    unfinished = any(j["session"] == session and j["phase"] not in {"baseline", "done", "superseded"}
                                     for j in self.journal.jobs.values())
                    if key not in self.journal.jobs and not unfinished:
                        self.journal.put(key, {"session": session, "day": day, "phase": "waiting"})
            for key, old in list(self.journal.jobs.items()):
                if old["session"] not in settings.sessions or old["phase"] in {"baseline", "done", "superseded"}:
                    continue
                later_done = any(other != key and j["session"] == old["session"] and j["phase"] == "done"
                                 and (j["day"] > old["day"] or j.get("completed_day", j["day"]) == day)
                                 for other, j in self.journal.jobs.items())
                if later_done:
                    job = deepcopy(old)
                    job["phase"] = "superseded"
                    self.journal.put(key, job)
                    continue
                try:
                    await self._rollover(key, day)
                    if self.journal.jobs[key]["phase"] == "done":
                        self.journal.put(self.key(old["session"], "baseline"),
                                         {"session": old["session"], "day": day, "phase": "baseline"})
                except Exception as exc:
                    job = deepcopy(self.journal.jobs[key])
                    job["error"] = str(exc) if isinstance(exc, Deferred) else type(exc).__name__
                    self.journal.put(key, job)
            for key, old in list(self.journal.jobs.items()):
                if old["session"] in settings.sessions and old.get("pending_ids"):
                    await self._summarize(key)

    async def _rollover(self, key, day):
        job = deepcopy(self.journal.jobs[key])
        session = job["session"]
        await self.adapter.probe(session)
        async with self.adapter.guard(session):
            current = await self.adapter.current(session)
            if job.get("new_cid") and current == job["new_cid"]:
                snapshot = self.journal.read_archive(job["archive"])
                await self.adapter.finish(session, snapshot, job["new_cid"])
                job.update(phase="done", completed_day=day, error="")
                self.journal.put(key, job)
                return
            if job.get("old_cid") and current != job["old_cid"]:
                job.update(phase="superseded", error="conversation_changed_externally")
                self.journal.put(key, job)
                return
            snapshot = await self.adapter.snapshot(session, now=self.clock().timestamp())
            digest = fingerprint(snapshot["identity"])
            if job.get("snapshot_digest") != digest:
                archive = self.journal.archive(snapshot)
                job.update(old_cid=current, snapshot_digest=digest, archive=archive, phase="archived")
                self.journal.put(key, job)
            else:
                snapshot = self.journal.read_archive(job["archive"])
            ids = await self.adapter.import_memory(session, snapshot, key)
            job["pending_ids"] = sorted(set(job.get("pending_ids", [])) | set(ids))
            job.update(phase="imported", summary_state="pending" if job["pending_ids"] else "complete")
            self.journal.put(key, job)
            new_cid = await self.adapter.prepare_new(session, snapshot, key)
            job.update(new_cid=new_cid, phase="prepared")
            self.journal.put(key, job)
            await self.adapter.finish(session, snapshot, new_cid)
            job.update(phase="done", completed_day=day, error="")
            self.journal.put(key, job)

    async def _summarize(self, key):
        job = deepcopy(self.journal.jobs[key])
        if job.get("summary_attempts", 0) >= 3 or time.time() < job.get("summary_next", 0):
            return
        try:
            await self.adapter.probe(job["session"])
            before = set(job["pending_ids"])
            remaining = await self.adapter.summarize(job["session"], before)
            job["pending_ids"] = sorted(remaining)
            progressed = set(remaining) != before
            attempts = 0 if progressed else job.get("summary_attempts", 0) + 1
            job.update(summary_attempts=attempts, summary_next=time.time() + (60 if progressed else 300 * 2 ** attempts),
                       summary_state="complete" if not remaining else "paused" if attempts >= 3 else "pending",
                       summary_error="" if progressed or not remaining else "summary_no_progress")
        except Exception as exc:
            attempts = job.get("summary_attempts", 0) + 1
            job.update(summary_attempts=attempts, summary_next=time.time() + 300 * 2 ** attempts,
                       summary_state="paused" if attempts >= 3 else "pending",
                       summary_error=str(exc) if isinstance(exc, Deferred) else type(exc).__name__)
        self.journal.put(key, job)
