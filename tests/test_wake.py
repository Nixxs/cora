import asyncio
import base64
from pathlib import Path
import subprocess
import sys
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import soxr
from textual import events
from textual.widgets import Button

from live import CoraLive
from test_keyboard_persistence import running_app
from test_reconnect import restarting_app
from ui.composer import ChatComposer
from wake.detector import DEFAULT_MODEL, WakeDetector, match_wake_phrase
from wake.listener import WakeListener


class WakeDetectorTests(unittest.TestCase):
    def make_detector(self, segments=()):
        model = MagicMock()
        model.transcribe.return_value = (iter(segments), SimpleNamespace())
        with patch("wake.detector.WhisperModel", return_value=model) as factory, \
                patch.object(Path, "is_file", return_value=True):
            detector = WakeDetector()
        factory.assert_called_once_with(
            str(DEFAULT_MODEL), device="cpu", compute_type="int8", cpu_threads=2,
            local_files_only=True,
        )
        return detector

    def test_snapshot_resampling_preserves_audio_and_model_sample_rate(self):
        detector = self.make_detector()
        pcm = (np.sin(np.arange(24000) * (2 * np.pi * 440 / 24000)) * 8000).astype("<i2")
        self.assertFalse(detector.process(pcm.tobytes()))
        actual = detector.model.transcribe.call_args.args[0]
        expected = soxr.resample(pcm.astype(np.float32) / 32768.0, 24000, 16000)
        self.assertEqual(actual.size, 16000)
        self.assertEqual(actual.dtype, np.float32)
        np.testing.assert_allclose(actual, expected, atol=1e-6)
        self.assertTrue(detector.model.transcribe.call_args.kwargs["vad_filter"])
        self.assertFalse(detector.model.transcribe.call_args.kwargs["condition_on_previous_text"])

    def test_missing_model_fails_before_constructing_native_detector(self):
        with patch("wake.detector.WhisperModel") as model:
            with self.assertRaises(FileNotFoundError):
                WakeDetector(Path("missing-wake-test-model"))
            model.assert_not_called()

    def test_whole_word_matching_accepts_aliases_but_not_substrings(self):
        for text, expected in (
            ("Hey Cora, what time is it?", "hey cora"),
            ("HEY KORA!", "hey kora"), ("Hey, Korra.", "hey korra"),
            ("they cora", ""), ("Hey Coraline", ""), ("Cora", ""), ("", ""),
        ):
            with self.subTest(text=text):
                self.assertEqual(match_wake_phrase(text), expected)

    def test_silence_and_short_chunks_do_not_run_whisper(self):
        detector = self.make_detector()
        self.assertFalse(detector.process(bytes(24000 * 2 * 3)))
        self.assertFalse(detector.process(b"\x10\x20" * 480))
        detector.model.transcribe.assert_not_called()

    def test_transcript_is_approximate_and_low_confidence_does_not_wake(self):
        for probability, logprob, expected in ((0.1, -0.2, True), (0.9, -0.2, False), (0.1, -2, False)):
            with self.subTest(probability=probability, logprob=logprob):
                detector = self.make_detector([SimpleNamespace(
                    text=" Hey Korra, hello. ", no_speech_prob=probability, avg_logprob=logprob,
                )])
                self.assertEqual(detector.process(b"\x10\x20" * 24000), expected)
                self.assertEqual(detector.last_transcript, "Hey Korra, hello.")
                self.assertEqual(detector.last_match, "hey korra" if expected else "")

    @unittest.skipUnless(DEFAULT_MODEL.is_dir(), "Manually downloaded wake model required")
    def test_actual_local_model_loads_and_rejects_silence(self):
        detector = WakeDetector()
        self.assertFalse(detector.process(bytes(24000 * 2 * 3)))

    @unittest.skipUnless(DEFAULT_MODEL.is_dir(), "Manually downloaded wake model required")
    def test_real_detector_does_not_write_over_terminal_ui(self):
        # Python redirect_stderr does not catch native C++ writes. Isolate and
        # capture the actual OS handles, without touching the app's handles.
        result = subprocess.run(
            [sys.executable, "-c", (
                "from wake.detector import WakeDetector; detector = WakeDetector(); "
                "import numpy as np; "
                "pcm=(np.sin(np.arange(72000)*2*np.pi*440/24000)*5000).astype('<i2').tobytes(); "
                "assert not detector.process(pcm)"
            )],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")


class WakeListenerTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_continues_during_slow_whisper_and_keeps_opening_speech(self):
        incoming = asyncio.Queue()
        decoding, woke = asyncio.Event(), asyncio.Event()
        release = Event()
        loop = asyncio.get_running_loop()

        async def chunks():
            while True:
                yield await incoming.get()
                incoming.task_done()

        def process(pcm):
            loop.call_soon_threadsafe(decoding.set)
            release.wait(3)
            return True

        def wake(listener):
            woke.set()
            return True

        listener = WakeListener(wake, MagicMock(), source=chunks, check_chunks=1,
                                detector_factory=lambda: MagicMock(process=MagicMock(side_effect=process)))
        listener.start()
        try:
            incoming.put_nowait(b"\x01\x00")
            await asyncio.wait_for(decoding.wait(), 2)
            for index in range(2, 5):
                incoming.put_nowait(bytes([index, 0]))
            await asyncio.wait_for(incoming.join(), 2)
            self.assertEqual(listener.chunk_count, 4)
            self.assertEqual(len(listener.pre_roll), 4)
            release.set()
            await asyncio.wait_for(woke.wait(), 2)
            self.assertEqual([listener.audio.get_nowait() for _ in range(4)], [
                bytes([index, 0]) for index in range(1, 5)
            ])
        finally:
            release.set()
            await listener.stop()

    async def test_stop_during_whisper_closes_capture_and_suppresses_late_results(self):
        decoding, closed = asyncio.Event(), asyncio.Event()
        release = Event()
        loop = asyncio.get_running_loop()

        async def chunks():
            try:
                yield b"\x01\x00"
                await asyncio.Event().wait()
            finally:
                closed.set()

        def process(pcm):
            loop.call_soon_threadsafe(decoding.set)
            release.wait(3)
            return True

        wake, debug = MagicMock(), MagicMock()
        detector = SimpleNamespace(process=process, last_transcript="Hey Cora", last_match="hey cora")
        listener = WakeListener(wake, MagicMock(), source=chunks, check_chunks=1,
                                detector_factory=lambda: detector, on_debug=debug)
        listener.start()
        try:
            await asyncio.wait_for(decoding.wait(), 2)
            await asyncio.wait_for(listener.stop(), 2)
            self.assertTrue(closed.is_set())
            self.assertTrue(listener.heartbeat_task.done())
            self.assertFalse(listener.pre_roll)
            wake.assert_not_called()
            debug.assert_not_called()
        finally:
            release.set()
            await listener.stop()

    async def test_transcript_and_heartbeat_show_progress_without_triggering_wake(self):
        reported = asyncio.Queue()

        async def chunks():
            yield b"\x10\x20"
            await asyncio.Event().wait()

        detector = SimpleNamespace(process=lambda pcm: False,
                                   last_transcript="Hey Laura", last_match="")
        wake = MagicMock()
        listener = WakeListener(wake, MagicMock(), source=chunks, check_chunks=1,
                                detector_factory=lambda: detector, on_debug=reported.put_nowait)
        listener.start()
        try:
            transcript = await asyncio.wait_for(reported.get(), 2)
            self.assertIn("Whisper heard (approximate): 'Hey Laura'", transcript)
            listener.report_health()
            health = await asyncio.wait_for(reported.get(), 2)
            self.assertIn("chunks=1, checks=1", health)
            self.assertIn("audio level=", health)
            self.assertIn("last audio", health)
            wake.assert_not_called()
        finally:
            await listener.stop()

    async def test_local_capture_handoff_keeps_order_waits_for_ready_and_opens_only_once(self):
        incoming = asyncio.Queue()
        opened, closed = asyncio.Event(), asyncio.Event()
        woke = asyncio.Event()
        processed = asyncio.Queue()
        loop = asyncio.get_running_loop()
        detector = MagicMock()

        def process(pcm):
            loop.call_soon_threadsafe(processed.put_nowait, None)
            return detector.process.call_count == 2

        detector.process.side_effect = process

        async def chunks():
            opened.set()
            try:
                while True:
                    yield await incoming.get()
                    incoming.task_done()
            finally:
                closed.set()

        def wake(listener):
            woke.set()
            return True

        source = MagicMock(side_effect=chunks)
        listener = WakeListener(wake, MagicMock(), detector_factory=lambda: detector, source=source, check_chunks=1)
        live = CoraLive()
        sent = asyncio.Queue()

        async def append(*, audio):
            sent.put_nowait(base64.b64decode(audio))

        connection = SimpleNamespace(session=SimpleNamespace(input_audio=SimpleNamespace(
            append=AsyncMock(side_effect=append),
        )))
        listener.start()
        try:
            await asyncio.wait_for(opened.wait(), 2)
            incoming.put_nowait(b"\x01\x00")
            await asyncio.wait_for(processed.get(), 2)
            incoming.put_nowait(b"\x02\x00")
            await asyncio.wait_for(woke.wait(), 2)
            # Standby has no transport audio sends, even after detection.
            connection.session.input_audio.append.assert_not_called()
            live.opening_microphone = listener
            live.microphone_task = asyncio.create_task(live.send_microphone(connection, MagicMock()))
            incoming.put_nowait(b"\x03\x00")
            await asyncio.wait_for(incoming.join(), 2)
            connection.session.input_audio.append.assert_not_called()
            live.started.set()
            for expected in (b"\x01\x00", b"\x02\x00", b"\x03\x00"):
                self.assertEqual(await asyncio.wait_for(sent.get(), 2), expected)
            incoming.put_nowait(b"\x04\x00")
            self.assertEqual(await asyncio.wait_for(sent.get(), 2), b"\x04\x00")
            source.assert_called_once()
            self.assertEqual(detector.process.call_count, 2)
        finally:
            await live.stop_microphone()
            await listener.stop()
        self.assertTrue(closed.is_set())
        self.assertFalse(listener.pre_roll)
        self.assertIsNone(live.opening_microphone)

    async def test_privacy_mute_during_reconnect_discards_buffer_and_closes_device(self):
        woke, closed = asyncio.Event(), asyncio.Event()

        async def chunks():
            try:
                yield b"\x01\x00"
                await asyncio.Event().wait()
            finally:
                closed.set()

        def wake(listener):
            woke.set()
            return True

        listener = WakeListener(wake, MagicMock(), detector_factory=lambda: MagicMock(
            process=MagicMock(return_value=True)), source=chunks, check_chunks=1)
        live = CoraLive()
        connection = MagicMock()
        waiting = asyncio.Event()

        async def wait_for_ready():
            waiting.set()
            await asyncio.Event().wait()

        live.started.wait = AsyncMock(side_effect=wait_for_ready)
        listener.start()
        try:
            await asyncio.wait_for(woke.wait(), 2)
            live.opening_microphone = listener
            live.microphone_task = asyncio.create_task(live.send_microphone(connection, MagicMock()))
            await asyncio.wait_for(waiting.wait(), 2)
            await live.set_microphone_muted(True)
            live.started.set()
            self.assertTrue(closed.is_set())
            self.assertIsNone(live.opening_microphone)
            connection.session.input_audio.append.assert_not_called()
            self.assertEqual(listener.audio.get_nowait(), None)
            self.assertTrue(listener.audio.empty())
        finally:
            await live.stop_microphone()
            await listener.stop()

    async def test_model_failure_never_opens_microphone(self):
        source = MagicMock(side_effect=AssertionError("Capture forbidden"))
        status = MagicMock()
        listener = WakeListener(MagicMock(), status, source=source,
                                detector_factory=MagicMock(side_effect=FileNotFoundError("private path")))
        listener.start()
        await asyncio.wait_for(listener.task, 2)
        source.assert_not_called()
        self.assertIsInstance(listener.error, FileNotFoundError)
        self.assertNotIn("private path", status.call_args.args[1])

    async def test_overflow_discards_audio_instead_of_truncating_request(self):
        incoming = asyncio.Queue()
        woke = asyncio.Event()

        async def chunks():
            while True:
                yield await incoming.get()

        def wake(listener):
            woke.set()
            return True

        listener = WakeListener(wake, MagicMock(), source=chunks, check_chunks=1,
                                detector_factory=lambda: MagicMock(process=MagicMock(return_value=True)))
        listener.audio = asyncio.Queue(maxsize=2)
        listener.start()
        incoming.put_nowait(b"\x01\x00")
        await asyncio.wait_for(woke.wait(), 2)
        for _ in range(3):
            incoming.put_nowait(b"\x01\x00")
        await asyncio.wait_for(listener.task, 2)
        self.assertIsInstance(listener.error, asyncio.QueueFull)
        self.assertEqual(listener.audio.get_nowait(), None)
        self.assertTrue(listener.audio.empty())
        self.assertFalse(listener.pre_roll)

    async def test_rejected_trigger_closes_capture_and_discards_audio(self):
        closed = asyncio.Event()

        async def chunks():
            try:
                yield b"\x01\x00"
                await asyncio.Event().wait()
            finally:
                closed.set()

        listener = WakeListener(lambda listener: False, MagicMock(), source=chunks, check_chunks=1,
                                detector_factory=lambda: MagicMock(process=MagicMock(return_value=True)))
        listener.start()
        await asyncio.wait_for(listener.task, 2)
        self.assertTrue(closed.is_set())
        self.assertEqual(listener.audio.get_nowait(), None)

    async def test_reconnection_buffer_times_out_without_a_live_consumer(self):
        async def chunks():
            yield b"\x01\x00"
            await asyncio.Event().wait()

        listener = WakeListener(lambda listener: True, MagicMock(), source=chunks, check_chunks=1,
                                detector_factory=lambda: MagicMock(process=MagicMock(return_value=True)))
        listener.reconnect_timeout = 0
        listener.start()
        await asyncio.wait_for(listener.task, 2)
        self.assertIsInstance(listener.error, TimeoutError)
        self.assertEqual(listener.audio.get_nowait(), None)


def fake_listener():
    return SimpleNamespace(start=MagicMock(), stop=AsyncMock(), error=None)


class WakeAppTests(unittest.IsolatedAsyncioTestCase):
    async def test_diagnostics_are_activity_only_and_privacy_mute_suppresses_them(self):
        async with running_app() as (app, pilot):
            app.wake.enabled = True
            app.handle_wake_diagnostic("Whisper heard (approximate): 'Hey Kora'")
            self.assertIn("Hey Kora", app.activity.entries[-1])
            self.assertFalse(app.conversation.log_entries)
            app.persistence.save_chat_message.assert_not_called()
            count = len(app.activity.entries)
            app.live.microphone_muted = True
            app.handle_wake_diagnostic("Do not record this")
            self.assertEqual(len(app.activity.entries), count)

    async def test_debug_logging_can_be_disabled_without_disabling_wake(self):
        listener = fake_listener()
        with patch.dict("os.environ", {"CORA_WAKE_DEBUG": "0"}), \
                patch("wake.controller.WakeListener", return_value=listener) as factory:
            async with running_app() as (app, pilot):
                app.wake.enabled = True
                app.standby = True
                app.start_wake_listener()
                self.assertIsNone(factory.call_args.kwargs["on_debug"])
                listener.start.assert_called_once()

    async def test_standby_listener_wakes_unfocused_app_without_a_click(self):
        incoming = asyncio.Queue()
        ready = asyncio.Event()
        detector = MagicMock(process=MagicMock(return_value=True))

        async def chunks():
            while True:
                yield await incoming.get()

        def create_listener(on_wake, on_status, **kwargs):
            def status(working, message):
                on_status(working, message)
                if working:
                    ready.set()

            listener = WakeListener(on_wake, status, detector_factory=lambda: detector,
                                    source=chunks, check_chunks=1, **kwargs)
            return listener

        with patch("wake.controller.WakeListener", side_effect=create_listener):
            async with restarting_app() as (app, pilot, connections, speakers, started):
                try:
                    app.wake.enabled = True
                    # Losing terminal focus must not gate standby capture or wake.
                    app.post_message(events.AppBlur())
                    await pilot.pause()
                    self.assertFalse(app.app_focus)
                    app.action_standby()
                    await asyncio.wait_for(app.reconnect_worker.wait(), 3)
                    await asyncio.wait_for(ready.wait(), 3)
                    self.assertTrue(app.standby)
                    incoming.put_nowait(bytes(960))
                    await asyncio.wait_for(started.get(), 3)
                    self.assertTrue(app.live.ready)
                    self.assertFalse(app.standby)
                    self.assertFalse(app.app_focus)
                    self.assertEqual(len(connections), 2)
                    detector.process.assert_called_once()
                finally:
                    await app.stop_wake_listener()

    async def test_opt_in_is_separate_from_privacy_mute_and_standby_only(self):
        listener = fake_listener()
        with patch.dict("os.environ", {"CORA_WAKE_ENABLED": "0"}), \
                patch("wake.controller.WakeListener", return_value=listener) as factory:
            async with running_app() as (app, pilot):
                self.assertFalse(app.wake.enabled)
                await pilot.click("#wake-status")
                self.assertTrue(app.wake.enabled)
                self.assertFalse(app.live.microphone_muted)
                factory.assert_not_called()
                app.standby = True
                app.start_wake_listener()
                listener.start.assert_called_once()
                app.handle_wake_status(True, "Local ready")
                button = app.query_one("#wake-status", Button)
                self.assertTrue(button.has_class("wake-listening"))
                await pilot.click("#mic-status")
                self.assertTrue(app.live.microphone_muted)
                self.assertTrue(app.wake.enabled)
                listener.stop.assert_awaited_once()
                self.assertFalse(app.wake.listening)
                self.assertIn("Privacy muted", button.tooltip)
                self.assertFalse(app.handle_wake(listener))

    async def test_wake_reconnects_once_preserves_draft_and_does_not_replay_requests(self):
        listener = fake_listener()
        with patch("wake.controller.WakeListener", return_value=listener):
            async with restarting_app() as (app, pilot, connections, speakers, started):
                app.wake.enabled = True
                composer = app.query_one("#chat-input", ChatComposer)
                composer.load_text("Unsent draft")
                app.live.set_speaker_muted(True)
                app.action_standby()
                await asyncio.wait_for(app.reconnect_worker.wait(), 3)
                self.assertTrue(app.standby)
                listener.start.assert_called_once()
                self.assertIsNone(app.live.connection)
                self.assertTrue(app.handle_wake(listener))
                self.assertFalse(app.handle_wake(listener))
                await asyncio.wait_for(started.get(), 3)
                self.assertEqual(len(connections), 2)
                self.assertFalse(app.standby)
                self.assertTrue(app.live.speaker_muted)
                self.assertEqual(composer.text, "Unsent draft")
                app.persistence.save_message.assert_not_called()
                connections[1].response.create.assert_not_called()
                self.assertIsNotNone(app.wake.response_deadline)

    async def test_wake_phrase_alone_times_out_but_request_and_typing_cancel_short_timeout(self):
        for activity in ("wake only", "request", "typing"):
            with self.subTest(activity=activity):
                async with running_app() as (app, pilot):
                    app.live.ready = True
                    app.wake.response_deadline = 0
                    for delta in ("Hey", " Cora", "."):
                        app.handle_live_event(SimpleNamespace(type="session.input_transcript.delta", delta=delta))
                    self.assertEqual(app.wake.response_deadline, 0)
                    if activity == "request":
                        app.handle_live_event(SimpleNamespace(type="session.input_transcript.delta", delta=" What time is it?"))
                    elif activity == "typing":
                        app.record_idle_activity(user_activity=True)
                    app.last_live_activity = 0
                    with patch.object(app, "action_standby") as standby:
                        app.check_wake_timeout()
                    if activity == "wake only":
                        standby.assert_called_once()
                    else:
                        standby.assert_not_called()
                        self.assertIsNone(app.wake.response_deadline)

    async def test_timeout_does_not_close_during_backend_work_or_playback(self):
        async with running_app() as (app, pilot):
            app.live.ready = True
            app.wake.response_deadline = 0
            app.backend_work.pending.add("work")
            with patch.object(app, "action_standby") as standby:
                app.check_wake_timeout()
                standby.assert_not_called()
                app.backend_work.clear()
                app.check_wake_timeout()
                standby.assert_called_once()

    async def test_wake_alias_only_does_not_count_as_a_request(self):
        async with running_app() as (app, pilot):
            for name in ("Kora", "Korra"):
                app.wake.response_deadline = 0
                app.wake.transcript = ""
                app.handle_live_event(SimpleNamespace(type="session.input_transcript.delta", delta=f"Hey {name}."))
                self.assertEqual(app.wake.response_deadline, 0)

    async def test_privacy_mute_is_available_while_wake_reconnect_is_pending(self):
        listener = fake_listener()
        with patch("wake.controller.WakeListener", return_value=listener):
            async with restarting_app() as (app, pilot, connections, speakers, started):
                app.wake.enabled = True
                app.action_standby()
                await asyncio.wait_for(app.reconnect_worker.wait(), 3)
                entered, release = asyncio.Event(), asyncio.Event()
                original_run = app.live.run

                async def delayed_run(*args):
                    entered.set()
                    await release.wait()
                    await original_run(*args)

                with patch.object(app.live, "run", side_effect=delayed_run):
                    try:
                        self.assertTrue(app.handle_wake(listener))
                        await asyncio.wait_for(entered.wait(), 3)
                        self.assertFalse(app.query_one("#mic-status", Button).disabled)
                        await pilot.click("#mic-status")
                        self.assertTrue(app.live.microphone_muted)
                        self.assertIsNone(app.live.opening_microphone)
                        self.assertIsNone(app.wake.listener)
                        listener.stop.assert_awaited()
                        release.set()
                        await asyncio.wait_for(started.get(), 3)
                        self.assertTrue(app.live.microphone_muted)
                    finally:
                        release.set()

    async def test_wake_connection_failure_discards_audio_without_retry(self):
        listener = fake_listener()
        async with running_app() as (app, pilot):
            app.wake.enabled = True
            app.wake.listener = listener
            app.live.opening_microphone = listener
            app.wake.reconnecting = True
            worker = app.abort_wake_connection()
            await asyncio.wait_for(worker.wait(), 3)
            listener.stop.assert_awaited_once()
            self.assertTrue(app.standby)
            self.assertTrue(app.wake.failed)
            self.assertIsNone(app.live.opening_microphone)
            with patch("wake.controller.WakeListener") as factory:
                app.start_wake_listener()
                factory.assert_not_called()

    async def test_quit_stops_local_listener_and_disables_wake_control(self):
        listener = fake_listener()
        async with running_app() as (app, pilot):
            app.wake.listener = listener
            with patch.object(app, "exit"):
                await app.action_disconnect_and_quit()
            listener.stop.assert_awaited_once()
            self.assertTrue(app.query_one("#wake-status", Button).disabled)
            self.assertIsNone(app.wake.listener)


if __name__ == "__main__":
    unittest.main()