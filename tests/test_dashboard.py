from __future__ import annotations

import asyncio
import importlib
import json
import sys
import tempfile
import types
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from rollover.adapters import AstrBotAdapter
from rollover.dashboard import Dashboard, PanelError, PLUGIN_NAME
from rollover.engine import Coordinator, Settings
from rollover.state import Journal
from test_rollover import Context, Locks, NOW, SESSION, OTHER


class Config(dict):
    fail = False
    saves = 0

    def save_config(self, values):
        self.update(values)
        if self.fail:
            raise OSError("SECRET provider-token and chat transcript")
        self.saves += 1


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.context = Context()
        self.config = Config(enabled=False, sessions=[SESSION], hour=4, minute=0, unrelated="keep")
        adapter = AstrBotAdapter(self.context, "4.29.1", lock_manager=Locks(), registry=NS(_events={}, _background_tasks={}))
        self.coordinator = Coordinator(Journal(self.temp.name), adapter, clock=lambda: NOW)
        self.plugin = NS(context=self.context, config=self.config, coordinator=self.coordinator, _panel_live=True,
                         settings=lambda: Settings.from_config(self.config))
        self.panel = Dashboard(self.plugin)

    def payload(self, **changes):
        config, revision = self.panel.settings_payload()
        return {"revision": revision, "config": {**config, **changes}}

    async def test_status_is_read_only_and_omits_private_archive_fields(self):
        key = self.coordinator.key(SESSION, "2026-10-25")
        self.coordinator.journal.put(key, {"session": SESSION, "day": "2026-10-25", "phase": "done",
                                         "archive": "SECRET/archive", "raw": "SECRET transcript", "old_cid": "SECRET cid",
                                         "pending_ids": ["SECRET timeline id"], "summary_state": "pending"})
        response = await self.panel.status()
        self.assertNotIn("SECRET", json.dumps(response))
        self.assertNotIn("unrelated", response["config"])
        self.assertEqual(response["jobs"][0]["pending_count"], 1)
        self.assertTrue(response["checks"][0]["ok"])
        self.assertEqual(self.context.conversation_manager.created, 0)
        self.assertEqual(self.context.summary_calls, 0)
        self.assertEqual(self.config.saves, 0)

    async def test_save_persists_only_known_fields_and_is_immediately_visible(self):
        await self.panel.save(self.payload(enabled=True, hour=5, minute=15))
        self.assertEqual(self.config["unrelated"], "keep")
        self.assertEqual(self.plugin.settings().hour, 5)
        self.assertEqual(self.config.saves, 1)
        await self.coordinator.tick(self.plugin.settings())
        self.assertEqual(self.context.conversation_manager.created, 0)

    async def test_invalid_payloads_are_rejected_without_writes(self):
        cases = [None, {}, self.payload(enabled="true"), self.payload(hour=True), self.payload(minute=60),
                 self.payload(sessions="123456"), self.payload(sessions=[OTHER + ":member"]),
                 self.payload(sessions=[123456]), self.payload(enabled=True, sessions=[]),
                 self.payload(sessions=[SESSION, "other:GroupMessage:123456"])]
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(PanelError):
                    await self.panel.save(payload)
        self.assertEqual(self.config.saves, 0)

    async def test_stale_revision_and_busy_worker_do_not_overwrite_settings(self):
        payload = self.payload(hour=5)
        self.config["hour"] = 6
        with self.assertRaises(PanelError) as cm:
            await self.panel.save(payload)
        self.assertEqual(cm.exception.status, 409)
        async with self.coordinator.lock:
            with self.assertRaises(PanelError):
                await self.panel.save(self.payload(hour=7))
        self.assertEqual(self.config["hour"], 6)
        self.assertEqual(self.config.saves, 0)

    async def test_save_failure_restores_config_without_exposing_exception(self):
        self.config.fail = True
        before = deepcopy(dict(self.config))
        with self.assertRaises(PanelError) as cm:
            await self.panel.save(self.payload(hour=7))
        self.assertEqual(dict(self.config), before)
        self.assertNotIn("SECRET", str(cm.exception))

    async def test_action_requires_enabled_confirmed_current_saved_scope(self):
        _, revision = self.panel.settings_payload()
        base = {"action": "now", "session": SESSION, "revision": revision, "confirmed": True}
        with self.assertRaises(PanelError):
            await self.panel.action(base)
        self.config["enabled"] = True
        _, revision = self.panel.settings_payload()
        base["revision"] = revision
        for changes in [{"confirmed": False}, {"session": OTHER}, {"revision": "stale"}, {"action": "delete"}]:
            with self.subTest(changes=changes):
                with self.assertRaises(PanelError):
                    await self.panel.action({**base, **changes})
        self.assertEqual(self.coordinator.journal.jobs, {})
        result = await self.panel.action(base)
        self.assertIn("排队", result["message"])
        self.assertEqual(self.context.conversation_manager.created, 0)
        await self.coordinator.tick(self.plugin.settings())
        self.assertEqual(self.context.conversation_manager.created, 1)
        await self.panel.action(base)
        await self.coordinator.tick(self.plugin.settings())
        self.assertEqual(self.context.conversation_manager.created, 1)

    async def test_retry_cannot_race_with_worker_and_does_not_create_conversation(self):
        self.config["enabled"] = True
        _, revision = self.panel.settings_payload()
        payload = {"action": "retry", "session": SESSION, "revision": revision, "confirmed": True}
        async with self.coordinator.lock:
            with self.assertRaises(PanelError):
                await self.panel.action(payload)
        await self.panel.action(payload)
        self.assertEqual(self.context.conversation_manager.created, 0)


class APIBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Only replace AstrBot's HTTP facade; exercise the real route handlers.
        facade = types.ModuleType("astrbot.api.web")
        facade.request = NS(username="admin", plugin_name=PLUGIN_NAME)
        facade.json_response = lambda body, status_code=200: {"body": body, "status": status_code}
        facade.error_response = lambda message, status_code=400: {"error": message, "status": status_code}
        with patch.dict(sys.modules, {"astrbot.api.web": facade}):
            self.web = importlib.import_module("rollover.web")
        self.identity = patch.object(self.web, "request", NS(username="admin", plugin_name=PLUGIN_NAME))
        self.identity.start()
        self.addCleanup(self.identity.stop)
        self.routes = []
        self.plugin = NS(_panel_live=True, context=NS(register_web_api=lambda *args: self.routes.append(args)))
        self.api = self.web.PanelAPI(self.plugin)

    async def test_missing_identity_wrong_plugin_and_stopped_instance_are_rejected(self):
        called = False
        async def operation():
            nonlocal called
            called = True
            return {}
        self.web.request.username = None
        self.assertEqual((await self.api.respond(operation))["status"], 401)
        self.web.request.username = "admin"
        self.web.request.plugin_name = "another_plugin"
        self.assertEqual((await self.api.respond(operation))["status"], 401)
        self.web.request.plugin_name = PLUGIN_NAME
        self.plugin._panel_live = False
        self.assertEqual((await self.api.respond(operation))["status"], 503)
        self.assertFalse(called)

    async def test_routes_and_response_envelope_mask_untrusted_exceptions(self):
        self.api.register()
        self.assertEqual({r[0] for r in self.routes}, {f"/{PLUGIN_NAME}/panel/{s}" for s in ("status", "settings", "action")})
        async def fail():
            raise RuntimeError("SECRET credentials and transcript")
        result = await self.api.respond(fail)
        self.assertEqual(result["status"], 500)
        self.assertNotIn("SECRET", str(result))
        async def good():
            return {"ok": True}
        self.assertEqual((await self.api.respond(good))["body"], {"ok": True})


if __name__ == "__main__":
    unittest.main()
