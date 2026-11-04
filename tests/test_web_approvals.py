"""Approving tools from the web page, and the desk's state shown there (WB4a)."""

import asyncio
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx2 as httpx

from app_areas.web_interface import approval_detail
from messaging.messages import APPROVAL_SECONDS
from server.listener import Listener, build_app
from services.confirmation import APPROVED, TIMED_OUT, ConfirmationRequest, Confirmations
from session_config import EMAIL_CHAT_INSTRUCTIONS, SIGNAL_CHAT_INSTRUCTIONS, WEB_CHAT_INSTRUCTIONS
from storage.web_device_store import WebDevice
from ui.conversation import Conversation
from web.feed import APPROVAL, APPROVAL_DONE, MessageFeed
from web.pairing import Pairing, token_hash
from web.routes import COOKIE, WebAccess, web_routes
import test_keyboard_persistence as keyboard_tests

TOKEN = "a-paired-devices-token"
NOW = datetime(2026, 11, 4, 9, tzinfo=timezone.utc)
IPHONE = WebDevice(uuid4(), "iPhone", NOW, NOW)


async def find_device(hashed):
    return IPHONE if hashed == token_hash(TOKEN) else None


class ApprovalDetailTests(unittest.TestCase):
    def test_each_argument_on_its_own_line_and_long_ones_cut(self):
        detail = approval_detail({"to": "reception@example.com", "subject": "Thursday?", "cc": ["a", "b"],
                                  "body": "x" * 500})
        lines = detail.splitlines()
        self.assertEqual(lines[:3], ["to: reception@example.com", "subject: Thursday?", 'cc: ["a", "b"]'])
        self.assertTrue(lines[3].endswith("…"))
        self.assertLessEqual(len(lines[3]), len("body: ") + 300)


class ApprovalRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.answers = []
        self.waiting = {"a1"}

        def answer_approval(approval_id, approved):
            self.answers.append((approval_id, approved))
            return approval_id in self.waiting

        access = WebAccess(Pairing(), AsyncMock(), find_device, answer_approval=answer_approval)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(web_routes(access))),
                                        base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def post(self, body, paired=True):
        headers = {"Cookie": f"{COOKIE}={TOKEN}"} if paired else {}
        return await self.client.post("/api/approvals", json=body, headers=headers)

    async def test_only_a_paired_device_can_answer(self):
        self.assertEqual((await self.post({"id": "a1", "approved": True}, paired=False)).status_code, 401)
        self.assertEqual(self.answers, [])

    async def test_an_answer_to_a_waiting_request_is_taken(self):
        answer = await self.post({"id": "a1", "approved": False})
        self.assertEqual((answer.status_code, answer.json()), (200, {"answered": True}))
        self.assertEqual(self.answers, [("a1", False)])

    async def test_one_no_longer_waiting_says_so(self):
        self.assertEqual((await self.post({"id": "gone", "approved": True})).status_code, 409)

    async def test_only_an_id_and_a_true_or_false_answer_count(self):
        for body in ({"id": "a1"}, {"id": "a1", "approved": "yes"}, {"id": 7, "approved": True}, {}):
            with self.subTest(body=body):
                self.assertEqual((await self.post(body)).status_code, 400)
        self.assertEqual(self.answers, [])


class ApprovalAndStatusStreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.feed = MessageFeed()
        access = WebAccess(Pairing(), AsyncMock(), find_device, feed=self.feed, feed_ready=AsyncMock())
        self.server = Listener(build_app(web_routes(access)), 0)
        self.server.open()
        self.serving = asyncio.create_task(self.server.serve())
        for _ in range(50):
            if self.server.server is not None and self.server.server.started:
                break
            await asyncio.sleep(0.05)
        self.client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{self.server.socket.getsockname()[1]}")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.server.stop()
        await asyncio.wait_for(self.serving, 5)

    async def read_events(self, count):
        events, current = [], {}
        async with self.client.stream("GET", "/api/events", headers={"Cookie": f"{COOKIE}={TOKEN}"}) as response:
            async for text in response.aiter_lines():
                if not text:
                    if "event" in current:
                        events.append((current["event"], current.get("id"), json.loads(current.get("data", "{}"))))
                    current = {}
                    if len(events) == count:
                        break
                    continue
                field, _, value = text.partition(": ")
                current[field] = value
        return events

    async def test_a_page_gets_the_desks_state_then_approvals_with_their_outcome(self):
        self.feed.set_status("standby")
        self.feed.publish_approval(APPROVAL, {"id": "a1", "title": "Email · send_email", "detail": "to: x",
                                              "seconds": 600})

        async def later():
            await asyncio.sleep(0.2)
            self.feed.set_status("active")
            self.feed.publish_approval(APPROVAL_DONE, {"id": "a1", "outcome": "approved"})

        changing = asyncio.create_task(later())
        events = await asyncio.wait_for(self.read_events(5), 5)
        await changing
        self.assertEqual([name for name, _, _ in events], ["reset", "status", "approval", "status", "approval_done"])
        _, status_id, status = events[1]
        # The desk's state has no place in the conversation, so no id.
        self.assertEqual((status_id, status), (None, {"state": "standby"}))
        self.assertEqual({key: events[2][2][key] for key in ("id", "title", "detail", "seconds")},
                         {"id": "a1", "title": "Email · send_email", "detail": "to: x", "seconds": 600})
        self.assertEqual(events[3][2], {"state": "active"})
        self.assertEqual((events[4][2]["id"], events[4][2]["outcome"]), ("a1", "approved"))


