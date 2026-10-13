import asyncio
import base64
from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, call, patch
from uuid import uuid4

from textual.widgets import Button

from app import CoraApp
from devices.speaker import Speaker
from live import CoraLive
from ui.composer import ChatComposer


@asynccontextmanager
async def live_transport(live):
    events = asyncio.Queue()
    delivered = asyncio.Queue()
    connection = SimpleNamespace(
        session=SimpleNamespace(
            start=AsyncMock(), thinking=SimpleNamespace(append=AsyncMock()),
            input_audio=SimpleNamespace(append=AsyncMock()), close=AsyncMock(),
        ),
        response=SimpleNamespace(item=SimpleNamespace(create=AsyncMock()), create=AsyncMock()),
    )

    class Connection:
        session = connection.session
        response = connection.response

        def __aiter__(self):
            async def receive():
                while True:
                    yield await events.get()
            return receive()

    transport = Connection()

    @asynccontextmanager
    async def connect():
        yield transport

    @asynccontextmanager
    async def client():
        yield SimpleNamespace(live=SimpleNamespace(connect=connect))

    async def microphone(*args):
        await asyncio.Event().wait()

    async def emit(event_type, **kwargs):
        await events.put(SimpleNamespace(type=event_type, **kwargs))
        return await asyncio.wait_for(delivered.get(), 2)

    speaker = MagicMock()
    speaker.play.return_value = True
    with patch("live.AsyncOpenAI", client), patch("live.Speaker", return_value=speaker), \
            patch.object(live, "send_microphone", side_effect=microphone):
        reader = asyncio.create_task(live.run(delivered.put_nowait, MagicMock()))
        try:
            await emit("session.started")
            yield transport, speaker, emit
        finally:
            await emit("session.closed")
            await asyncio.wait_for(reader, 2)


class SpeakerMuteTests(unittest.IsolatedAsyncioTestCase):
    async def test_mute_aborts_playback_drops_new_audio_and_unmute_plays_only_new_chunks(self):
        live = CoraLive()
        async with live_transport(live) as (connection, speaker, emit):
            await emit("session.output_audio.delta", delta="AQI=")
            live.set_speaker_muted(True)
            speaker.close.assert_called_once_with()
            await emit("session.output_audio.delta", delta="AwQ=")
            self.assertEqual(speaker.play.call_args_list, [call("AQI=")])
            event = await emit("session.output_transcript.delta", delta="Still visible")
            self.assertEqual(event.delta, "Still visible")
            live.set_speaker_muted(False)
            await emit("session.output_audio.delta", delta="BQY=")
            self.assertEqual(speaker.play.call_args_list, [call("AQI="), call("BQY=")])
            connection.session.close.assert_not_called()
            self.assertTrue(live.ready)
        self.assertIsNone(live.speaker)
        self.assertIsNone(live.microphone_task)

    async def test_muted_before_connection_never_opens_playback_and_typing_still_works(self):
        live = CoraLive()
        live.set_speaker_muted(True)
        await live.set_microphone_muted(True)
        async with live_transport(live) as (connection, speaker, emit):
            await emit("session.output_audio.delta", delta="AQI=")
            speaker.play.assert_not_called()
            await live.send_text("Hello quietly")
            connection.session.thinking.append.assert_awaited_once()
            connection.response.item.create.assert_awaited_once_with(item={
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "Hello quietly"}],
            })
            connection.response.create.assert_awaited_once()
            self.assertTrue(connection.response.create.call_args.kwargs["event_id"].startswith("typed_response_"))


