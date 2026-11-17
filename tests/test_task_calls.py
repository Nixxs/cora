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
from app_areas.task_calls import CUT_MARK, TaskCall, call_view, clock_length, cut_middle, said_lines, transcript_step
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


async def until(condition, seconds=3.0):
    """Waits for condition() to come true (failing the test if it doesn't in time)."""
    for _ in range(int(seconds / 0.02)):
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("it didn't happen in time")


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
            with patch.object(twilio, "place_call", AsyncMock(return_value="CA42")) as place, \
                    patch.object(app, "start_task_call_session") as start:
                self.assertEqual(await app.place_task_call(SHOP, "Ace Plumbing", "Find a plumber", None, "Ask."), task_id)
            # Her session connects while it rings, so she's there when they answer.
            start.assert_called_once_with(app.task_call)
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
        app.task_call_link(call)  # answered: its stream claimed the call's token
        app.on_task_call_stream(call, listener, True)
        await pilot.pause()
        await asyncio.wait_for(app.workers.wait_for_complete(), 3)
        await pilot.pause()

    def said(self, kind, text):
        return SimpleNamespace(type=kind, delta=text)

    def ringing(self, app, call):
        """Her session connects while the call rings, and stays on until it's closed."""
        events = asyncio.Queue()
        closed = asyncio.Event()

        async def run(on_event, on_mic_status, on_audio_status=None):
            call.live.connection = MagicMock()
            call.live.connection.session.commentary.append = AsyncMock()
            sent = call.live.connection.session.commentary.append
            run.sent = sent
            waiting = asyncio.ensure_future(closed.wait())
            while True:
                getting = asyncio.ensure_future(events.get())
                await asyncio.wait({getting, waiting}, return_when=asyncio.FIRST_COMPLETED)
                if waiting.done():
                    getting.cancel()
                    break
                on_event(getting.result())
            call.live.connection = None

        async def close():
            closed.set()

        call.live.run = run
        call.live.close = close
        call.live.append_commentary = AsyncMock()
        app.task_call = call
        app.persistence.load_session_context = MagicMock()
        return events

    async def test_she_is_ready_while_it_rings_and_answers_their_hello_herself(self):
        async with keyboard_tests.running_app() as (app, pilot):
            call = TaskCall(SHOP, "Ace Plumbing", uuid4(), "Find a plumber", "Ask.")
            events = self.ringing(app, call)
            with patch.object(task_calls, "ANSWER_WAIT_SECONDS", 0.05), patch.object(app, "coordinate_task"):
                app.start_task_call_session(call)
                events.put_nowait(SimpleNamespace(type="session.started"))
                await until(lambda: call.session_ready)
                call.live.append_commentary.assert_not_awaited()  # nobody's there yet
                # They answer: the call's audio goes to the session that was waiting.
                self.assertIsNotNone(app.task_call_link(call))
                listener = call.relay.listen("phone")
                app.on_task_call_stream(call, listener, True)
                self.assertIs(listener.mic, call.mic)
                call.relay.hear(listener, b"hello?")
                self.assertEqual(call.mic.get_nowait(), b"hello?")
                events.put_nowait(self.said("session.input_transcript.delta", "Hello?"))
                await asyncio.sleep(0.15)
                # They spoke, so she answers them herself: no prompt.
                call.live.append_commentary.assert_not_awaited()
                app.on_task_call_stream(call, None, False)
                await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            app.persistence.add_task_step.assert_called()

    async def test_answered_in_silence_she_is_prompted_to_start(self):
        async with keyboard_tests.running_app() as (app, pilot):
            call = TaskCall(SHOP, "Ace Plumbing", uuid4(), "Find a plumber", "Ask.")
            events = self.ringing(app, call)
            with patch.object(task_calls, "ANSWER_WAIT_SECONDS", 0.05), patch.object(app, "coordinate_task"):
                app.start_task_call_session(call)
                app.task_call_link(call)
                app.on_task_call_stream(call, call.relay.listen("phone"), True)  # answered before she's ready
                await asyncio.sleep(0.1)
                call.live.append_commentary.assert_not_awaited()
                events.put_nowait(SimpleNamespace(type="session.started"))
                await until(lambda: call.live.append_commentary.await_count)
                call.live.append_commentary.assert_awaited_once()
                self.assertIn("hasn't spoken", call.live.append_commentary.await_args.args[2])
                app.on_task_call_stream(call, None, False)
                await asyncio.wait_for(app.workers.wait_for_complete(), 3)

    async def test_a_call_nobody_answers_closes_her_waiting_session(self):
        async with keyboard_tests.running_app() as (app, pilot):
            call = TaskCall(SHOP, "Ace Plumbing", uuid4(), "Find a plumber", "Ask.", call_sid="CA42")
            self.ringing(app, call)
            with patch.object(app, "coordinate_task") as coordinate:
                app.start_task_call_session(call)
                await asyncio.sleep(0.05)
                app.on_twilio_status("CA42", "no-answer")
                await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            self.assertIsNone(app.task_call)
            coordinate.assert_called_once()
            self.assertIn("nobody answered", coordinate.call_args.args[2])
            app.persistence.save_task_call.assert_not_called()

    async def test_if_nothing_says_how_it_went_her_session_gives_up_after_ringing(self):
        async with keyboard_tests.running_app() as (app, pilot):
            call = TaskCall(SHOP, "Ace Plumbing", uuid4(), "Find a plumber", "Ask.")
            self.ringing(app, call)
            with patch.object(task_calls, "RING_SECONDS", 0.05), patch.object(app, "coordinate_task") as coordinate:
                app.start_task_call_session(call)
                await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            self.assertIsNone(app.task_call)
            self.assertIn("nobody answered", coordinate.call_args.args[2])

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

    def test_a_step_keeps_the_start_and_the_end_of_a_call_that_waited_through_a_menu_and_hold(self):
        # Her first real call: the menu and hold ads filled the step, and the person who answered was cut.
        menu = "To hear our trading hours, press one. To make a new purchase, press three. " * 6
        hold = "You could earn 90-day store cash on qualifying products. Please hold. " * 60
        turns = [("user", "Thank you for calling The Good Guys."), ("assistant", "."),
                 *[turn for _ in range(4) for turn in (("user", menu), ("assistant", "New purchase, please."))],
                 ("user", hold + "Good afternoon, Stacey, how can we help?"),
                 ("assistant", "Hi Stacey, I'm Cora, calling for Nick about two split systems."),
                 ("user", "We've got both in stock, $1,899 each installed."),
                 ("assistant", "Thanks, I'll pass that on.")]
        call = TaskCall(SHOP, "The Good Guys", uuid4(), "Goal", "Brief", turns=turns)
        step = transcript_step(call, 8)
        self.assertLessEqual(len(step), STEP_MAX_CHARS)
        self.assertTrue(step.startswith("Call with The Good Guys (8 min), transcript:\nThem: Thank you for calling"))
        self.assertIn(CUT_MARK, step)
        self.assertIn("Them: We've got both in stock, $1,899 each installed.\nCora: Thanks, I'll pass that on.", step)
        self.assertNotIn("Cora: .", step)

    def test_wordless_turns_are_dropped_and_the_words_either_side_joined(self):
        self.assertEqual(said_lines([("user", "Thank you for calling The Good"), ("assistant", "."),
                                     ("user", "Guys."), ("assistant", "Hi there."), ("assistant", "")]),
                         ["Them: Thank you for calling The Good Guys.", "Cora: Hi there."])

    def test_cutting_the_middle_fits_even_one_long_line(self):
        self.assertEqual(cut_middle(["Them: short"], 100), "Them: short")
        text = cut_middle(["Them: " + "a" * 5000, "Cora: " + "b" * 5000], 1000, head_chars=200)
        self.assertLessEqual(len(text), 1000)
        self.assertTrue(text.startswith("Them: aaa"))
        self.assertTrue(text.endswith("bbb"))
        self.assertIn(CUT_MARK, text)

    async def test_the_whole_call_is_kept_with_how_fast_she_replied(self):
        async with keyboard_tests.running_app() as (app, pilot):
            call = TaskCall(SHOP, "Ace Plumbing", uuid4(), "Find a plumber", "Ask.")
            record_id = uuid4()
            app.persistence.save_task_call = MagicMock(return_value=record_id)
            app.persistence.task_call_replies = MagicMock()
            long_answer = "Tuesday is fine. " * 400
            with patch.object(app, "coordinate_task"):
                await self.run_call(app, pilot, call, [
                    self.said("session.output_transcript.delta", "Hi, it's Cora."),
                    self.said("session.input_transcript.delta", long_answer)])
            task_id, who, number, began_at, transcript, replies = app.persistence.save_task_call.call_args.args
            self.assertEqual((task_id, who, number, replies), (call.task_id, "Ace Plumbing", SHOP, None))
            self.assertEqual(transcript, "Cora: Hi, it's Cora.\nThem: " + long_answer.strip())
            self.assertIsNotNone(began_at.tzinfo)
            self.assertEqual(call.record_id, record_id)
            # Her reply times come once the call's stream has closed.
            app.on_task_call_gaps(call, "")
            app.on_task_call_gaps(call, "Her replies began 0.9 s after you stopped talking, typically")
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            app.persistence.task_call_replies.assert_called_once_with(
                record_id, "Her replies began 0.9 s after you stopped talking, typically")


