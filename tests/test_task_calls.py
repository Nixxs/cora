"""Calls she places for the person's tasks (P3a): approved first, placed through Twilio, in a session of
their own, with the transcript kept on the task and the person always told how it went."""

import asyncio
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlencode
from uuid import uuid4

import httpx2 as httpx

from app_areas import task_calls
from app_areas.contact_calls import CallRoute
from app_areas.task_calls import TaskCall, transcript_step
from phone import twilio
from phone.routes import PhoneAccess, phone_routes
from server.listener import build_app
from services.confirmation import APPROVED, DECLINED
from storage.task_store import STEP_MAX_CHARS, PhoneParty
from storage.webhook_settings_store import WebhookSettings, check_account_sid, check_phone_number
from tools.call_someone import CallSomeoneTool, validate_arguments
import test_keyboard_persistence as keyboard_tests

TOKEN = "0123456789abcdef0123456789abcdef"
SID = "AC" + "0" * 32
PUBLIC = "https://cora.example.com"
HER = "+61485008128"
YOU = "+61400000001"
SHOP = "+61299999999"
NEW_TASK = {"goal": "Find a plumber for Tuesday", "task_id": None,
            "brief": "Ask if they can come Tuesday morning and roughly what it costs.", "deadline": None}


def arguments(**changes):
    return {"number": SHOP, "name": "Ace Plumbing", "task": dict(NEW_TASK)} | changes


class TwilioCallTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_call_is_placed_from_her_number_in_her_region_with_a_time_limit(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["auth"] = request.headers["authorization"]
            seen["form"] = parse_qs(request.content.decode())
            return httpx.Response(201, json={"sid": "CA42"})

        sid = await twilio.place_call(SID, TOKEN, HER, SHOP, "<Response/>", PUBLIC + twilio.STATUS_PATH, 600,
                                      transport=httpx.MockTransport(handler))
        self.assertEqual(sid, "CA42")
        self.assertEqual(seen["url"], f"https://api.sydney.au1.twilio.com/2010-04-01/Accounts/{SID}/Calls.json")
        self.assertTrue(seen["auth"].startswith("Basic "))
        form = {key: values[0] for key, values in seen["form"].items()}
        self.assertEqual((form["From"], form["To"], form["TimeLimit"]), (HER, SHOP, "600"))
        self.assertEqual(form["StatusCallback"], "https://cora.example.com/phone/status")

    async def test_a_refusal_says_only_its_code(self):
        def handler(request):
            return httpx.Response(400, json={"code": 21215, "message": "Calls to +61299999999 aren't allowed"})

        with self.assertRaises(twilio.TwilioError) as raised:
            await twilio.place_call(SID, TOKEN, HER, SHOP, "<Response/>", PUBLIC, 600, transport=httpx.MockTransport(handler))
        self.assertEqual(raised.exception.code, 21215)
        self.assertNotIn("299999999", str(raised.exception))


class SettingsCheckTests(unittest.TestCase):
    def test_the_account_and_number_are_checked(self):
        self.assertEqual(check_phone_number("+61 485 008 128"), HER)
        self.assertEqual(check_account_sid(f" {SID} "), SID)
        for bad in ("0485008128", "+0123", "61485008128"):
            with self.assertRaises(ValueError):
                check_phone_number(bad)
        with self.assertRaises(ValueError):
            check_account_sid("SK" + "0" * 32)


class CallToolTests(unittest.IsolatedAsyncioTestCase):
    def tool(self, outcome=APPROVED, can_call=lambda number: "", place_call=None):
        self.confirm = AsyncMock(return_value=outcome)
        self.place_call = place_call or AsyncMock(return_value=uuid4())
        return CallSomeoneTool(lambda: None, lambda: False, MagicMock(), self.confirm,
                               AsyncMock(return_value="Find a plumber"), can_call, self.place_call)

    def test_arguments_need_a_full_number_and_a_task(self):
        validate_arguments(arguments())
        for bad in (arguments(number="0299999999"), arguments(task=None),
                    arguments(task=NEW_TASK | {"goal": None})):
            with self.assertRaises((ValueError, TypeError)):
                validate_arguments(bad)

    async def test_the_person_approves_who_the_number_and_the_brief_before_anything_is_placed(self):
        tool = self.tool()
        result = await tool.result(arguments())
        self.assertEqual(result["status"], "ok")
        shown = self.confirm.await_args.args[2]
        self.assertEqual(shown["call"], f"Ace Plumbing ({SHOP})")
        self.assertEqual(shown["brief"], NEW_TASK["brief"])
        self.assertIn("AI assistant", shown["on the call"])
        self.place_call.assert_awaited_once()
        self.assertEqual(self.place_call.await_args.args[:2], (SHOP, "Ace Plumbing"))

    async def test_declined_or_not_possible_places_nothing(self):
        tool = self.tool(outcome=DECLINED)
        self.assertEqual((await tool.result(arguments()))["status"], "not_done")
        self.place_call.assert_not_awaited()
        tool = self.tool(can_call=lambda number: "Calling out isn't set up yet.")
        result = await tool.result(arguments())
        self.assertEqual(result, {"status": "error", "message": "Calling out isn't set up yet."})
        self.confirm.assert_not_awaited()


class StatusRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_twilio_can_say_how_a_call_ended(self):
        statuses = []
        access = PhoneAccess(token=lambda: TOKEN, public_url=lambda: PUBLIC, on_call=lambda caller, call: "",
                             on_status=lambda call_id, status: statuses.append((call_id, status)))
        params = [("CallSid", "CA42"), ("CallStatus", "no-answer")]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(phone_routes(access))),
                                     base_url="http://127.0.0.1:8000") as client:
            signed = twilio.signature(TOKEN, PUBLIC + twilio.STATUS_PATH, params)
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            answer = await client.post(twilio.STATUS_PATH, content=urlencode(params),
                                       headers=headers | {"X-Twilio-Signature": signed})
            self.assertEqual(answer.status_code, 204)
            refused = await client.post(twilio.STATUS_PATH, content=urlencode(params), headers=headers)
            self.assertEqual(refused.status_code, 403)
        self.assertEqual(statuses, [("CA42", "no-answer")])


