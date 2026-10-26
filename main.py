"""Daily conversation rollover; upstream: Song-yunru/astrbot_plugin_auto_reset."""
from __future__ import annotations

import asyncio
from importlib.metadata import version

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import PermissionType, permission_type
from astrbot.api.star import Context, Star, StarTools, register

from .rollover.adapters import AstrBotAdapter
from .rollover.engine import Coordinator, Settings
from .rollover.state import Journal


@register("auto_reset", "HTwoOhwater", "每日归档后换新对话，联动现有陪伴与记忆插件", "2.0.0")
class AutoResetPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.coordinator = Coordinator(
            Journal(StarTools.get_data_dir("astrbot_plugin_auto_reset")),
            AstrBotAdapter(context, version("astrbot")),
        )
        self._worker = None

    def settings(self) -> Settings:
        return Settings.from_config(self.config)

    async def initialize(self):
        self._worker = asyncio.create_task(self._run(), name="auto_reset_rollover")

    async def terminate(self):
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass

    async def _run(self):
        while True:
            try:
                await self.coordinator.tick(self.settings())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Provider exceptions can include credentials or transcript text.
                logger.error("[AutoReset] 巡检失败: %s", type(exc).__name__)
            await asyncio.sleep(60)

    @filter.command("auto_reset")
    @permission_type(PermissionType.ADMIN)
    async def auto_reset_cmd(self, event: AstrMessageEvent):
        parts = event.message_str.strip().split()
        action = parts[1] if len(parts) > 1 else "status"
        try:
            settings = self.settings()
            if action in {"status", "preview"}:
                result = await self.coordinator.preview(settings)
            elif action == "now":
                # Do not acquire the native session lock from its own command.
                if not settings.enabled:
                    yield event.plain_result("请先在插件配置中启用每日换新。")
                    return
                self.coordinator.request_run(settings, event.unified_msg_origin)
                result = "已排队；指令结束且会话空闲后执行。同一逻辑日只换新一次。"
            elif action == "retry":
                self.coordinator.retry_summaries(settings, event.unified_msg_origin)
                result = "当前会话待整理摘要已重新排队；原始消息始终保留。"
            else:
                result = "用法：/auto_reset status | preview | now | retry。时间和范围请在插件配置中保存。"
            yield event.plain_result(result)
        except Exception as exc:
            yield event.plain_result(f"操作未完成：{type(exc).__name__}。请检查状态与配置。")
