"""Hearing Cora on the web page (WV1): the voice relay, its WebSocket, and Live sending to it."""

import asyncio
import base64
from datetime import datetime, timezone
from struct import pack
import unittest
from unittest.mock import AsyncMock, MagicMock, call, patch
from uuid import uuid4

from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from live import CoraLive
from server.listener import build_app
from storage.web_device_store import WebDevice
from web.pairing import Pairing, token_hash
from web.routes import COOKIE, WebAccess, web_routes
from web.voice import BACKLOG_SECONDS, BYTES_PER_SECOND, PLAYBACK_MARGIN_SECONDS, VoiceRelay
from test_audio_controls import live_transport
import test_keyboard_persistence as keyboard_tests

TOKEN = "a-paired-devices-token"
NOW = datetime(2026, 11, 5, 9, tzinfo=timezone.utc)
IPHONE = WebDevice(uuid4(), "iPhone", NOW, NOW)
SPEECH = pack("<4h", 100, -200, 300, -400)
SILENCE = bytes(8)


def delta(chunk: bytes) -> str:
    return base64.b64encode(chunk).decode()


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class VoiceRelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_her_speech_goes_to_every_listener_but_silence_doesnt(self):
        relay = VoiceRelay()
        self.assertFalse(relay.active)
        relay.play(delta(SPEECH))  # nobody listening yet: nothing kept
        iphone, ipad = relay.listen("iPhone"), relay.listen("iPad")
        self.assertTrue(relay.active)
        relay.play(delta(SILENCE))
        relay.play(delta(SPEECH))
        for listener in (iphone, ipad):
            self.assertEqual(await relay.next_chunk(listener, 1), SPEECH)
            self.assertTrue(listener.audio.empty())
        relay.stop(iphone)
        relay.stop(ipad)
        self.assertFalse(relay.active)

    async def test_a_listener_far_behind_is_dropped_rather_than_drifting_later(self):
        relay = VoiceRelay()
        slow = relay.listen("iPhone")
        second = bytes(BYTES_PER_SECOND - 2) + pack("<h", 1)
        for _ in range(BACKLOG_SECONDS + 2):
            relay.play(delta(second))
        self.assertFalse(relay.active)
        chunks = list(slow.audio._queue)
        self.assertIsNone(chunks[-1])

    def test_it_knows_shes_still_speaking_on_a_page_from_how_long_the_audio_takes(self):
        clock = Clock()
        relay = VoiceRelay(clock)
        relay.listen("iPhone")
        one_second = pack("<h", 5) * 24000
        relay.play(delta(one_second))
        relay.play(delta(one_second))
        self.assertTrue(relay.is_playback_busy())
        clock.now += 2 + PLAYBACK_MARGIN_SECONDS - 0.01
        self.assertTrue(relay.is_playback_busy())
        clock.now += 0.02
        self.assertFalse(relay.is_playback_busy())


class VoiceSocketTests(unittest.TestCase):
    def setUp(self):
        self.relay = VoiceRelay()
        self.listening = []

        async def find_device(hashed):
            return IPHONE if hashed == token_hash(TOKEN) else None

        access = WebAccess(Pairing(), AsyncMock(), find_device, voice=self.relay,
                           on_listening=lambda device, on: self.listening.append((device.name, on)))
        self.client = TestClient(build_app(web_routes(access)), base_url="https://cora.example.com")

    # The test client always calls itself testserver for WebSockets.
    def connect(self, paired=True, origin="https://testserver"):
        headers = {"origin": origin} if origin else {}
        if paired:
            headers["cookie"] = f"{COOKIE}={TOKEN}"
        return self.client.websocket_connect("/api/voice", headers=headers)

    def test_a_paired_page_hears_her_speech_and_the_desk_knows_it_listens(self):
        with self.connect() as socket:
            self.assertEqual(socket.receive_json(), {"type": "listening"})
            self.assertTrue(self.relay.active)
            self.relay.play(delta(SPEECH))
            self.assertEqual(socket.receive_bytes(), SPEECH)
            self.assertEqual(self.listening, [("iPhone", True)])
        # Hung up: no longer listening, and the desk is told.
        for _ in range(50):
            if not self.relay.active:
                break
            asyncio.run(asyncio.sleep(0.02))
        self.assertFalse(self.relay.active)
        self.assertEqual(self.listening, [("iPhone", True), ("iPhone", False)])

    def test_an_unpaired_device_or_another_sites_page_cant_listen(self):
        for paired, origin in ((False, "https://testserver"), (True, "https://evil.example"), (True, None)):
            with self.subTest(paired=paired, origin=origin), self.assertRaises(WebSocketDisconnect):
                with self.connect(paired=paired, origin=origin) as socket:
                    socket.receive_json()
        self.assertEqual(self.listening, [])
        self.assertFalse(self.relay.active)


class LiveVoiceRelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_while_a_page_listens_her_voice_goes_there_and_the_desk_speaker_is_quiet(self):
        live = CoraLive()
        live.voice_relay = VoiceRelay()
        async with live_transport(live) as (connection, speaker, emit):
            await emit("session.output_audio.delta", delta=delta(SPEECH))
            self.assertEqual(speaker.play.call_args_list, [call(delta(SPEECH))])
            listener = live.voice_relay.listen("iPhone")
            await emit("session.output_audio.delta", delta=delta(SPEECH))
            self.assertEqual(speaker.play.call_count, 1)
            self.assertEqual(await live.voice_relay.next_chunk(listener, 1), SPEECH)
            # Standby waits for what's still playing on the page.
            speaker.is_playback_busy.return_value = False
            self.assertTrue(live.playback_busy())
            live.voice_relay.stop(listener)
            await emit("session.output_audio.delta", delta=delta(SPEECH))
            self.assertEqual(speaker.play.call_count, 2)

    async def test_a_page_starting_to_listen_quiets_what_the_desk_still_had_queued(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.assertIs(app.live.voice_relay, app.web_voice)
            app.live.speaker = MagicMock()
            listener = app.web_voice.listen("iPhone")
            app.on_web_listening(IPHONE, True)
            app.live.speaker.close.assert_called_once_with()
            app.web_voice.stop(listener)
            app.on_web_listening(IPHONE, False)
            app.live.speaker = None


class TalkRelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_page_talks_at_a_time_and_only_its_microphone_is_heard(self):
        relay = VoiceRelay()
        iphone, ipad = relay.listen("iPhone"), relay.listen("iPad")
        self.assertIsNone(relay.start_talking(iphone))
        relay.hear(iphone, SPEECH)
        relay.hear(ipad, SPEECH)  # not talking: dropped
        self.assertEqual(iphone.mic.get_nowait(), SPEECH)
        # The iPad takes over: the iPhone stops being heard.
        self.assertIs(relay.start_talking(ipad), iphone)
        self.assertIsNone(iphone.mic)
        self.assertFalse(relay.stop_talking(iphone))
        self.assertTrue(relay.stop_talking(ipad))
        self.assertIsNone(relay.talker)

    async def test_messages_reach_a_page_in_order_with_her_audio(self):
        relay = VoiceRelay()
        page = relay.listen("iPhone")
        relay.play(delta(SPEECH))
        relay.tell(page, {"type": "talking"})
        self.assertEqual(await relay.next_chunk(page, 1), SPEECH)
        self.assertEqual(await relay.next_chunk(page, 1), {"type": "talking"})


class TalkSocketTests(unittest.TestCase):
    def setUp(self):
        self.relay = VoiceRelay()
        self.talk = []
        self.heard = []

        async def find_device(hashed):
            return IPHONE if hashed == token_hash(TOKEN) else None

        def on_talk(device, listener, talking):
            self.talk.append(talking)
            if talking:
                self.relay.start_talking(listener)
                self.relay.tell(listener, {"type": "talking"})
            else:
                if listener.mic is not None:
                    while not listener.mic.empty():
                        self.heard.append(listener.mic.get_nowait())
                self.relay.stop_talking(listener)

        access = WebAccess(Pairing(), AsyncMock(), find_device, voice=self.relay, on_talk=on_talk)
        self.client = TestClient(build_app(web_routes(access)))

    def test_a_pages_microphone_reaches_her_while_talking_and_leaving_hangs_up(self):
        headers = {"origin": "https://testserver", "cookie": f"{COOKIE}={TOKEN}"}
        with self.client.websocket_connect("/api/voice", headers=headers) as socket:
            self.assertEqual(socket.receive_json(), {"type": "listening"})
            socket.send_json({"type": "talk", "on": True})
            self.assertEqual(socket.receive_json(), {"type": "talking"})
            socket.send_bytes(SPEECH)
            socket.send_bytes(bytes(64 * 1024))  # far too big for one piece: ignored
            socket.send_json({"type": "talk", "on": False})
            socket.send_json({"type": "talk", "on": True})
            self.assertEqual(socket.receive_json(), {"type": "talking"})
        for _ in range(50):
            if self.relay.talker is None and not self.relay.active:
                break
            asyncio.run(asyncio.sleep(0.02))
        # Leaving while talking hangs up too.
        self.assertIsNone(self.relay.talker)
        self.assertEqual(self.talk, [True, False, True, False])
        self.assertEqual(self.heard, [SPEECH])


class LiveWebMicrophoneTests(unittest.IsolatedAsyncioTestCase):
    async def test_while_a_page_talks_live_hears_it_and_the_desk_mic_again_after(self):
        live = CoraLive()
        async with live_transport(live) as (connection, speaker, emit):
            # The harness stands in for the desk microphone; the page's goes through the real path.
            stand_in = live.send_microphone
            live.send_microphone = CoraLive.send_microphone.__get__(live)
            audio = asyncio.Queue()
            await live.use_web_microphone(audio)
            audio.put_nowait(SPEECH)
            for _ in range(50):
                if connection.session.input_audio.append.await_count:
                    break
                await asyncio.sleep(0.01)
            connection.session.input_audio.append.assert_awaited_with(audio=delta(SPEECH))
            # The desk's privacy mute doesn't silence a page that's talking.
            await live.set_microphone_muted(True)
            audio.put_nowait(SPEECH)
            for _ in range(50):
                if connection.session.input_audio.append.await_count >= 2:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(connection.session.input_audio.append.await_count, 2)
            await live.set_microphone_muted(False)
            live.send_microphone = stand_in  # back to the harness's stand-in for the desk
            await live.use_web_microphone(None)
            self.assertIsNone(live.web_microphone)
            # The page's queue was told to end.
            self.assertIn(None, list(audio._queue))


class WebTalkAppTests(unittest.IsolatedAsyncioTestCase):
    async def test_talk_from_standby_wakes_her_and_hanging_up_puts_her_back(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.standby = True
            listener = app.web_voice.listen("iPhone")
            with patch.object(app, "action_reconnect") as reconnect, \
                    patch.object(app.live, "use_web_microphone", AsyncMock()) as use_mic, \
                    patch.object(app, "request_standby") as standby:
                app.on_web_talk(IPHONE, listener, True)
                reconnect.assert_called_once_with()
                self.assertIs(app.web_voice.talker, listener)
                self.assertTrue(app.web_talk_woke)
                await pilot.pause()
                use_mic.assert_awaited_with(listener.mic)
                self.assertEqual(await app.web_voice.next_chunk(listener, 1), {"type": "talking"})
                # Now Active; hanging up sends her back to standby.
                app.standby = False
                app.live.ready = True
                app.on_web_talk(IPHONE, listener, False)
                standby.assert_called_once_with()
                await pilot.pause()
                use_mic.assert_awaited_with(None)
                self.assertFalse(app.web_talk_woke)
                app.live.ready = False

    async def test_talk_while_already_active_just_hands_back_to_the_desk(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.standby = False
            app.live.ready = True
            listener = app.web_voice.listen("iPhone")
            with patch.object(app, "action_reconnect") as reconnect, \
                    patch.object(app.live, "use_web_microphone", AsyncMock()), \
                    patch.object(app, "request_standby") as standby:
                app.on_web_talk(IPHONE, listener, True)
                app.on_web_talk(IPHONE, listener, False)
                reconnect.assert_not_called()
                standby.assert_not_called()
            app.live.ready = False

    async def test_another_device_takes_over_and_standby_ends_the_call(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.live.ready = True
            iphone, ipad = app.web_voice.listen("iPhone"), app.web_voice.listen("iPad")
            with patch.object(app.live, "use_web_microphone", AsyncMock()):
                app.on_web_talk(IPHONE, iphone, True)
                app.on_web_talk(IPHONE, ipad, True)
                messages = [await app.web_voice.next_chunk(iphone, 1) for _ in range(2)]
                self.assertEqual(messages, [{"type": "talking"}, {"type": "talk_ended", "reason": "taken over"}])
                self.assertIs(app.web_voice.talker, ipad)
                app.set_connection_state("standby")
                self.assertIsNone(app.web_voice.talker)
                self.assertEqual(await app.web_voice.next_chunk(ipad, 1), {"type": "talking"})
                self.assertEqual(await app.web_voice.next_chunk(ipad, 1), {"type": "talk_ended", "reason": "standby"})
            app.live.ready = False
