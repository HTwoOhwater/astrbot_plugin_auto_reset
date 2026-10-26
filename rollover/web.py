"""Authenticated AstrBot plugin-view endpoints; no additional server or port."""
from astrbot.api.web import error_response, json_response, request

from .dashboard import Dashboard, PanelError, PLUGIN_NAME


class PanelAPI:
    def __init__(self, plugin):
        self.plugin = plugin
        self.dashboard = Dashboard(plugin)

    def register(self):
        for endpoint, handler, methods in (("panel/status", self.status, ["GET"]),
                                            ("panel/settings", self.save, ["POST"]),
                                            ("panel/action", self.action, ["POST"])):
            self.plugin.context.register_web_api(f"/{PLUGIN_NAME}/{endpoint}", handler, methods, "每日记忆管理")

    async def respond(self, operation, *, body=False):
        # AstrBot authenticates extension routes. Check bound identity too, and
        # reject stale handlers if the plugin has been disabled/reloaded.
        if not request.username or request.plugin_name != PLUGIN_NAME:
            return error_response("请先登录 AstrBot 后台。", status_code=401)
        if not self.plugin._panel_live:
            return error_response("插件已停止，请启用插件后刷新页面。", status_code=503)
        try:
            payload = await request.json(default=None) if body else None
            result = await operation(payload) if body else await operation()
            return json_response(result)
        except PanelError as exc:
            return error_response(str(exc), status_code=exc.status)
        except Exception:
            # Exceptions from dependencies can contain provider credentials.
            return error_response("操作暂未完成，请刷新检查配置和任务状态。", status_code=500)

    async def status(self):
        return await self.respond(self.dashboard.status)

    async def save(self):
        return await self.respond(self.dashboard.save, body=True)

    async def action(self):
        return await self.respond(self.dashboard.action, body=True)
