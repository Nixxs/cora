"""Approving, from the person's own Signal chat, tools that change things. Only their own reply counts."""

import asyncio
from contextvars import copy_context
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from backend_chat import BackendRoute
from messaging.messages import APPROVAL_SECONDS, approval_answer, approval_request_text
from messaging.receiver import IncomingMessage
from messaging.setup import LINKED, SignalStatus
from services.confirmation import APPROVED, DECLINED, TIMED_OUT, Confirmations
from storage.client_store import Client
from storage.signal_settings_store import SignalSettings
from tools.route import current_route
import test_keyboard_persistence as keyboard_tests

CORA = "+61400000001"
NICK = "+61400000002"
SARAH = "+61400000003"


def incoming(text, sender=NICK):
    return IncomingMessage(sender, text, datetime.now(timezone.utc))


class AnswerTests(unittest.TestCase):
    def test_only_a_clear_yes_or_no_counts(self):
        for text in ("YES", "yes.", " Yep! ", "approve", "Yes please"):
            with self.subTest(text=text):
                self.assertIs(approval_answer(text), True)
        for text in ("no", "Nope.", "don’t", "cancel", "No thanks"):
            with self.subTest(text=text):
                self.assertIs(approval_answer(text), False)
        for text in ("yes but change the title", "what will it say?", "ok", "sure", ""):
            with self.subTest(text=text):
                self.assertIsNone(approval_answer(text))

    def test_the_request_shows_exactly_what_would_be_done(self):
        request = SimpleNamespace(title="GitHub", tool="create_issue", arguments={"title": "Standby bug"})
        text = approval_request_text(request)
        self.assertIn("GitHub · create_issue", text)
        self.assertIn('"title": "Standby bug"', text)
        self.assertIn("Reply YES to approve or NO to decline", text)
        self.assertIn(f"{APPROVAL_SECONDS // 60} minutes", text)
        # A whole email with its task and brief is shown in full; anything longer is cut.
        email = approval_request_text(SimpleNamespace(title="Email", tool="send_email", arguments={"message": "x" * 4000}))
        self.assertIn("x" * 4000, email)
        long = approval_request_text(SimpleNamespace(title="GitHub", tool="t", arguments={"body": "x" * 20000}))
        self.assertLess(len(long), 8300)


class ChatApprovalQueueTests(unittest.IsolatedAsyncioTestCase):
    async def ask(self, queue, route):
        context = copy_context()
        context.run(current_route.set, route)
        return await asyncio.get_running_loop().create_task(
            queue.ask("github", "create_issue", {"title": "x"}), context=context,
        )

    async def test_a_call_from_the_persons_signal_chat_is_asked_there_not_with_the_pop_up(self):
        for answer, expected in ((True, APPROVED), (False, DECLINED)):
            with self.subTest(answer=answer):
                shown = []
                ask = AsyncMock(return_value=answer)
                queue = Confirmations(shown.append, MagicMock(), MagicMock())
                self.assertEqual(await self.ask(queue, BackendRoute(ask_approval=ask, approval_seconds=1)), expected)
                self.assertEqual(shown, [])
                self.assertEqual(ask.await_args.args[0].tool, "create_issue")

    async def test_no_answer_in_time_is_timed_out(self):
        async def never(request):
            await asyncio.Event().wait()
        queue = Confirmations(MagicMock(), MagicMock(), MagicMock())
        self.assertEqual(await self.ask(queue, BackendRoute(ask_approval=never, approval_seconds=0.05)), TIMED_OUT)

    async def test_other_chats_still_use_the_pop_up(self):
        shown = []
        queue = Confirmations(shown.append, MagicMock(), MagicMock(), timeout=0.05)
        self.assertEqual(await self.ask(queue, BackendRoute()), TIMED_OUT)
        self.assertEqual(len(shown), 1)


class AppApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def linked(self, app):
        app.signal.status = SignalStatus(LINKED, account=CORA)
        app.signal_settings = SignalSettings(True, CORA, NICK)
        app.signal_clients = {SARAH: Client(uuid4(), "Sarah", SARAH)}
        app.signal.client = MagicMock()
        app.signal.client.send = AsyncMock()
        app.signal.client.send_typing = AsyncMock()
        app.backend_chat.reply = AsyncMock(return_value="Sure.")
        app.live.note_signal_exchange = AsyncMock()
        app.persistence.client_conversation.return_value = uuid4()
        app.persistence.load_client_history.return_value = []

    async def start_asking(self, app):
        ask = app.signal_approval_asker(CORA, NICK, NICK)
        request = SimpleNamespace(title="GitHub", tool="create_issue", arguments={"title": "Standby bug"})
        waiting = asyncio.ensure_future(ask(request))
        for _ in range(20):
            if NICK in app.signal_approvals:
                break
            await asyncio.sleep(0.01)
        return waiting

    async def settle(self, app, pilot):
        for _ in range(3):
            await pilot.pause()
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)

    async def test_the_persons_yes_approves_and_is_shown_not_answered(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.linked(app)
            waiting = await self.start_asking(app)
            sent = app.signal.client.send.await_args.args
            self.assertEqual(sent[:2], (CORA, NICK))
            self.assertIn("Reply YES", sent[2])
            app.on_signal_message(incoming("Yes"))
            self.assertIs(await asyncio.wait_for(waiting, 1), True)
            await self.settle(app, pilot)
            app.backend_chat.reply.assert_not_called()
            self.assertIn("You · Signal: Yes", app.conversation.log_entries)
            self.assertEqual(app.signal_approvals, {})

    async def test_anything_else_declines_and_is_answered_as_a_message(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.linked(app)
            waiting = await self.start_asking(app)
            app.on_signal_message(incoming("wait, change the title first"))
            self.assertIs(await asyncio.wait_for(waiting, 1), False)
            await self.settle(app, pilot)
            app.backend_chat.reply.assert_awaited_once()
            self.assertEqual(app.backend_chat.reply.await_args.kwargs["content"][0]["text"],
                             "wait, change the title first")

    async def test_nobody_else_can_answer_it(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.linked(app)
            waiting = await self.start_asking(app)
            app.on_signal_message(incoming("YES", sender=SARAH))
            app.on_signal_message(incoming("YES", sender="+61499999999"))
            await self.settle(app, pilot)
            self.assertFalse(waiting.done())
            waiting.cancel()

    async def test_a_real_tool_called_from_the_persons_signal_chat_asks_there(self):
        import json
        from ui.confirm_view import ConfirmScreen
        async with keyboard_tests.running_app() as (app, pilot):
            await self.linked(app)
            asked = []

            async def ask(request):
                asked.append(request)
                return True

            route = BackendRoute(ask_approval=ask, approval_seconds=5)
            self.assertTrue(app.tools.handle_function_call({
                "name": "send_signal_message", "call_id": "c1", "status": "completed",
                "arguments": json.dumps({"to": "Sarah", "message": "Hi Sarah, I'm Cora.", "task": None}),
            }, route=route))
            output = json.loads(await route.wait_for("c1", 5))
            self.assertEqual(output["status"], "ok")
            self.assertEqual([request.tool for request in asked], ["send_signal_message"])
            self.assertNotIsInstance(app.screen, ConfirmScreen)
            app.signal.client.send.assert_awaited_with(CORA, SARAH, "Hi Sarah, I'm Cora.")