class SpeakerQueueTests(unittest.TestCase):
    def test_mute_clears_pending_and_queued_audio_and_reopens_for_fresh_playback(self):
        speaker = Speaker()
        old_stream = MagicMock()
        new_stream = MagicMock()
        with patch("devices.speaker.sd.RawOutputStream", side_effect=[old_stream, new_stream]):
            speaker.play(base64.b64encode(b"\x01\x02" * 4).decode("ascii"))
            speaker.play(base64.b64encode(b"\x03\x04" * 4).decode("ascii"))
            output = bytearray(2)
            speaker.callback(output, 1, None, None)
            self.assertTrue(speaker.pending)
            self.assertFalse(speaker.chunks.empty())
            live = CoraLive()
            live.speaker = speaker
            live.set_speaker_muted(True)
            old_stream.abort.assert_called_once_with()
            old_stream.close.assert_called_once_with()
            self.assertEqual(speaker.pending, bytearray())
            self.assertTrue(speaker.chunks.empty())
            live.set_speaker_muted(False)
            speaker.play(base64.b64encode(b"\x05\x06").decode("ascii"))
            speaker.callback(output, 1, None, None)
            self.assertEqual(output, b"\x05\x06")
            new_stream.start.assert_called_once_with()
            speaker.close()


class MicrophoneMuteTests(unittest.IsolatedAsyncioTestCase):
    async def test_mute_closes_capture_sends_only_silence_and_unmute_opens_fresh_source(self):
        live = CoraLive()
        source_started = asyncio.Queue()
        source_closed = asyncio.Queue()
        appended = asyncio.Queue()
        inputs = []

        async def chunks():
            source_id = len(inputs) + 1
            inputs.append(source_id)
            source_started.put_nowait(source_id)
            try:
                yield bytes([source_id, 0])
                await asyncio.Event().wait()
            finally:
                source_closed.put_nowait(source_id)

        async def append(*, audio):
            appended.put_nowait(base64.b64decode(audio))

        connection = SimpleNamespace(session=SimpleNamespace(
            input_audio=SimpleNamespace(append=AsyncMock(side_effect=append)), close=AsyncMock(),
        ))
        live.connection = connection
        live.ready = True
        live.on_mic_status = MagicMock()
        with patch("live.microphone_chunks", side_effect=chunks) as capture:
            live.microphone_task = asyncio.create_task(live.send_microphone(connection, live.on_mic_status))
            try:
                self.assertEqual(await asyncio.wait_for(source_started.get(), 2), 1)
                self.assertEqual(await asyncio.wait_for(appended.get(), 2), b"\x01\x00")
                await live.set_microphone_muted(True)
                self.assertEqual(await asyncio.wait_for(source_closed.get(), 2), 1)
                self.assertEqual(await asyncio.wait_for(appended.get(), 2), bytes(960))
                self.assertEqual(capture.call_count, 1)
                muted_task = live.microphone_task
                await live.set_microphone_muted(True)
                self.assertIs(live.microphone_task, muted_task)
                await live.set_microphone_muted(False)
                self.assertEqual(await asyncio.wait_for(source_started.get(), 2), 2)
                # Any silence already sent before unmute is harmless; no old capture is replayed.
                while True:
                    data = await asyncio.wait_for(appended.get(), 2)
                    if data != bytes(960):
                        break
                self.assertEqual(data, b"\x02\x00")
                connection.session.close.assert_not_called()
            finally:
                await live.stop_microphone()
            self.assertEqual(await asyncio.wait_for(source_closed.get(), 2), 2)
            self.assertIsNone(live.microphone_task)

    async def test_muted_capture_never_opens_device_and_silence_task_can_stop(self):
        live = CoraLive()
        await live.set_microphone_muted(True)
        appended = asyncio.Event()
        connection = SimpleNamespace(session=SimpleNamespace(input_audio=SimpleNamespace(
            append=AsyncMock(side_effect=lambda **kwargs: appended.set()),
        )))
        status = MagicMock()
        with patch("live.microphone_chunks", side_effect=AssertionError("Recording forbidden")) as capture:
            live.microphone_task = asyncio.create_task(live.send_microphone(connection, status))
            try:
                await asyncio.wait_for(appended.wait(), 2)
            finally:
                await live.stop_microphone()
            capture.assert_not_called()
            status.assert_called_once_with(False, "microphone task stopped")
            self.assertIsNone(live.microphone_task)


