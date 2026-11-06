"""Phone calls to Cora's number (P1): only Twilio gets an answer, only the person gets through, and
their call reaches her voice."""

import asyncio
import base64
from dataclasses import replace
import json
import re
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlencode

import httpx2 as httpx
import numpy as np
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from phone import twilio
from messaging.setup import LINKED, NOT_LINKED, SignalStatus
from phone.audio import CallAudio, DECODE, ReplyGaps, mulaw_to_pcm, pcm_to_mulaw
from services.confirmation import APPROVED, Confirmations
from phone.routes import PHONE_DEVICE, PhoneAccess, phone_routes
from server.listener import build_app
from storage.webhook_settings_store import WebhookSettings
from web.voice import VoiceRelay
import test_keyboard_persistence as keyboard_tests

TOKEN = "0123456789abcdef0123456789abcdef"
PUBLIC = "https://cora.example.com"
YOU = "+61400000001"
CALL = [("CallSid", "CA123"), ("From", YOU), ("To", "+61200000000"), ("CallStatus", "ringing")]


class TwilioTests(unittest.TestCase):
    def test_the_signature_matches_twilios_own_example(self):
        # From Twilio's security documentation (and its own libraries' tests).
        params = [("CallSid", "CA1234567890ABCDE"), ("Caller", "+12349013030"), ("Digits", "1234"),
                  ("From", "+12349013030"), ("To", "+18005551212")]
        self.assertEqual(twilio.signature("12345", "https://mycompany.com/myapp.php?foo=1&bar=2", params),
                         "0/KCTR6DLpKmkAf8muzZqo1nDgQ=")

    def test_a_signature_covers_the_address_and_every_field(self):
        url = PUBLIC + twilio.INCOMING_PATH
        signed = twilio.signature(TOKEN, url, CALL)
        self.assertTrue(twilio.valid_signature(TOKEN, url, list(reversed(CALL)), signed))
        self.assertFalse(twilio.valid_signature(TOKEN, url, [*CALL[:1], ("From", "+61499999999"), *CALL[2:]], signed))
        self.assertFalse(twilio.valid_signature(TOKEN, "https://elsewhere.example.com/phone/incoming", CALL, signed))
        self.assertFalse(twilio.valid_signature("f" * 32, url, CALL, signed))
        self.assertFalse(twilio.valid_signature("", url, CALL, signed))
        self.assertFalse(twilio.valid_signature(TOKEN, url, CALL, None))

    def test_only_the_same_full_number_matches(self):
        self.assertTrue(twilio.same_number("+61 400 000 001", YOU))
        for caller in ("+61400000002", "0400000001", "", "anonymous", None):
            self.assertFalse(twilio.same_number(caller, YOU))
        self.assertFalse(twilio.same_number(YOU, None))

    def test_what_twilio_says_is_escaped(self):
        answer = twilio.say_and_hang_up("Fish & chips <now>")
        self.assertIn("<Say language=\"en-AU\">Fish &amp; chips &lt;now&gt;</Say><Hangup/>", answer)

    def test_only_a_twilio_auth_token_is_saved_and_empty_forgets_it(self):
        with patch.object(twilio.keyring, "set_password") as saved, \
                patch.object(twilio.keyring, "delete_password") as deleted:
            self.assertEqual(twilio.save_twilio_token(f" {TOKEN} "), TOKEN)
            saved.assert_called_once_with("cora", twilio.TOKEN_ACCOUNT, TOKEN)
            with self.assertRaises(ValueError):
                twilio.save_twilio_token("not-a-token")
            self.assertEqual(twilio.save_twilio_token(""), "")
            deleted.assert_called_once_with("cora", twilio.TOKEN_ACCOUNT)

    def test_a_call_is_connected_to_her_stream_with_its_token(self):
        self.assertEqual(twilio.stream_url("https://cora.example.com/"), "wss://cora.example.com/phone/stream")
        answer = twilio.connect_stream("wss://cora.example.com/phone/stream", "a&b")
        self.assertIn('<Connect><Stream url="wss://cora.example.com/phone/stream">', answer)
        self.assertIn('<Parameter name="token" value="a&amp;b"/>', answer)

    def test_the_listener_runs_for_phone_calls_alone(self):
        self.assertTrue(WebhookSettings(public_url=PUBLIC, phone_enabled=True).listening)
        self.assertFalse(WebhookSettings(phone_enabled=True).listening)


def tone(hz: float, seconds: float, rate: int = 24000, level: int = 8000) -> np.ndarray:
    return (level * np.sin(2 * np.pi * hz * np.arange(int(rate * seconds)) / rate)).astype(np.int16)