def ready_for_calls(app):
    app.webhook_settings = WebhookSettings(public_url=PUBLIC, phone_enabled=True, twilio_account_sid=SID,
                                           phone_number=HER)
    app.twilio_token = TOKEN
    app.signal_settings = replace(app.signal_settings, allowed_number=YOU)


class TaskCallAppTests(unittest.IsolatedAsyncioTestCase):
    async def test_calls_go_out_only_when_set_up_in_hours_and_one_at_a_time(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.webhook_settings = WebhookSettings(public_url=PUBLIC, phone_enabled=True)
            with patch.object(app, "webhook_listening", return_value=True):
                self.assertIn("isn't set up", app.can_call_out(SHOP))
                self.assertIn("call_someone", app.unavailable_tools())
                ready_for_calls(app)
                self.assertNotIn("call_someone", app.unavailable_tools())
                with patch.object(task_calls, "datetime") as clock:
                    clock.now.return_value = datetime(2026, 11, 6, 10, 0)
                    self.assertEqual(app.can_call_out(SHOP), "")
                    self.assertIn("own number", app.can_call_out(HER))
                    self.assertIn("message them instead", app.can_call_out(YOU))
                    app.task_call = object()
                    self.assertIn("already on a call", app.can_call_out(SHOP))
                    app.task_call = None
                    clock.now.return_value = datetime(2026, 11, 6, 21, 30)
                    self.assertIn("between 8am and 8pm", app.can_call_out(SHOP))

    async def test_placing_a_call_records_them_on_the_task_then_rings_them(self):
        async with keyboard_tests.running_app() as (app, pilot):
            ready_for_calls(app)
            task_id, party_id = uuid4(), uuid4()
            app.save_task_party = AsyncMock(return_value=task_id)
            app.task_goal = AsyncMock(return_value="Find a plumber for Tuesday")
            app.persistence.task_party_id.return_value = party_id
            with patch.object(twilio, "place_call", AsyncMock(return_value="CA42")) as place:
                self.assertEqual(await app.place_task_call(SHOP, "Ace Plumbing", "Find a plumber", None, "Ask."), task_id)
            party = app.save_task_party.await_args.args[0]
            self.assertEqual(party, PhoneParty(SHOP, "Ace Plumbing"))
            sid, token, from_number, to_number, twiml, status_url, limit = place.await_args.args
            self.assertEqual((sid, from_number, to_number, limit), (SID, HER, SHOP, 600))
            self.assertIn('<Stream url="wss://cora.example.com/phone/stream">', twiml)
            self.assertEqual(status_url, "https://cora.example.com/phone/status")
            call = app.task_call
            self.assertEqual((call.call_sid, call.party_id), ("CA42", party_id))
            # Its stream gets its own relay, never the person's.
            stream_token = twiml.split('name="token" value="')[1].split('"')[0]
            link = app.claim_phone_token(stream_token)
            self.assertIsNot(link.voice, app.web_voice)

    async def test_a_call_twilio_wont_place_is_noted_and_nothing_is_on(self):
        async with keyboard_tests.running_app() as (app, pilot):
            ready_for_calls(app)
            app.save_task_party = AsyncMock(return_value=uuid4())
            app.task_goal = AsyncMock(return_value="Find a plumber")
            with patch.object(twilio, "place_call", AsyncMock(side_effect=twilio.TwilioError(400, 21215))):
                with self.assertRaises(ValueError) as raised:
                    await app.place_task_call(SHOP, None, "Find a plumber", None, "Ask.")
            self.assertIn("error 21215", str(raised.exception))
            self.assertIsNone(app.task_call)
            self.assertIn("failed", app.persistence.add_task_step.call_args.args[1])

    async def test_an_unanswered_call_reaches_the_person_through_the_coordinator(self):
        async with keyboard_tests.running_app() as (app, pilot):
            call = TaskCall(SHOP, "Ace Plumbing", uuid4(), "Find a plumber", "Ask.", call_sid="CA42")
            app.task_call = call
            with patch.object(app, "coordinate_task") as coordinate:
                app.on_twilio_status("CA99", "no-answer")  # not this call
                self.assertIs(app.task_call, call)
                app.on_twilio_status("CA42", "no-answer")
                await pilot.pause()
            self.assertIsNone(app.task_call)
            self.assertIn("nobody answered", coordinate.call_args.args[2])
            self.assertFalse(coordinate.call_args.kwargs["settled"])

    async def run_call(self, app, pilot, call, events):
        async def run(on_event, on_mic_status, on_audio_status=None):
            call.live.connection = MagicMock()
            call.live.connection.session.commentary.append = AsyncMock()
            for event in events:
                on_event(event)
            await pilot.pause()
            call.live.connection = None

        call.live.run = run
        listener = call.relay.listen("phone")
        app.task_call = call
        app.persistence.load_session_context = MagicMock()
        app.on_task_call_stream(call, listener, True)
        await pilot.pause()
        await asyncio.wait_for(app.workers.wait_for_complete(), 3)
        await pilot.pause()

    def said(self, kind, text):
        return SimpleNamespace(type=kind, delta=text)

    async def test_her_report_comes_back_by_pass_on_and_the_transcript_is_kept(self):
        async with keyboard_tests.running_app() as (app, pilot):
            call = TaskCall(SHOP, "Ace Plumbing", uuid4(), "Find a plumber", "Ask about Tuesday.", party_id=uuid4())
            pass_on = SimpleNamespace(type="response.event", delegation_id="d1", event={
                "type": "response.output_item.done",
                "item": {"type": "function_call", "name": "pass_on", "call_id": "c1", "arguments": "{}"}})
            with patch.object(app.tools, "handle_function_call") as handle, \
                    patch.object(app, "coordinate_task") as coordinate:
                await self.run_call(app, pilot, call, [
                    SimpleNamespace(type="session.started"),
                    self.said("session.output_transcript.delta", "Hi, it's Cora, an AI assistant."),
                    self.said("session.input_transcript.delta", "Tuesday's fine, $120."),
                    pass_on])
                route = handle.call_args.kwargs["route"]
                self.assertIsInstance(route, CallRoute)
                self.assertEqual(route.email_party, (call.party_id, call.task_id, "Ace Plumbing"))
                coordinate.assert_not_called()
            step = app.persistence.add_task_step.call_args.args[1]
            self.assertIn("Cora: Hi, it's Cora, an AI assistant.", step)
            self.assertIn("Them: Tuesday's fine, $120.", step)
            self.assertIsNone(app.task_call)

    async def test_a_call_that_ends_without_a_report_still_reaches_the_person(self):
        async with keyboard_tests.running_app() as (app, pilot):
            call = TaskCall(SHOP, None, uuid4(), "Find a plumber", "Ask.")
            with patch.object(app, "coordinate_task") as coordinate:
                await self.run_call(app, pilot, call, [
                    self.said("session.input_transcript.delta", "Sorry, wrong number.")])
            self.assertIn("Them: Sorry, wrong number.", coordinate.call_args.args[2])

    async def test_she_hangs_up_once_her_goodbye_has_been_heard(self):
        async with keyboard_tests.running_app() as (app, pilot):
            call = TaskCall(SHOP, "Ace Plumbing", uuid4(), "Find a plumber", "Ask.")
            call.live.close = AsyncMock()
            busy = [True, True, False]
            call.relay.is_playback_busy = lambda: busy.pop(0) if busy else False
            with patch("app_areas.contact_calls.GOODBYE_START_SECONDS", 0):
                app.hang_up(call.live, call.relay, call.who)
                await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            call.live.close.assert_awaited_once_with()
            self.assertEqual(busy, [])

    def test_a_long_transcript_fits_a_task_step(self):
        call = TaskCall(SHOP, None, uuid4(), "Goal", "Brief", turns=[("user", "word " * 2000)])
        self.assertLessEqual(len(transcript_step(call, 3)), STEP_MAX_CHARS)


class EndCallToolTests(unittest.IsolatedAsyncioTestCase):
    def connection(self, end_call=None):
        connection = SimpleNamespace(response=SimpleNamespace(item=SimpleNamespace(create=AsyncMock()),
                                                              create=AsyncMock()))
        if end_call is not None:
            connection.end_call = end_call
        return connection

    async def test_only_a_call_can_be_hung_up_and_she_says_nothing_more(self):
        from tools.end_call import EndCallTool
        ended = MagicMock()
        on_call = self.connection(ended)
        await EndCallTool(lambda: on_call, lambda: False, MagicMock()).handle("c1", {})
        ended.assert_called_once_with()
        self.assertIn("Say nothing more", on_call.response.item.create.await_args.kwargs["item"]["output"])
        on_call.response.create.assert_not_awaited()
        elsewhere = self.connection()
        await EndCallTool(lambda: elsewhere, lambda: False, MagicMock()).handle("c2", {})
        self.assertIn("no call here", elsewhere.response.item.create.await_args.kwargs["item"]["output"])
        elsewhere.response.create.assert_awaited_once()

    def test_never_offered_in_the_persons_own_sessions_and_chats(self):
        from backend_chat import SIGNAL_EXCLUDED, STANDBY_EXCLUDED, WEB_EXCLUDED
        for excluded in (STANDBY_EXCLUDED, SIGNAL_EXCLUDED, WEB_EXCLUDED):
            self.assertIn("end_call", excluded)


if __name__ == "__main__":
    unittest.main()
