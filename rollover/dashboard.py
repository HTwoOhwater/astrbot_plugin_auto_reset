"""Dashboard business logic, kept independent of the HTTP framework."""
from __future__ import annotations

from copy import deepcopy
from datetime import timedelta

from .engine import BEIJING, Deferred, Settings
from .state import fingerprint

PLUGIN_NAME = "astrbot_plugin_auto_reset"
VERSION = "2.1.0"
CONFIG_KEYS = ("enabled", "sessions", "hour", "minute")


class PanelError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class Dashboard:
    def __init__(self, plugin):
        self.plugin = plugin

    def settings_payload(self):
        cfg = self.plugin.config
        value = {"enabled": cfg.get("enabled", False), "sessions": deepcopy(cfg.get("sessions", [])),
                 "hour": cfg.get("hour", 4), "minute": cfg.get("minute", 0)}
        return value, fingerprint(value)

    async def status(self):
        config, revision = self.settings_payload()
        try:
            settings = self.plugin.settings()
        except (TypeError, ValueError):
            raise PanelError("当前插件配置无效，请先在插件卡片的配置中修正。") from None
        coordinator = self.plugin.coordinator
        now = coordinator.clock().astimezone(BEIJING)
        next_run = now.replace(hour=settings.hour, minute=settings.minute, second=0, microsecond=0)
        if now >= next_run:
            next_run += timedelta(days=1)
        checks = []
        for session in settings.sessions:
            try:
                await coordinator.adapter.probe(session)
                checks.append({"session": session, "ok": True, "reason": ""})
            except Exception as exc:
                checks.append({"session": session, "ok": False,
                               "reason": str(exc) if isinstance(exc, Deferred) else type(exc).__name__})
        dependencies = [{"name": "AstrBot", "expected": "4.29.1", "version": coordinator.adapter.core_version,
                         "active": True}]
        stars = {m.root_dir_name: m for m in self.plugin.context.get_all_stars()}
        for root, label, expected in (("astrbot_plugin_private_companion", "陪伴插件", "6.7.0"),
                                      ("astrbot_plugin_memory_companion", "记忆插件", "1.10.5")):
            meta = stars.get(root)
            dependencies.append({"name": label, "expected": expected,
                                 "version": str(meta.version) if meta else "未安装",
                                 "active": bool(meta and meta.activated and meta.star_cls is not None)})
        # Explicit allowlist of public fields: never return archive contents,
        # provider exceptions, raw chat buffers, paths or pending timeline IDs.
        jobs = []
        for job in coordinator.journal.jobs.values():
            if job["session"] not in settings.sessions or job["phase"] == "baseline":
                continue
            jobs.append({k: job.get(k, "") for k in
                         ("session", "day", "completed_day", "phase", "summary_state", "error", "summary_error")}
                        | {"pending_count": len(job.get("pending_ids", []))})
        jobs.sort(key=lambda j: (j["day"], j["session"]), reverse=True)
        day = settings.day(now)
        stats = {"today_done": len({j["session"] for j in jobs if j["phase"] == "done"
                                   and (j["completed_day"] or j["day"]) == day}),
                 "pending_jobs": sum(j["pending_count"] > 0 for j in jobs),
                 "paused_jobs": sum(j["pending_count"] > 0 and j["summary_state"] == "paused" for j in jobs)}
        return {"version": VERSION, "config": config, "revision": revision,
                "now": now.isoformat(timespec="seconds"), "next_run": next_run.isoformat(timespec="seconds"),
                "day": day, "busy": coordinator.lock.locked(), "stats": stats,
                "dependencies": dependencies, "checks": checks, "jobs": jobs[:50]}

    async def save(self, payload):
        if not isinstance(payload, dict) or set(payload) != {"revision", "config"}:
            raise PanelError("提交格式无效，请刷新页面后重试。")
        candidate = payload["config"]
        if not isinstance(candidate, dict) or set(candidate) != set(CONFIG_KEYS):
            raise PanelError("请填写开关、群范围和执行时间。")
        if type(candidate["enabled"]) is not bool or type(candidate["hour"]) is not int or type(candidate["minute"]) is not int:
            raise PanelError("开关必须是开或关，执行时间必须是整数。")
        sessions = candidate["sessions"]
        if not isinstance(sessions, list) or len(sessions) > 100 or any(not isinstance(s, str) or len(s) > 200 for s in sessions):
            raise PanelError("群范围最多填写 100 个完整会话 ID。")
        try:
            settings = Settings.from_config(candidate)
        except (TypeError, ValueError):
            raise PanelError("群会话 ID 或时间无效。请使用 /sid 返回的完整共享群 ID，时间范围为 00:00–23:59。") from None
        if settings.enabled and not settings.sessions:
            raise PanelError("请先填写生效群，再开启每日换新。")
        coordinator = self.plugin.coordinator
        if coordinator.lock.locked():
            raise PanelError("任务正在处理，请稍后保存。", 409)
        async with coordinator.lock:
            _, revision = self.settings_payload()
            if payload["revision"] != revision:
                raise PanelError("设置已在其他页面修改，请刷新后再保存。", 409)
            old = deepcopy(dict(self.plugin.config))
            values = {"enabled": settings.enabled, "sessions": list(settings.sessions),
                      "hour": settings.hour, "minute": settings.minute}
            try:
                # AstrBot's synchronous save performs its own atomic replacement;
                # no await between changing and saving the in-memory config.
                self.plugin.config.save_config(values)
            except Exception:
                self.plugin.config.clear()
                self.plugin.config.update(old)
                raise PanelError("设置保存失败，当前设置已恢复，请检查磁盘和后台日志。", 500) from None
        return {"message": "设置已保存，下次巡检使用新设置。", "config": values,
                "revision": fingerprint(values)}

    async def action(self, payload):
        if not isinstance(payload, dict) or set(payload) != {"action", "session", "revision", "confirmed"}:
            raise PanelError("操作参数无效，请刷新后重试。")
        if payload["confirmed"] is not True:
            raise PanelError("请先确认所选群和操作。")
        _, revision = self.settings_payload()
        if payload["revision"] != revision:
            raise PanelError("设置已改变，请刷新并重新确认目标群。", 409)
        settings = self.plugin.settings()
        session = payload["session"]
        if not isinstance(session, str) or session not in settings.sessions:
            raise PanelError("只能操作已经保存的生效群。")
        if not settings.enabled:
            raise PanelError("请先开启并保存每日换新设置。")
        coordinator = self.plugin.coordinator
        if coordinator.lock.locked():
            raise PanelError("任务正在处理，请稍后再试。", 409)
        if payload["action"] == "now":
            day = settings.day(coordinator.clock())
            existing = coordinator.journal.jobs.get(coordinator.key(session, day))
            if existing:
                if existing["phase"] == "done":
                    return {"message": "这个群今天已经换新，不会重复执行。"}
                if existing["phase"] == "superseded":
                    return {"message": "当天任务已结束或被其他对话选择替代，不会覆盖当前选择。"}
            coordinator.request_run(settings, session)
            message = "已排队，会话空闲后执行；正在进行的回复会延后处理。"
        elif payload["action"] == "retry":
            coordinator.retry_summaries(settings, session)
            message = "未完成的摘要已重新排队，不会重复换新。"
        else:
            raise PanelError("不支持这项操作。")
        return {"message": message}