def tone_strength(samples, frequency, rate=24000):
    """How much of one frequency a stretch of audio holds (Goertzel), relative to its size."""
    import numpy as np
    t = np.arange(len(samples)) / rate
    return abs(np.sum(samples * np.exp(-2j * np.pi * frequency * t))) / len(samples)


class PressKeysTests(unittest.IsolatedAsyncioTestCase):
    def test_each_key_is_its_row_and_column_tones_with_quiet_between(self):
        import numpy as np
        from phone import dtmf
        audio = np.frombuffer(dtmf.tones("3#"), dtype="<i2").astype(float)
        tone, gap, lead = (int(seconds * dtmf.RATE) for seconds in (dtmf.TONE_SECONDS, dtmf.GAP_SECONDS,
                                                                        dtmf.LEAD_SECONDS))
        self.assertEqual(len(audio), lead + 2 * (tone + gap))
        three = audio[lead:lead + tone]
        hash_key = audio[lead + tone + gap:lead + 2 * tone + gap]
        for samples, (low, high), others in ((three, (697, 1477), (941, 1209)), (hash_key, (941, 1477), (697, 1336))):
            for frequency in (low, high):
                self.assertGreater(tone_strength(samples, frequency), 5000)
            for frequency in others:
                self.assertLess(tone_strength(samples, frequency), 500)
        self.assertEqual(np.abs(audio[:lead]).max(), 0)
        self.assertLess(np.abs(audio).max(), 32767)
        with self.assertRaises(KeyError):
            dtmf.tones("x")

    def test_only_keypad_keys_are_accepted(self):
        from tools.press_keys import validate_arguments
        validate_arguments({"keys": "3"})
        validate_arguments({"keys": "0412345678#"})
        for bad in ({"keys": ""}, {"keys": "three"}, {"keys": "3 4"}, {}, {"keys": "3", "more": 1}, {"keys": 3}):
            with self.assertRaises(ValueError):
                validate_arguments(bad)

    async def test_keys_are_pressed_only_on_a_call_and_she_waits_silently(self):
        import json
        from tools.press_keys import PressKeysTool
        pressed = MagicMock()
        on_call = EndCallToolTests.connection(None)
        on_call.press_keys = pressed
        await PressKeysTool(lambda: on_call, lambda: False, MagicMock()).handle("c1", {"keys": "3"})
        pressed.assert_called_once_with("3")
        output = json.loads(on_call.response.item.create.await_args.kwargs["item"]["output"])
        self.assertIn("wait silently", output["message"])
        elsewhere = EndCallToolTests.connection(None)
        await PressKeysTool(lambda: elsewhere, lambda: False, MagicMock()).handle("c2", {"keys": "3"})
        self.assertIn("no phone call", elsewhere.response.item.create.await_args.kwargs["item"]["output"])

    async def test_on_her_call_the_tones_go_down_the_line_and_into_the_transcript(self):
        from phone import dtmf
        from tools.end_call import CALL_ONLY
        self.assertIn("press_keys", CALL_ONLY)
        self.assertIn("press_keys", task_calls.TASK_CALL_TOOLS)
        async with keyboard_tests.running_app() as (app, pilot):
            call = TaskCall(SHOP, "The Good Guys", uuid4(), "Air conditioners", "Ask.")
            listener = call.relay.listen("phone")
            call.transcript.add("user", "To make a new purchase, press three.")
            app.press_call_keys(call, "3")
            frames = []
            while not listener.audio.empty():
                frames.append(listener.audio.get_nowait())
            self.assertEqual(b"".join(frames), dtmf.tones("3"))
            self.assertTrue(all(len(frame) <= 960 for frame in frames))  # 20 ms, like the rest of the call
            self.assertEqual(call.turns, [("user", "To make a new purchase, press three."), ("assistant", "[pressed 3]")])
            self.assertTrue(call.relay.is_playback_busy())  # a goodbye waits for the tones too

    def test_she_stays_silent_for_recordings_and_menus_and_greets_only_a_person(self):
        from session_config import TASK_CALL_LIVE_INSTRUCTIONS, phone_party_instructions
        rules = phone_party_instructions("The Good Guys", SHOP, "Nick")
        self.assertIn("As soon as a person answers", rules)
        for said in ("phone menu, hold music", "never fill their pauses", "press_keys", "On hold, stay silent",
                     "voicemail is the exception"):
            self.assertIn(said, rules)
        self.assertIn("press_keys", TASK_CALL_LIVE_INSTRUCTIONS)
        self.assertIn("stay completely silent", TASK_CALL_LIVE_INSTRUCTIONS)