@asynccontextmanager
async def running_app(size=(90, 35)):
    with patch.object(CoraApp, "connect_to_cora", return_value=None), \
            patch.object(CoraApp, "summarize_previous_conversations", return_value=None):
        app = CoraApp()
        async with app.run_test(size=size) as pilot:
            app.persistence = MagicMock(conversation_id=uuid4())
            app.live.send_text = AsyncMock()
            app.live.close = AsyncMock()
            yield app, pilot


class AudioControlUITests(unittest.IsolatedAsyncioTestCase):
    async def test_click_controls_updates_labels_tooltips_focus_and_preserves_typing(self):
        async with running_app() as (app, pilot):
            mic = app.query_one("#mic-status", Button)
            speaker = app.query_one("#speaker-status", Button)
            composer = app.query_one("#chat-input", ChatComposer)
            app.handle_mic_status(True)
            self.assertEqual(str(mic.label), "🎙 Listening")
            await pilot.click("#mic-status")
            self.assertTrue(app.live.microphone_muted)
            self.assertEqual(str(mic.label), "🎙 Muted")
            self.assertEqual(mic.tooltip, "Unmute microphone")
            self.assertTrue(mic.has_class("audio-muted"))
            # Late transcripts and idle timers must not overwrite the mute indicator.
            with patch.object(app, "record_chat_message"):
                app.handle_live_event(SimpleNamespace(type="session.input_transcript.delta", delta="late"))
                app.reset_mic_indicator()
                self.assertEqual(str(mic.label), "🎙 Muted")
                app.conversation.flush_transcript()
            await pilot.click("#speaker-status")
            self.assertTrue(app.live.speaker_muted)
            self.assertEqual(str(speaker.label), "🔇 Muted")
            self.assertEqual(speaker.tooltip, "Unmute speaker")
            self.assertTrue(composer.has_focus)
            composer.load_text("A quiet question")
            await pilot.press("enter")
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            app.live.send_text.assert_awaited_once_with("A quiet question")
            app.persistence.save_message.assert_called_once()
            app.live.close.assert_not_called()
            await pilot.click("#mic-status")
            await pilot.click("#speaker-status")
            self.assertFalse(app.live.microphone_muted)
            self.assertFalse(app.live.speaker_muted)
            self.assertFalse(mic.has_class("audio-muted"))
            self.assertEqual(str(speaker.label), "🔊 Speaker")
            self.assertFalse(app.closing)

    async def test_controls_fit_existing_status_row_in_narrow_terminal(self):
        async with running_app(size=(55, 25)) as (app, pilot):
            await pilot.pause()
            mic = app.query_one("#mic-status", Button)
            speaker = app.query_one("#speaker-status", Button)
            self.assertEqual(mic.region.height, 1)
            self.assertEqual(speaker.region.height, 1)
            self.assertEqual(mic.region.y, speaker.region.y)
            self.assertLessEqual(speaker.region.right, app.size.width)
            self.assertTrue(await pilot.click("#speaker-status"))
            self.assertTrue(app.live.speaker_muted)

    async def test_control_failure_warns_without_disconnect_and_shutdown_disables_buttons(self):
        async with running_app() as (app, pilot):
            with patch.object(app.live, "set_speaker_muted", side_effect=RuntimeError("private details")):
                await pilot.click("#speaker-status")
            self.assertTrue(any("Audio control failed (RuntimeError)" in entry for entry in app.activity.entries))
            self.assertNotIn("private details", "\n".join(app.activity.entries))
            self.assertFalse(app.query_one("#speaker-status", Button).disabled)
            app.live.close.assert_not_called()
            with patch.object(app, "exit"):
                await app.action_disconnect_and_quit()
            self.assertTrue(app.query_one("#mic-status", Button).disabled)
            self.assertTrue(app.query_one("#speaker-status", Button).disabled)
            app.live.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()