class CallAudioTests(unittest.TestCase):
    def test_mulaw_is_g711(self):
        self.assertEqual((DECODE[0xFF], DECODE[0x7F], DECODE[0x00], DECODE[0x80]), (0, 0, -32124, 32124))
        samples = np.arange(-32768, 32768, dtype=np.int16)
        back = mulaw_to_pcm(pcm_to_mulaw(samples)).astype(int)
        # Within μ-law's step size everywhere.
        self.assertTrue(np.all(np.abs(back - samples) <= np.maximum(np.abs(samples.astype(int)) / 16, 16)))

    def test_her_voice_reaches_the_caller_at_8khz_and_theirs_reaches_her_at_24khz(self):
        audio = CallAudio()
        speech = tone(1000, 1)
        # In uneven pieces, as Live sends them: still exactly a third as many samples.
        phone = np.concatenate([mulaw_to_pcm(base64.b64decode(audio.for_caller(speech[i:i + 1001].tobytes())))
                                for i in range(0, len(speech), 1001)])
        self.assertEqual(len(phone), 8000)
        self.assertGreater(np.abs(phone[100:]).max(), 7000)
        caller = CallAudio()
        heard = np.concatenate([
            np.frombuffer(caller.for_live(base64.b64encode(pcm_to_mulaw(phone[i:i + 160])).decode()), dtype=np.int16)
            for i in range(0, len(phone), 160)])
        self.assertEqual(len(heard), 24000)
        self.assertGreater(np.abs(heard[300:]).max(), 7000)

    def test_sound_above_the_phone_band_isnt_folded_back_into_it(self):
        phone = mulaw_to_pcm(base64.b64decode(CallAudio().for_caller(tone(6000, 0.5).tobytes())))
        self.assertLess(np.abs(phone[100:]).max(), 200)


class ReplyGapTests(unittest.TestCase):
    def test_a_reply_is_timed_from_when_the_caller_stopped_to_her_first_audio(self):
        now = [0.0]
        gaps = ReplyGaps(clock=lambda: now[0])
        loud, quiet = tone(300, 0.02, level=4000).tobytes(), bytes(960)
        for step in range(50):  # a second of the caller talking
            now[0] = step * 0.02
            gaps.caller(loud)
        now[0] = 1.2
        gaps.caller(quiet)
        now[0] = 1.8
        gaps.her()
        now[0] = 1.9
        gaps.her()  # the same reply going on
        # Her talking over the caller isn't a reply.
        now[0] = 3.0
        gaps.caller(loud)
        now[0] = 3.03
        gaps.her()
        self.assertEqual([round(gap, 2) for gap in gaps.gaps], [0.82])
        self.assertIn("0.8 s after you stopped talking", gaps.summary())
        self.assertEqual(ReplyGaps().summary(), "")


class IncomingRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []

        def on_call(caller, call_id):
            self.calls.append((caller, call_id))
            return twilio.say_and_hang_up("Hello")

        access = PhoneAccess(token=lambda: TOKEN, public_url=lambda: PUBLIC, on_call=on_call)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(phone_routes(access))),
                                        base_url="http://127.0.0.1:8000")

    async def asyncTearDown(self):
        await self.client.aclose()

    def post(self, params, signed_with=TOKEN, content_type="application/x-www-form-urlencoded"):
        headers = {"Content-Type": content_type,
                   "X-Twilio-Signature": twilio.signature(signed_with, PUBLIC + twilio.INCOMING_PATH, params)}
        return self.client.post(twilio.INCOMING_PATH, content=urlencode(params), headers=headers)

    async def test_twilio_signed_calls_get_twiml(self):
        answer = await self.post(CALL)
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(answer.headers["content-type"].split(";")[0], "text/xml")
        self.assertIn("<Say", answer.text)
        self.assertEqual(self.calls, [(YOU, "CA123")])

    async def test_anything_not_signed_by_twilio_is_refused_before_it_is_read(self):
        self.assertEqual((await self.post(CALL, signed_with="f" * 32)).status_code, 403)
        unsigned = await self.client.post(twilio.INCOMING_PATH, content=urlencode(CALL),
                                          headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(unsigned.status_code, 403)
        self.assertEqual((await self.post(CALL, content_type="application/json")).status_code, 415)
        self.assertEqual(self.calls, [])


SPEECH = tone(500, 0.1).tobytes()


class StreamSocketTests(unittest.TestCase):
    def setUp(self):
        self.relay = VoiceRelay()
        self.tokens = {"good"}
        self.streams = []

        def claim(token):
            if token in self.tokens:
                self.tokens.discard(token)
                return True
            return False

        def on_stream(listener, connected):
            self.streams.append((listener.device, connected))
            if connected:
                self.relay.start_talking(listener)
            else:
                self.relay.stop_talking(listener)

        access = PhoneAccess(token=lambda: TOKEN, public_url=lambda: PUBLIC, on_call=lambda caller, call: "",
                             claim=claim, voice=self.relay, on_stream=on_stream)
        self.client = TestClient(build_app(phone_routes(access)))

    def start(self, socket, token="good"):
        socket.send_text(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
        socket.send_text(json.dumps({"event": "start", "start": {
            "streamSid": "MZ1", "callSid": "CA1", "customParameters": {"token": token},
            "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}}}))

    def wait_for(self, condition):
        for _ in range(100):
            if condition():
                return True
            asyncio.run(asyncio.sleep(0.01))
        return False

    def test_the_call_hears_her_and_she_hears_the_call(self):
        with self.client.websocket_connect(twilio.STREAM_PATH) as socket:
            self.start(socket)
            self.assertTrue(self.wait_for(lambda: self.relay.talker is not None))
            talker = self.relay.talker
            self.assertEqual(talker.device, PHONE_DEVICE)
            # 20 ms of the caller (160 μ-law bytes) is 20 ms of 24 kHz PCM16 for her.
            silence = base64.b64encode(bytes([0xFF]) * 160).decode()
            socket.send_text(json.dumps({"event": "media", "streamSid": "MZ1",
                                         "media": {"track": "inbound", "payload": silence}}))
            self.assertTrue(self.wait_for(lambda: not talker.mic.empty()))
            self.assertEqual(len(talker.mic.get_nowait()), 960)
            self.relay.play(base64.b64encode(SPEECH).decode())
            message = json.loads(socket.receive_text())
            self.assertEqual(message["event"], "media")
            self.assertEqual(message["streamSid"], "MZ1")
            self.assertGreater(len(base64.b64decode(message["media"]["payload"])), 700)
            socket.send_text(json.dumps({"event": "stop", "streamSid": "MZ1"}))
        self.assertTrue(self.wait_for(lambda: len(self.streams) == 2))
        self.assertEqual(self.streams, [(PHONE_DEVICE, True), (PHONE_DEVICE, False)])
        self.assertFalse(self.relay.active)

    def test_a_stream_without_a_fresh_token_is_closed(self):
        with self.client.websocket_connect(twilio.STREAM_PATH) as socket:
            self.start(socket)
            self.assertTrue(self.wait_for(lambda: self.relay.talker is not None))
            socket.send_text(json.dumps({"event": "stop"}))
        self.assertTrue(self.wait_for(lambda: len(self.streams) == 2))
        # The same token again, or one never given out: closed before reaching her.
        for token in ("good", "made-up"):
            with self.subTest(token=token), self.client.websocket_connect(twilio.STREAM_PATH) as socket:
                self.start(socket, token)
                with self.assertRaises(WebSocketDisconnect):
                    socket.receive_text()
        self.assertEqual(len(self.streams), 2)

    def test_the_call_ends_when_she_goes_into_standby(self):
        with self.client.websocket_connect(twilio.STREAM_PATH) as socket:
            self.start(socket)
            self.assertTrue(self.wait_for(lambda: self.relay.talker is not None))
            self.relay.tell(self.relay.talker, {"type": "talk_ended", "reason": "standby"})
            with self.assertRaises(WebSocketDisconnect):
                socket.receive_text()


class PhoneCallAppTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_your_number_gets_through_and_strangers_are_turned_away(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.webhook_settings = WebhookSettings(public_url=PUBLIC, phone_enabled=True)
            app.signal_settings = replace(app.signal_settings, allowed_number=YOU)
            app.activity.write = MagicMock()
            answer = app.on_phone_call(YOU, "CA1")
            self.assertIn('<Stream url="wss://cora.example.com/phone/stream">', answer)
            token = re.search(r'name="token" value="([^"]+)"', answer).group(1)
            # Each call's token lets its stream in once.
            self.assertTrue(app.claim_phone_token(token))
            self.assertFalse(app.claim_phone_token(token))
            self.assertFalse(app.claim_phone_token("made-up"))
            # Already on a call with her: a second call is told so.
            app.phone_listener = object()
            self.assertIn("already on a call", app.on_phone_call(YOU, "CA5"))
            app.phone_listener = None
            stranger = app.on_phone_call("+61499999123", "CA2")
            self.assertIn("I can only take calls from people I already know", stranger)
            # Activity says a stranger called without keeping their whole number.
            logged = str(app.activity.write.call_args_list[-1])
            self.assertIn("…123", logged)
            self.assertNotIn("61499999123", logged)
            self.assertIn("I can only take calls", app.on_phone_call("", "CA3"))
            app.webhook_settings = replace(app.webhook_settings, phone_enabled=False)
            self.assertIn("can't take calls right now", app.on_phone_call(YOU, "CA4"))

    async def test_a_connected_call_talks_like_talk_and_she_answers_it(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.live.ready = True
            app.standby = False
            listener = app.web_voice.listen(PHONE_DEVICE)
            with patch.object(app.live, "yield_speaker") as quiet_desk, \
                    patch.object(app.live, "use_web_microphone", AsyncMock()) as use_mic, \
                    patch.object(app.live, "send_phone_answer", AsyncMock()) as answer, \
                    patch.object(app.live, "note_where_they_are", AsyncMock(return_value=True)) as where:
                app.on_phone_stream(listener, True)
                await pilot.pause()
                self.assertIs(app.phone_listener, listener)
                self.assertIs(app.web_voice.talker, listener)
                quiet_desk.assert_called_once_with()
                use_mic.assert_awaited_with(listener.mic)
                answer.assert_awaited_once_with()
                self.assertIn("phone call", where.await_args.args[0])
                # No location during a call.
                result = await app.locate_person(True)
                self.assertEqual(result["status"], "unavailable")
                self.assertIn("phone call", result["message"])
                app.web_voice.stop(listener)
                app.on_phone_stream(listener, False)
                await pilot.pause()
                self.assertIsNone(app.phone_listener)
                self.assertIsNone(app.web_voice.talker)
                use_mic.assert_awaited_with(None)
            app.live.ready = False

    async def test_a_call_wakes_her_from_standby_and_hanging_up_puts_her_back(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.standby = True
            listener = app.web_voice.listen(PHONE_DEVICE)

            def reconnect():
                app.reconnecting = True
                app.standby = False

            with patch.object(app, "action_reconnect", side_effect=reconnect), \
                    patch.object(app.live, "use_web_microphone", AsyncMock()), \
                    patch.object(app, "request_standby") as standby:
                app.on_phone_stream(listener, True)
                self.assertEqual(app.pending_greeting, "phone")
                app.reconnecting = False
                app.live.ready = True
                app.web_voice.stop(listener)
                app.on_phone_stream(listener, False)
                standby.assert_called_once_with()
            app.live.ready = False

    async def test_on_a_call_approvals_go_to_the_signal_chat_never_the_call(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.signal.status = SignalStatus(LINKED, account="+61400000009")
            app.signal_settings = replace(app.signal_settings, allowed_number=YOU)
            self.assertIsNone(app.phone_approval_route())  # no call: the desk's pop-up
            app.phone_listener = object()
            asked = []

            def asker(account, recipient, number):
                async def ask(request):
                    asked.append((account, number, request.tool))
                    return True
                return ask

            with patch.object(app, "signal_approval_asker", side_effect=asker), \
                    patch.object(app, "tell_cora_approval_waiting") as told, \
                    patch.object(app, "show_confirmation") as pop_up:
                outcome = await app.confirmations.ask("tasks", "send_email", {"to": "a@example.com"}, title="Email")
                self.assertEqual(outcome, APPROVED)
                self.assertEqual(asked, [("+61400000009", YOU, "send_email")])
                told.assert_called_once_with("Email", "send_email", on_signal=True)
                pop_up.assert_not_called()
            # Without Signal linked, it waits on the desk instead.
            app.signal.status = SignalStatus(NOT_LINKED)
            self.assertIsNone(app.phone_approval_route())
            app.phone_listener = None

    async def test_settings_say_what_is_still_needed(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.webhook_settings = WebhookSettings(public_url=PUBLIC, phone_enabled=True)
            self.assertEqual(app.phone_incoming_url(), "https://cora.example.com/phone/incoming")
            with patch.object(app, "webhook_listening", return_value=True):
                app.twilio_token = ""
                app.signal_settings = replace(app.signal_settings, allowed_number=None)
                line, missing = app.phone_status()
                self.assertEqual(line, "● on · not answering yet")
                self.assertIn("Twilio auth token", missing)
                self.assertIn("your number under Signal", missing)
                app.twilio_token = TOKEN
                app.signal_settings = replace(app.signal_settings, allowed_number=YOU)
                self.assertEqual(app.phone_status(), ("● answering calls", ""))
            app.webhook_settings = WebhookSettings(public_url=PUBLIC)
            self.assertEqual(app.phone_status(), ("", ""))


if __name__ == "__main__":
    unittest.main()
