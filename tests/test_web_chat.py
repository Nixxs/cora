"""Talking to Cora from the web page (WB2): the message endpoint, safe rendering, and the shared chat."""

import asyncio
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx2 as httpx

from backend_chat import WEB_EXCLUDED
from server.listener import build_app
from session_config import WEB_CHAT_INSTRUCTIONS
from storage.web_device_store import WebDevice
from web.pairing import Pairing, token_hash
from web.rendering import chat_html
from web.routes import COOKIE, MESSAGE_MAX_CHARS, REPLY_FAILED, WebAccess, web_routes
import test_keyboard_persistence as keyboard_tests

TOKEN = "a-paired-devices-token"
NOW = datetime(2026, 11, 4, 9, tzinfo=timezone.utc)
IPHONE = WebDevice(uuid4(), "iPhone", NOW, NOW)


def lines(response) -> list[dict]:
    return [json.loads(text) for text in response.text.splitlines() if text.strip()]


class RenderingTests(unittest.TestCase):
    def test_her_markdown_becomes_html(self):
        html = chat_html("A **light** day:\n\n- one\n- two\n\n`code` and [a link](https://example.com)")
        for part in ("<strong>light</strong>", "<li>one</li>", "<code>code</code>", '<a href="https://example.com">'):
            self.assertIn(part, html)

    def test_nothing_she_quotes_can_add_markup_or_scripts(self):
        html = chat_html('<script>alert(1)</script> <img src=x onerror=alert(1)> [click](javascript:alert(1))')
        self.assertNotIn("<script", html)
        self.assertNotIn("<img", html)
        self.assertNotIn('href="javascript', html)
        self.assertIn("&lt;script&gt;", html)

    def test_code_with_a_language_is_highlighted_and_still_escaped(self):
        html = chat_html('```python\nif x < 1:\n    print("<script>")  # hi\n```')
        self.assertIn('<code class="language-python"><span class="k">if</span>', html)
        self.assertIn('<span class="c1"># hi</span>', html)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        # No language, or one Pygments doesn't know: shown plain, escaped as before.
        for fence in ("```\nx < y\n```", "```nosuchlang\nx < y\n```"):
            plain = chat_html(fence)
            self.assertNotIn("<span", plain)
            self.assertIn("x &lt; y", plain)


class MessageRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sent = []
        self.refs = []
        self.reply = "Here's **tomorrow**."
        self.pieces = ["Here's ", "Here's **tomo", "Here's **tomorrow**."]

        async def find_device(hashed):
            return IPHONE if hashed == token_hash(TOKEN) else None

        async def send_message(device, text, on_text, ref=None):
            self.refs.append(ref)
            self.sent.append((device, text))
            for piece in self.pieces:
                on_text(piece)
                await asyncio.sleep(0)
            return self.reply

        self.access = WebAccess(Pairing(), AsyncMock(), find_device, send_message=send_message)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(web_routes(self.access))),
                                        base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def post(self, body, paired=True, **kwargs):
        headers = {"Cookie": f"{COOKIE}={TOKEN}"} if paired else {}
        if body is not None:
            kwargs["json"] = body
        return await self.client.post("/api/messages", headers=headers, **kwargs)

    async def test_only_a_paired_device_can_send(self):
        self.assertEqual((await self.post({"text": "Hi"}, paired=False)).status_code, 401)
        self.assertEqual(self.sent, [])

    async def test_her_reply_streams_in_pieces_then_arrives_finished(self):
        answer = await self.post({"text": "  What's on tomorrow?  "})
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(answer.headers["content-type"], "application/x-ndjson")
        self.assertEqual(answer.headers["cache-control"], "no-store")
        updates = lines(answer)
        self.assertEqual(self.sent, [(IPHONE, "What's on tomorrow?")])
        deltas = [update["text"] for update in updates if update["type"] == "delta"]
        self.assertEqual("".join(deltas), "Here's **tomorrow**.")
        self.assertEqual(updates[-1], {"type": "done", "html": chat_html(self.reply)})

    async def test_the_pages_mark_for_its_message_is_passed_on_and_checked(self):
        for ref, expected in (("abc-123", "abc-123"), ("x" * 65, None), (42, None), (None, None)):
            await self.post({"text": "Hi", "ref": ref})
            self.assertEqual(self.refs[-1], expected)

    async def test_a_failed_reply_says_so(self):
        self.reply = None
        self.assertEqual(lines(await self.post({"text": "Hi"}))[-1], {"type": "error", "message": REPLY_FAILED})

        async def broken(device, text, on_text, ref=None):
            raise RuntimeError("boom")

        self.access.send_message = broken
        self.assertEqual(lines(await self.post({"text": "Hi"}))[-1], {"type": "error", "message": REPLY_FAILED})

    async def test_empty_too_long_or_not_json_is_refused(self):
        self.assertEqual((await self.post({"text": "   "})).status_code, 400)
        self.assertEqual((await self.post({"nothing": 1})).status_code, 400)
        self.assertEqual((await self.post(None, data={"text": "Hi"})).status_code, 400)
        self.assertEqual((await self.post({"text": "x" * (MESSAGE_MAX_CHARS + 1)})).status_code, 413)
        self.assertEqual(self.sent, [])

    async def test_without_an_app_to_answer_it_says_so(self):
        self.access.send_message = None
        self.assertEqual((await self.post({"text": "Hi"})).status_code, 503)