class FollowingACallTests(unittest.IsolatedAsyncioTestCase):
    """A call she placed, followed as it happens beside the chat, in the dashboard and on the web page."""

    def test_it_shows_how_it_stands_for_how_long_and_whats_said_so_far(self):
        call = TaskCall(SHOP, "The Good Guys", uuid4(), "Air conditioners", "Ask.", started=100.0)
        view = call_view(call, 112.0)
        self.assertEqual((view["who"], view["number"], view["goal"], view["state"], view["for"], view["lines"]),
                         ("The Good Guys", SHOP, "Air conditioners", "ringing", "0:12", []))
        call.connected, call.answered_at = True, 120.0
        call.turns = [("user", "Thank you for calling."), ("assistant", ".")]
        call.transcript.add("assistant", "Hi, I'm Cora, ")
        call.transcript.add("assistant", "calling for Nick.")
        view = call_view(call, 185.0)
        self.assertEqual((view["state"], view["for"]), ("on the call", "1:05"))
        # The turn going on now is shown as it comes; wordless ones aren't.
        self.assertEqual(view["lines"], [{"who": "them", "said": "Thank you for calling."},
                                         {"who": "cora", "said": "Hi, I'm Cora, calling for Nick."}])
        call.done, call.ended_at = True, 300.0
        self.assertEqual((call_view(call, 900.0)["state"], call_view(call, 900.0)["for"]), ("ended", "3:00"))
        unanswered = TaskCall(SHOP, None, uuid4(), "Goal", "Ask.", started=0.0, done=True, ended_at=30.0)
        self.assertEqual((call_view(unanswered, 40.0)["state"], call_view(unanswered, 40.0)["who"]),
                         ("didn't connect", SHOP))
        self.assertEqual(clock_length(3725), "1:02:05")

    async def test_the_latest_call_is_shown_until_a_while_after_it_ends(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.assertIsNone(app.task_call_view())
            call = TaskCall(SHOP, "Ace Plumbing", uuid4(), "Find a plumber", "Ask.")
            app.shown_task_call = call
            self.assertEqual(app.task_call_view()["state"], "ringing")
            call.done, call.connected = True, True
            call.ended_at = task_calls.monotonic() - 60
            self.assertEqual(app.task_call_view()["state"], "ended")
            call.ended_at = task_calls.monotonic() - task_calls.CALL_SHOWN_SECONDS - 1
            self.assertIsNone(app.task_call_view())

    async def test_beside_the_chat_it_shows_while_theres_a_call_and_a_click_follows_it(self):
        from ui.call_view import CallEntry, CallScreen
        from ui.context_view import MarkdownView
        async with keyboard_tests.running_app() as (app, pilot):
            await pilot.resize_terminal(150, 40)
            await pilot.pause()
            panel = app.query_one("#side-panel")
            panel.show_claude_code()
            await pilot.pause()
            self.assertFalse(app.query_one("#side-call").display)
            call = TaskCall(SHOP, "Ace Plumbing", uuid4(), "Find a plumber", "Ask.", connected=True)
            call.turns = [("user", "Ace Plumbing, Dave speaking.")]
            app.shown_task_call = call
            panel.show_claude_code()
            await pilot.pause()
            self.assertTrue(app.query_one("#side-call").display)
            entry = app.query_one("#side-call-entry", CallEntry)
            self.assertIn("call with Ace Plumbing", str(entry.render()))
            self.assertIn("Them: Ace Plumbing, Dave speaking.", str(entry.render()))
            await pilot.click(entry)
            await pilot.pause()
            self.assertIsInstance(app.screen, CallScreen)
            self.assertIn("Them: Ace Plumbing, Dave speaking.", app.screen.query_one("#context-view-text", MarkdownView).text)
            # It keeps up as the call goes on.
            call.transcript.add("assistant", "Hi Dave, I'm Cora.")
            for _ in range(30):
                if "Hi Dave" in app.screen.query_one("#context-view-text", MarkdownView).text:
                    break
                await pilot.pause(0.1)
            self.assertIn("Cora: Hi Dave, I'm Cora.", app.screen.query_one("#context-view-text", MarkdownView).text)
            await pilot.press("escape")
            await pilot.pause()

    async def test_the_web_page_gets_it_only_on_a_paired_device(self):
        from storage.web_device_store import WebDevice
        from web.pairing import Pairing, token_hash
        from web.routes import COOKIE, WebAccess, web_routes
        device = WebDevice(uuid4(), "iPhone", datetime(2026, 11, 17), datetime(2026, 11, 17))

        async def find_device(hashed):
            return device if hashed == token_hash("token") else None

        shown = {"who": "Ace Plumbing", "state": "on the call", "for": "0:42", "lines": []}
        access = WebAccess(Pairing(), AsyncMock(), find_device, task_call=lambda: shown)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(web_routes(access))),
                                     base_url="http://test") as client:
            self.assertEqual((await client.get("/api/call")).status_code, 401)
            found = await client.get("/api/call", headers={"Cookie": f"{COOKIE}=token"})
            self.assertEqual(found.json(), {"call": shown})
            shown = None
            access.task_call = lambda: shown
            self.assertEqual((await client.get("/api/call", headers={"Cookie": f"{COOKIE}=token"})).json(),
                             {"call": None})


