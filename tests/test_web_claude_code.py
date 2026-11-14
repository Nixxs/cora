"""Claude Code's sessions on the web page: the list under her voice and the pop-up that follows one."""

from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx2 as httpx

from app_areas.claude_code import ClaudeCodeMixin
from claude_code.sessions import SessionTracker
from server.listener import build_app
from storage.web_device_store import WebDevice
from web.pairing import Pairing, token_hash
from web.routes import COOKIE, STATIC_DIR, WebAccess, web_routes

TOKEN = "a-paired-devices-token"
NOW = datetime(2026, 11, 14, 9, tzinfo=timezone.utc)
IPHONE = WebDevice(uuid4(), "iPhone", NOW, NOW)


async def find_device(hashed):
    return IPHONE if hashed == token_hash(TOKEN) else None


class ClaudeCodeRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sessions = [{"id": "s1", "project": "ComboDots", "state": "working", "ago": "just now", "detail": "dots"}]
        self.asked = []

        async def claude_session(session_id):
            self.asked.append(session_id)
            return {"project": "ComboDots", "exchanges": []} if session_id == "s1" else None

        self.access = WebAccess(Pairing(), AsyncMock(), find_device, claude_sessions=lambda: self.sessions,
                                claude_session=claude_session)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(web_routes(self.access))),
                                        base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def get(self, path, paired=True):
        headers = {"Cookie": f"{COOKIE}={TOKEN}"} if paired else {}
        return await self.client.get(path, headers=headers)

    async def test_only_a_paired_device_sees_them(self):
        for path in ("/api/claude-code", "/api/claude-code/s1"):
            self.assertEqual((await self.get(path, paired=False)).status_code, 401)
        self.assertEqual(self.asked, [])

    async def test_the_list_or_that_cora_isnt_listening(self):
        listed = await self.get("/api/claude-code")
        self.assertEqual(listed.json(), {"listening": True, "sessions": self.sessions})
        self.assertEqual(listed.headers["cache-control"], "no-store")
        self.sessions = None
        self.assertEqual((await self.get("/api/claude-code")).json(), {"listening": False})

    async def test_one_session_followed_or_not_found(self):
        self.assertEqual((await self.get("/api/claude-code/s1")).json()["project"], "ComboDots")
        self.assertEqual((await self.get("/api/claude-code/gone")).status_code, 404)
        # Ids are checked before anything is looked up.
        self.assertEqual((await self.get("/api/claude-code/a.b")).status_code, 404)
        self.assertEqual(self.asked, ["s1", "gone"])


class ClaudeCodeForThePageTests(unittest.IsolatedAsyncioTestCase):
    """What the app gives the page, from the sessions it's heard."""

    def setUp(self):
        self.app = SimpleNamespace(claude_code_listen=True, claude_sessions=SessionTracker(clock=lambda: NOW))
        self.app.claude_sessions_shown = lambda: ClaudeCodeMixin.claude_sessions_shown(self.app)
        self.app.claude_sessions.update({"hook_event_name": "UserPromptSubmit", "session_id": "s1",
                                         "cwd": r"D:\Games\ComboDots", "prompt": "five dots " + "x" * 300})

    def test_the_list_is_the_desks(self):
        [listed] = ClaudeCodeMixin.web_claude_sessions(self.app)
        self.assertEqual({key: listed[key] for key in ("id", "project", "state", "ago")},
                         {"id": "s1", "project": "ComboDots", "state": "working", "ago": "just now"})
        self.assertLessEqual(len(listed["detail"]), 120)
        self.app.claude_code_listen = False
        self.assertIsNone(ClaudeCodeMixin.web_claude_sessions(self.app))

    async def test_one_session_with_its_latest_exchanges(self):
        with patch("app_areas.claude_code.read_exchanges", return_value=[{"who": "you", "said": "five dots"}]):
            found = await ClaudeCodeMixin.web_claude_session(self.app, "s1")
        self.assertEqual((found["project"], found["folder"], found["for"]),
                         ("ComboDots", r"D:\Games\ComboDots", "under a minute"))
        self.assertEqual(found["exchanges"], [{"who": "you", "said": "five dots"}])
        self.assertIsNone(await ClaudeCodeMixin.web_claude_session(self.app, "gone"))


class ClaudeCodePageTests(unittest.TestCase):
    def test_the_page_shows_what_a_session_said_as_text_never_markup(self):
        script = (STATIC_DIR / "claude.js").read_text(encoding="utf-8")
        self.assertNotIn("innerHTML", script)
        self.assertIn('id="claude-panel"', (STATIC_DIR / "index.html").read_text(encoding="utf-8"))