class WebChatAppTests(unittest.IsolatedAsyncioTestCase):
    async def settle(self, app, pilot):
        for _ in range(3):
            await pilot.pause()
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)

    async def test_your_message_joins_the_desks_chat_and_is_answered_there_too(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.backend_chat.reply = AsyncMock(return_value="It's at 2pm.")
            app.live.note_signal_exchange = AsyncMock()
            app.conversation.log_entries.append("Cora: Earlier, by voice")
            records = []
            app.conversation.record_message = lambda role, text, channel, device=None: records.append(
                (role, text, channel, device))
            streamed = []
            reply = await app.answer_on_web(IPHONE, "When's the dentist?", streamed.append)
            await self.settle(app, pilot)
            self.assertEqual(reply, "It's at 2pm.")
            kwargs = app.backend_chat.reply.await_args.kwargs
            self.assertEqual(kwargs["content"], [{"type": "input_text", "text": "When's the dentist?"}])
            # One conversation with the desk: earlier voice is history, and the new message isn't repeated.
            self.assertEqual([h["content"][0]["text"] for h in kwargs["history"]], ["Earlier, by voice"])
            self.assertEqual(kwargs["extra_instructions"], WEB_CHAT_INSTRUCTIONS)
            # Maps only with a Google Maps key (none here).
            self.assertEqual(kwargs["route"].excluded, WEB_EXCLUDED | {"show_map", "call_someone", "claude_code", "run_claude_code"})
            # Streams to the page as she writes.
            self.assertEqual(kwargs["on_text"], streamed.append)
            # Labelled and saved with the device it came from; her reply has none.
            self.assertIn("You · web · iPhone: When's the dentist?", app.conversation.log_entries)
            self.assertIn("Cora · web:", app.conversation.log_entries)
            self.assertEqual(records, [("user", "When's the dentist?", "web", "iPhone"),
                                       ("assistant", "It's at 2pm.", "web", None)])
            app.live.note_signal_exchange.assert_awaited_once_with(
                "When's the dentist?", "It's at 2pm.", "wrote to you on your web page",
            )
            history = app.conversation.get_reconnect_history()
            self.assertEqual([(h["role"], h["content"][0]["text"]) for h in history][-2:],
                             [("user", "When's the dentist?"), ("assistant", "It's at 2pm.")])

    async def test_a_failed_reply_shows_your_message_but_no_answer(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.backend_chat.reply = AsyncMock(side_effect=RuntimeError("down"))
            app.live.note_signal_exchange = AsyncMock()
            self.assertIsNone(await app.answer_on_web(IPHONE, "Hello?", lambda text: None))
            await self.settle(app, pilot)
            self.assertIn("You · web · iPhone: Hello?", app.conversation.log_entries)
            self.assertNotIn("Cora · web:", app.conversation.log_entries)
            app.live.note_signal_exchange.assert_not_awaited()

    async def test_messages_are_answered_one_at_a_time_each_with_the_last_in_its_history(self):
        async with keyboard_tests.running_app() as (app, pilot):
            release = asyncio.Event()
            replies = iter(["First answer", "Second answer"])

            async def reply(**kwargs):
                await release.wait()
                return next(replies)

            app.backend_chat.reply = AsyncMock(side_effect=reply)
            app.live.note_signal_exchange = AsyncMock()
            first = asyncio.ensure_future(app.answer_on_web(IPHONE, "one", lambda text: None))
            second = asyncio.ensure_future(app.answer_on_web(IPHONE, "two", lambda text: None))
            await pilot.pause()
            self.assertEqual(app.backend_chat.reply.await_count, 1)
            release.set()
            self.assertEqual(await asyncio.wait_for(asyncio.gather(first, second), 3), ["First answer", "Second answer"])
            history = app.backend_chat.reply.await_args_list[1].kwargs["history"]
            self.assertEqual([h["content"][0]["text"] for h in history][-2:], ["one", "First answer"])