class ReadCallTests(unittest.IsolatedAsyncioTestCase):
    """read_call: a call she placed, read whole, for when the person asks what was said."""

    def saved(self, who, started, minutes, transcript, replies=None):
        from datetime import timedelta, timezone
        began = datetime(2026, 11, 17, *started, tzinfo=timezone.utc)
        return SimpleNamespace(who=who, started_at=began, ended_at=began + timedelta(minutes=minutes),
                               transcript=transcript, replies=replies)

    def test_only_a_task_and_a_calls_number_are_taken(self):
        from tools.read_call import validate_arguments
        validate_arguments({"task_id": str(uuid4()), "call": None})
        validate_arguments({"task_id": str(uuid4()), "call": 2})
        for bad in ({"task_id": "nope", "call": None}, {"task_id": str(uuid4()), "call": 0},
                    {"task_id": str(uuid4()), "call": True}, {"task_id": str(uuid4())},
                    {"task_id": str(uuid4()), "call": "1"}):
            with self.assertRaises(ValueError):
                validate_arguments(bad)

    async def test_she_reads_the_latest_call_whole_or_the_one_asked_for(self):
        async with keyboard_tests.running_app() as (app, pilot):
            task_id = uuid4()
            stacey = "\n".join(["Them: Good afternoon, Stacey speaking.",
                                *["Them: Both in stock, $1,899 each installed."] * 600])
            app.persistence.task_calls = MagicMock(return_value=[
                self.saved("The Good Guys", (5, 23), 8, "Them: Press three.\n" + stacey, "Her replies began 0.9 s"),
                self.saved("The Good Guys", (6, 5), 2, "Them: Thanks for calling back."),
            ])
            result = await app.read_task_call(task_id, None)
            app.persistence.task_calls.assert_called_once_with(task_id)
            self.assertEqual(result["call"], 2)
            self.assertEqual(result["transcript"], "Them: Thanks for calling back.")
            self.assertEqual([(call["call"], call["who"], call["minutes"]) for call in result["calls"]],
                             [(1, "The Good Guys", 8), (2, "The Good Guys", 2)])
            self.assertRegex(result["calls"][0]["when"], r"^[A-Z][a-z]{2} 1[78] Nov, \d{1,2}:\d\d [ap]m$")
            first = await app.read_task_call(task_id, 1)
            # Long calls keep their start and as much of the end as fits.
            self.assertLessEqual(len(first["transcript"]), task_calls.READ_CALL_MAX_CHARS)
            self.assertTrue(first["transcript"].startswith("Them: Press three.\nThem: Good afternoon, Stacey"))
            self.assertIn(CUT_MARK, first["transcript"])
            self.assertTrue(first["transcript"].endswith("Both in stock, $1,899 each installed."))
            self.assertEqual(first["replies"], "Her replies began 0.9 s")
            self.assertEqual((await app.read_task_call(task_id, 3))["status"], "error")
            app.persistence.task_calls = MagicMock(return_value=[])
            self.assertEqual((await app.read_task_call(task_id, None))["status"], "none")

    async def test_a_call_thats_on_now_is_read_as_far_as_its_got(self):
        async with keyboard_tests.running_app() as (app, pilot):
            call = TaskCall(SHOP, "Ace Plumbing", uuid4(), "Find a plumber", "Ask.", connected=True)
            call.turns = [("user", "Ace Plumbing, Dave speaking.")]
            call.transcript.add("assistant", "Hi Dave, I'm Cora.")
            app.shown_task_call = call
            app.persistence.task_calls = MagicMock()
            result = await app.read_task_call(call.task_id, None)
            app.persistence.task_calls.assert_not_called()
            self.assertEqual(result["on_now"]["state"], "on the call")
            self.assertEqual(result["on_now"]["transcript_so_far"],
                             "Them: Ace Plumbing, Dave speaking.\nCora: Hi Dave, I'm Cora.")

    async def test_the_tool_answers_with_what_she_reads(self):
        import json
        from tools.read_call import ReadCallTool
        task_id = uuid4()
        read = AsyncMock(return_value={"status": "ok", "transcript": "Them: Tuesday's fine."})
        connection = EndCallToolTests.connection(None)
        await ReadCallTool(lambda: connection, lambda: False, MagicMock(), read).handle(
            "c1", {"task_id": str(task_id), "call": None})
        read.assert_awaited_once_with(task_id, None)
        output = json.loads(connection.response.item.create.await_args.kwargs["item"]["output"])
        self.assertEqual(output["transcript"], "Them: Tuesday's fine.")
        connection.response.create.assert_awaited_once()

    def test_she_and_the_coordinator_have_it_but_contacts_and_calls_dont(self):
        from app_areas.contact_calls import CALL_TOOLS
        from app_areas.signal_chats import CLIENT_CHAT_TOOLS
        from app_areas.tasks import COORDINATOR_NOTE, COORDINATOR_TOOLS
        from session_config import instruction_sections
        self.assertIn("read_call", COORDINATOR_TOOLS)
        self.assertIn("read_call", COORDINATOR_NOTE)
        for allowed in (CALL_TOOLS, task_calls.TASK_CALL_TOOLS, CLIENT_CHAT_TOOLS):
            self.assertNotIn("read_call", allowed)
        self.assertIn("read_call", instruction_sections(tasks_context="[]")["tasks"])


class EndCallToolTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def connection(end_call=None):
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