class WebApprovalAppTests(unittest.IsolatedAsyncioTestCase):
    def request(self):
        return ConfirmationRequest("email", "send_email", {"to": "reception@example.com"}, "Email")

    async def test_a_request_shows_on_the_page_and_the_first_answer_counts(self):
        async with keyboard_tests.running_app() as (app, pilot):
            asking = asyncio.ensure_future(app.ask_on_web(self.request()))
            await pilot.pause()
            asked = app.web_feed.items[-1]
            self.assertEqual(asked.kind, APPROVAL)
            self.assertEqual((asked.data["title"], asked.data["detail"], asked.data["seconds"]),
                             ("Email · send_email", "to: reception@example.com", APPROVAL_SECONDS))
            self.assertTrue(app.answer_web_approval(asked.data["id"], True))
            self.assertTrue(await asyncio.wait_for(asking, 3))
            # A second answer, from another page, is too late.
            self.assertFalse(app.answer_web_approval(asked.data["id"], False))
            done = app.web_feed.items[-1]
            self.assertEqual((done.kind, done.data), (APPROVAL_DONE, {"id": asked.data["id"], "outcome": "approved"}))
            self.assertEqual(app.web_approvals, {})

    async def test_a_declined_or_lapsed_request_shows_as_such(self):
        async with keyboard_tests.running_app() as (app, pilot):
            asking = asyncio.ensure_future(app.ask_on_web(self.request()))
            await pilot.pause()
            app.answer_web_approval(app.web_feed.items[-1].data["id"], False)
            self.assertFalse(await asyncio.wait_for(asking, 3))
            self.assertEqual(app.web_feed.items[-1].data["outcome"], "declined")
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(app.ask_on_web(self.request()), 0.1)
            self.assertEqual(app.web_feed.items[-1].data["outcome"], "lapsed")
            self.assertEqual(app.web_approvals, {})
            self.assertFalse(app.answer_web_approval("unknown", True))

    async def test_web_chats_ask_on_the_page_and_her_instructions_say_so(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.backend_chat.reply = AsyncMock(return_value="Done.")
            app.live.note_signal_exchange = AsyncMock()
            await app.answer_on_web(IPHONE, "Email them", lambda text: None)
            route = app.backend_chat.reply.await_args.kwargs["route"]
            self.assertEqual(route.ask_approval, app.ask_on_web)
            self.assertEqual((route.approval_seconds, route.asked_where), (APPROVAL_SECONDS, "on the web page"))
            self.assertIn("Approve and Deny", WEB_CHAT_INSTRUCTIONS)

    async def test_the_confirmation_flow_runs_through_the_page(self):
        async with keyboard_tests.running_app() as (app, pilot):
            route = MagicMock(ask_approval=app.ask_on_web, approval_seconds=5, asked_where="on the web page")
            outcome = asyncio.ensure_future(app.confirmations.ask_in_chat(self.request(), route))
            await pilot.pause()
            app.answer_web_approval(app.web_feed.items[-1].data["id"], True)
            self.assertEqual(await asyncio.wait_for(outcome, 3), APPROVED)
            route.approval_seconds = 0.1
            self.assertEqual(await app.confirmations.ask_in_chat(self.request(), route), TIMED_OUT)
            self.assertEqual(app.web_feed.items[-1].data["outcome"], "lapsed")

    async def test_the_desks_state_reaches_the_feed(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.assertIsNotNone(app.web_feed.status)
            app.set_connection_state("standby")
            self.assertEqual(app.web_feed.status, "standby")
            app.set_connection_state("active")
            self.assertEqual(app.web_feed.status, "active")


class ApprovalOutcomeNoteTests(unittest.IsolatedAsyncioTestCase):
    """Cora knows in later turns what was approved, even if her reply at the time got it wrong."""

    async def test_every_outcome_is_told_wherever_it_was_asked(self):
        told = []
        confirmations = Confirmations(MagicMock(), MagicMock(), MagicMock(), timeout=0.2,
                                      on_outcome=lambda request, outcome: told.append((request.tool, outcome)))
        desk = asyncio.ensure_future(confirmations.ask("email", "send_email", {}, title="Email"))
        await asyncio.sleep(0)
        confirmations.answer(confirmations.waiting[0].id, False)
        self.assertEqual(await desk, "declined")
        self.assertEqual(await confirmations.ask("email", "send_email", {}, title="Email"), TIMED_OUT)

        async def yes(request):
            return True

        route = MagicMock(ask_approval=yes, approval_seconds=1)
        self.assertEqual(await confirmations.ask_in_chat(ConfirmationRequest("email", "send_email", {}, "Email"), route),
                         APPROVED)
        self.assertEqual(told, [("send_email", "declined"), ("send_email", TIMED_OUT), ("send_email", APPROVED)])

    async def test_a_failing_note_doesnt_change_the_answer(self):
        confirmations = Confirmations(MagicMock(), MagicMock(), MagicMock(),
                                      on_outcome=MagicMock(side_effect=RuntimeError("boom")))

        async def yes(request):
            return True

        route = MagicMock(ask_approval=yes, approval_seconds=1)
        self.assertEqual(await confirmations.ask_in_chat(ConfirmationRequest("email", "send_email", {}, "Email"), route),
                         APPROVED)

    def test_the_note_shows_quietly_and_goes_into_her_history(self):
        conversation = Conversation(MagicMock(), MagicMock(), MagicMock(), record_message=MagicMock())
        conversation.show_web_message("Email them please")
        conversation.show_approval_outcome("Email · send_email", APPROVED)
        conversation.show_written_reply("Sent.", label="Cora · web", channel="web")
        conversation.show_approval_outcome("Signal · send_signal_message", TIMED_OUT)
        self.assertIn("Cora · approval: you approved Email · send_email", conversation.log_entries)
        history = [(item["role"], item["content"][0]["text"]) for item in conversation.get_reconnect_history()]
        self.assertEqual(history, [
            ("user", "Email them please"),
            ("assistant", "You approved my request to use Email · send_email."),
            ("assistant", "Sent."),
            ("assistant", "You didn't answer my request to use Signal · send_signal_message in time, so it wasn't done."),
        ])
        # Not saved: only the two messages were.
        self.assertEqual(conversation.record_message.call_count, 2)

    def test_written_chats_report_the_result_rather_than_saying_its_waiting(self):
        for instructions in (WEB_CHAT_INSTRUCTIONS, SIGNAL_CHAT_INSTRUCTIONS, EMAIL_CHAT_INSTRUCTIONS):
            with self.subTest(instructions=instructions[:40]):
                self.assertNotIn("rather than that it's done", instructions)
                self.assertIn("Never say it's still waiting once you have the result", instructions)

    async def test_an_approval_on_the_page_is_noted_in_the_desks_chat(self):
        async with keyboard_tests.running_app() as (app, pilot):
            route = MagicMock(ask_approval=app.ask_on_web, approval_seconds=5, asked_where="on the web page")
            outcome = asyncio.ensure_future(app.confirmations.ask_in_chat(
                ConfirmationRequest("email", "send_email", {"to": "x@example.com"}, "Email"), route))
            await pilot.pause()
            app.answer_web_approval(app.web_feed.items[-1].data["id"], True)
            await asyncio.wait_for(outcome, 3)
            self.assertIn("Cora · approval: you approved Email · send_email", app.conversation.log_entries)
