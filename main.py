"""定时自动执行 /reset 的插件"""

import json
import os
from datetime import datetime, date

from astrbot.api import logger
from astrbot.api.all import AstrMessageEvent, Context, Star, register
from astrbot.api.event import filter
from astrbot.core.config.astrbot_config import AstrBotConfig

_PLUGIN_DIR = os.path.abspath(os.path.dirname(__file__))
_STATE_FILE = os.path.join(_PLUGIN_DIR, "auto_reset_state.json")

_DEFAULT_STATE = {
    "last_reset_date": None,
}


def _load_state() -> dict:
    try:
        if os.path.exists(_STATE_FILE):
            with open(_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"[AutoReset] 读取状态文件失败: {e}")
    return dict(_DEFAULT_STATE)


def _save_state(data: dict) -> None:
    try:
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"[AutoReset] 写入状态文件失败: {e}")


@register("auto_reset", "草莓精的自动重启", version="1.1.0", desc="每隔 N 天自动执行 /reset，可调相隔天数与执行时间")
class AutoResetPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        # 优先使用 AstrBot 面板传入的配置，没有则用本地 JSON
        if config is not None:
            self._cfg = config
        else:
            logger.warning("[AutoReset] 未收到 AstrBot 配置，尝试使用本地默认配置")
            self._cfg = {"enabled": False, "interval_days": 7, "hour": 3, "minute": 0}

        self.state = _load_state()
        self._cron_job_id = None

    # ── 配置快捷读取 ──────────────────────────────────
    @property
    def _enabled(self) -> bool:
        return bool(self._cfg.get("enabled", False))

    @property
    def _interval_days(self) -> int:
        return int(self._cfg.get("interval_days", 7))

    @property
    def _hour(self) -> int:
        return int(self._cfg.get("hour", 3))

    @property
    def _minute(self) -> int:
        return int(self._cfg.get("minute", 0))

    # ── 生命周期 ──────────────────────────────────────
    async def initialize(self):
        if self._enabled:
            await self._register_cron()
        else:
            logger.info("[AutoReset] 未启用，使用 /auto_reset on 或面板开启。")

    async def terminate(self):
        await self._unregister_cron()
        logger.info("[AutoReset] 已卸载。")

    # ── Cron 管理 ─────────────────────────────────────
    async def _register_cron(self):
        """注册每分钟检查一次的定时任务"""
        try:
            cron_mgr = self.context.cron_manager
            job_name = "AutoReset_Check"

            if self._cron_job_id:
                try:
                    await cron_mgr.delete_job(self._cron_job_id)
                except Exception:
                    pass
                self._cron_job_id = None

            # 每分钟执行一次检查，看是否到了重置时间
            job = await cron_mgr.add_basic_job(
                name=job_name,
                cron_expression="* * * * *",
                handler=self._on_cron_tick,
                description="AutoReset: 每分钟检查是否需要执行 /reset",
                persistent=True,
            )
            self._cron_job_id = job.job_id
            logger.info(
                f"[AutoReset] 已启动监控: 每 {self._interval_days} 天 "
                f"{self._hour:02d}:{self._minute:02d} 执行 /reset"
            )
        except Exception as e:
            logger.error(f"[AutoReset] 注册定时任务失败: {e}", exc_info=True)

    async def _unregister_cron(self):
        if self._cron_job_id:
            try:
                await self.context.cron_manager.delete_job(self._cron_job_id)
            except Exception:
                pass
            self._cron_job_id = None

    # ── 定时检查 ──────────────────────────────────────
    async def _on_cron_tick(self):
        """每分钟触发一次，检查是否满足执行条件"""
        if not self._enabled:
            return

        # 检查当前时间是否匹配设定时间
        now = datetime.now()
        if now.hour != self._hour or now.minute != self._minute:
            return

        logger.info("[AutoReset] 时间匹配，检查是否需要重置...")

        if not self._should_reset_today():
            logger.info("[AutoReset] 未到间隔天数，跳过")
            return

        logger.info("[AutoReset] 开始重置...")
        await self._do_reset()

        self.state["last_reset_date"] = date.today().isoformat()
        _save_state(self.state)
        logger.info(f"[AutoReset] 完成，下次约 {self._interval_days} 天后")

    def _should_reset_today(self) -> bool:
        days = self._interval_days
        last = self.state.get("last_reset_date")
        if last is None:
            return True
        try:
            last_date = datetime.strptime(last, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return True
        return (date.today() - last_date).days >= days

    # ── 执行重置 ──────────────────────────────────────
    async def _do_reset(self):
        reset_count = 0
        fail_count = 0
        try:
            conv_mgr = self.context.conversation_manager
            sessions = dict(conv_mgr.session_conversations)
            if not sessions:
                logger.info("[AutoReset] 无活跃会话")
                return
            for umo, cid in sessions.items():
                try:
                    await conv_mgr.update_conversation(umo, cid, [])
                    reset_count += 1
                except Exception as e:
                    logger.warning(f"[AutoReset] 重置 {umo} 失败: {e}")
                    fail_count += 1
            for umo in sessions:
                try:
                    await self.context.send_message(
                        session=umo,
                        message="🔄 自动重置完成，对话上下文已清空。",
                    )
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"[AutoReset] 异常: {e}", exc_info=True)
        logger.info(f"[AutoReset] 成功 {reset_count} / 失败 {fail_count}")

    # ── 命令 ──────────────────────────────────────────
    @filter.command("auto_reset")
    async def auto_reset_cmd(self, event: AstrMessageEvent):
        parts = event.message_str.strip().split()
        if len(parts) < 2:
            yield event.plain_result(
                "📋 可用命令:\n"
                "  /auto_reset interval <天数>   设置相隔天数\n"
                "  /auto_reset time <时:分>      设置执行时间\n"
                "  /auto_reset on                 启用\n"
                "  /auto_reset off                关闭\n"
                "  /auto_reset now                立即重置\n"
                "  /auto_reset status             查看状态\n"
                "💡 也可以去 AstrBot 面板 → 插件配置 直接修改"
            )
            return

        cmd = parts[1]

        if cmd == "interval":
            if len(parts) < 3:
                yield event.plain_result("用法: /auto_reset interval <天数>")
                return
            try:
                days = int(parts[2])
                if days < 1:
                    yield event.plain_result("天数必须 ≥ 1")
                    return
                self._cfg["interval_days"] = days
                yield event.plain_result(f"✅ 相隔天数已设为 {days} 天\n（面板中也会同步更新）")
            except ValueError:
                yield event.plain_result("天数必须是正整数")

        elif cmd == "time":
            if len(parts) < 3:
                yield event.plain_result("用法: /auto_reset time <时:分>")
                return
            try:
                t = parts[2].split(":")
                h, m = int(t[0]), int(t[1]) if len(t) > 1 else 0
                if not (0 <= h <= 23 and 0 <= m <= 59):
                    yield event.plain_result("格式错误，示例: 03:00")
                    return
                self._cfg["hour"] = h
                self._cfg["minute"] = m
                yield event.plain_result(f"✅ 执行时间已设为 {h:02d}:{m:02d}\n（面板中也会同步更新）")
            except (ValueError, IndexError):
                yield event.plain_result("格式错误，示例: /auto_reset time 03:00")

        elif cmd == "on":
            self._cfg["enabled"] = True
            await self._register_cron()
            yield event.plain_result(
                f"✅ 已启用，每 {self._interval_days} 天 "
                f"{self._hour:02d}:{self._minute:02d} 执行"
            )

        elif cmd in ("off", "stop", "disable"):
            self._cfg["enabled"] = False
            yield event.plain_result("✅ 已关闭")

        elif cmd == "status":
            if self._enabled:
                last_date = self.state.get("last_reset_date", "从未")
                yield event.plain_result(
                    f"📊 自动重置状态:\n"
                    f"  ├ 状态: ✅ 已启用\n"
                    f"  ├ 相隔: 每 {self._interval_days} 天\n"
                    f"  ├ 时间: {self._hour:02d}:{self._minute:02d}\n"
                    f"  └ 上次重置: {last_date}"
                )
            else:
                yield event.plain_result(
                    "📊 自动重置状态: ❌ 未启用\n"
                    "使用 /auto_reset on 或面板开启"
                )

        elif cmd == "now":
            yield event.plain_result("🔄 手动重置中...")
            await self._do_reset()
            self.state["last_reset_date"] = date.today().isoformat()
            _save_state(self.state)

        else:
            yield event.plain_result(
                "可用命令:\n"
                "  interval <天数>   设置相隔天数\n"
                "  time <时:分>      设置执行时间\n"
                "  on / off          启用/关闭\n"
                "  now               立即重置\n"
                "  status            查看状态"
            )
