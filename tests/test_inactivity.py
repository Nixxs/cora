import asyncio
import base64
from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from app import read_idle_timeout
from devices.speaker import Speaker
from test_keyboard_persistence import running_app
from test_reconnect import backend_event, restarting_app
from ui.composer import ChatComposer


@asynccontextmanager
async def idle_app():
    with patch.dict("os.environ", {"CORA_IDLE_TIMEOUT_SECONDS": "300", "CORA_STANDBY_FAREWELL": "0"}):
        async with running_app() as (app, pilot):
            # Tests drive the monotonic clock/check explicitly, without real sleeps.
            app.idle_standby_timer.stop()
            app.live.ready = True
            app.connection_worker = None
            with patch("app.monotonic", return_value=1000) as clock:
                yield app, pilot, clock


class IdleConfigurationTests(unittest.TestCase):
    def test_default_is_five_minutes(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(read_idle_timeout(), 300.0)

    def test_seconds_and_zero_disable(self):
        for value, expected in (("0", 0), ("60", 60), ("0.5", 0.5)):
            with self.subTest(value=value), patch.dict("os.environ", {"CORA_IDLE_TIMEOUT_SECONDS": value}):
                self.assertEqual(read_idle_timeout(), expected)

    def test_invalid_negative_and_nonfinite_values_rejected(self):
        for value in ("", "invalid", "-1", "nan", "inf", "-inf", "1e309"):
            with self.subTest(value=value), patch.dict("os.environ", {"CORA_IDLE_TIMEOUT_SECONDS": value}):
                with self.assertRaises(ValueError):
                    read_idle_timeout()


class InactivityTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_or_invalid_configuration_creates_no_timer(self):
        for value in ("0", "invalid"):
            with self.subTest(value=value), patch.dict("os.environ", {"CORA_IDLE_TIMEOUT_SECONDS": value}):
                async with running_app() as (app, pilot):
                    self.assertEqual(app.idle_timeout_seconds, 0)
                    self.assertIsNone(app.idle_standby_timer)
                    if value == "invalid":
                        self.assertIn("automatic standby disabled", "\n".join(app.activity.entries))

    @patch("app.CoraApp.notify")
    @patch("app.CoraApp.reconnect_live", return_value=None)
    async def test_deadline_reuses_standby_once_without_notifications(self, transition, notify):
        async with idle_app() as (app, pilot, clock):
            clock.return_value = 399.9
            app.last_idle_activity = 100
            app.check_inactivity()
            transition.assert_not_called()
            clock.return_value = 400
            app.check_inactivity()
            app.check_inactivity()
            transition.assert_called_once_with(restart=False)
            notify.assert_not_called()
            self.assertIn("Automatic standby", app.activity.panel.title)

    @patch("app.CoraApp.action_standby")
    async def test_inactive_states_do_not_request_standby(self, standby):
        async with idle_app() as (app, pilot, clock):
            app.last_idle_activity = 0
            for attribute, value in (("closing", True), ("reconnecting", True), ("standby", True),
                                     ("idle_timeout_seconds", 0)):
                with self.subTest(attribute=attribute):
                    previous = getattr(app, attribute)
                    setattr(app, attribute, value)
                    app.check_inactivity()
                    setattr(app, attribute, previous)
            app.live.ready = False
            app.connection_worker = MagicMock(is_finished=False)
            app.check_inactivity()
            standby.assert_not_called()

    @patch("app.CoraApp.notify")
    @patch("app.CoraApp.action_standby")
    async def test_known_work_blocks_silently_and_gets_fresh_full_grace(self, standby, notify):
        async with idle_app() as (app, pilot, clock):
            cases = (
                ("backend", lambda: app.pending_backend_work.add("pending")),
                ("acknowledgement", lambda: app.live.pending_text_requests.add("typed")),
                ("submission", lambda: setattr(app, "keyboard_worker", MagicMock(is_finished=False))),
                ("playback", lambda: setattr(app.live, "speaker", MagicMock(is_playback_busy=lambda: True))),
                ("speech", lambda: setattr(app, "last_live_activity", 1000)),
            )
            for name, arrange in cases:
                with self.subTest(name=name):
                    clock.return_value = 1000
                    app.last_idle_activity = 0
                    app.idle_was_busy = False
                    arrange()
                    app.check_inactivity()
                    standby.assert_not_called()
                    app.pending_backend_work.clear()
                    app.live.pending_text_requests.clear()
                    app.keyboard_worker = None
                    app.live.speaker = None
                    app.last_live_activity = 0
                    clock.return_value = 1001
                    app.check_inactivity()
                    self.assertEqual(app.last_idle_activity, 1001)
                    clock.return_value = 1300.9
                    app.check_inactivity()
                    standby.assert_not_called()
                    clock.return_value = 1301
                    app.check_inactivity()
                    standby.assert_called_once()
                    standby.reset_mock()
            notify.assert_not_called()

    @patch("app.CoraApp.action_standby")
    async def test_tool_worker_blocks_but_background_memory_worker_does_not(self, standby):
        async with idle_app() as (app, pilot, clock):
            release = asyncio.Event()
            tool = app.run_worker(release.wait(), group="tool-actions")
            app.last_idle_activity = 0
            app.check_inactivity()
            standby.assert_not_called()
            release.set()
            await asyncio.wait_for(tool.wait(), 3)
            clock.return_value = 1001
            app.check_inactivity()
            self.assertEqual(app.last_idle_activity, 1001)
            memory_release = asyncio.Event()
            memory = app.run_worker(memory_release.wait(), group="memory-summary")
            try:
                clock.return_value = 1301
                app.check_inactivity()
                standby.assert_called_once()
            finally:
                memory_release.set()
                await asyncio.wait_for(memory.wait(), 3)

    async def test_typing_deleting_pasting_and_cursor_movement_reset_clock(self):
        async with idle_app() as (app, pilot, clock):
            composer = app.query_one("#chat-input", ChatComposer)
            await pilot.press("a", "b")
            self.assertEqual(app.last_idle_activity, 1000)
            clock.return_value = 1010
            await pilot.press("backspace")
            self.assertEqual(app.last_idle_activity, 1010)
            clock.return_value = 1020
            with patch("ui.composer.pyperclip.paste", return_value="pasted text"):
                await pilot.press("ctrl+v")
            self.assertIn("pasted text", composer.text)
            self.assertEqual(app.last_idle_activity, 1020)
            clock.return_value = 1030
            await pilot.press("left")
            self.assertEqual(app.last_idle_activity, 1030)

    async def test_submission_and_worker_completion_between_polls_reset_clock(self):
        async with idle_app() as (app, pilot, clock):
            composer = app.query_one("#chat-input", ChatComposer)
            composer.load_text("Question")
            await pilot.pause()
            clock.return_value = 1010
            entered, release = asyncio.Event(), asyncio.Event()

            async def send(message):
                entered.set()
                await release.wait()

            app.live.send_text.side_effect = send
            await pilot.press("enter")
            await asyncio.wait_for(entered.wait(), 3)
            self.assertEqual(app.last_idle_activity, 1010)
            clock.return_value = 1020
            release.set()
            await asyncio.wait_for(app.keyboard_worker.wait(), 3)
            self.assertEqual(app.last_idle_activity, 1020)

    @patch("app.CoraApp.action_standby")
    async def test_backend_finishing_between_polls_gets_full_grace(self, standby):
        async with idle_app() as (app, pilot, clock):
            for terminal in ("response.completed", "response.failed", "response.incomplete"):
                with self.subTest(terminal=terminal):
                    clock.return_value = 1000
                    app.last_idle_activity = 0
                    app.handle_live_event(backend_event("response.created"))
                    app.handle_live_event(backend_event(terminal))
                    self.assertEqual(app.last_idle_activity, 1000)
                    self.assertFalse(app.pending_backend_work)
                    clock.return_value = 1299.9
                    app.check_inactivity()
                    standby.assert_not_called()

    async def test_transcripts_and_audible_audio_reset_but_silence_does_not(self):
        async with idle_app() as (app, pilot, clock):
            for kind in ("session.input_transcript.delta", "session.output_transcript.delta"):
                app.last_idle_activity = 0
                app.handle_live_event(SimpleNamespace(type=kind, delta="Speech"))
                self.assertEqual(app.last_idle_activity, 1000)
            speaker = Speaker()
            app.live.speaker = speaker
            with patch("devices.speaker.sd.RawOutputStream"):
                try:
                    silence = base64.b64encode(bytes(960)).decode("ascii")
                    speaker.play(silence)
                    clock.return_value = 1010
                    app.handle_live_event(SimpleNamespace(type="session.output_audio.delta", delta=silence))
                    self.assertEqual(app.last_idle_activity, 1000)
                    speech = base64.b64encode(b"\x00\x20" * 480).decode("ascii")
                    speaker.play(speech)
                    app.handle_live_event(SimpleNamespace(type="session.output_audio.delta", delta=speech))
                    self.assertEqual(app.last_idle_activity, 1010)
                    app.live.speaker_muted = True
                    clock.return_value = 1020
                    app.handle_live_event(SimpleNamespace(type="session.output_audio.delta", delta=speech))
                    self.assertEqual(app.last_idle_activity, 1010)
                finally:
                    speaker.close()
                    app.live.speaker = None

    async def test_audio_control_buttons_reset_clock(self):
        async with idle_app() as (app, pilot, clock):
            await pilot.click("#mic-status")
            self.assertEqual(app.last_idle_activity, 1000)
            clock.return_value = 1010
            await pilot.click("#speaker-status")
            self.assertEqual(app.last_idle_activity, 1010)

    @patch("app.CoraApp.exit")
    async def test_timer_stopped_on_quit_and_unmount(self, exit_app):
        async with idle_app() as (app, pilot, clock):
            with patch.object(app.idle_standby_timer, "stop") as stop:
                await app.action_disconnect_and_quit()
                stop.assert_called_once()
                await app.on_unmount()
                self.assertEqual(stop.call_count, 2)

    @patch("app.monotonic", return_value=1000)
    async def test_automatic_close_preserves_state_and_manual_resume_gets_full_grace(self, clock):
        with patch.dict("os.environ", {"CORA_IDLE_TIMEOUT_SECONDS": "300"}):
            async with restarting_app() as (app, pilot, connections, speakers, started):
                app.idle_standby_timer.stop()
                conversation_id = app.persistence.conversation_id
                await app.live.set_microphone_muted(True)
                app.live.set_speaker_muted(True)
                app.conversation.write_log("You: Keep the blue notebook")
                composer = app.query_one("#chat-input", ChatComposer)
                composer.load_text("Unsent draft")
                await pilot.pause()
                app.last_idle_activity = 700
                app.check_inactivity()
                await asyncio.wait_for(app.reconnect_worker.wait(), 3)
                await pilot.pause()
                self.assertTrue(app.standby)
                self.assertIsNone(app.live.connection)
                self.assertEqual(composer.text, "Unsent draft")
                self.assertEqual(app.persistence.conversation_id, conversation_id)
                self.assertTrue(app.live.microphone_muted)
                self.assertTrue(app.live.speaker_muted)
                connections[0].session.close.assert_awaited_once()
                history = app.conversation.get_reconnect_history()
                clock.return_value = 2000
                await pilot.press("ctrl+r")
                await asyncio.wait_for(app.reconnect_worker.wait(), 3)
                await asyncio.wait_for(started.get(), 5)
                await pilot.pause()
                self.assertFalse(app.standby)
                self.assertTrue(app.live.ready)
                self.assertEqual(app.last_idle_activity, 2000)
                self.assertEqual(connections[1].session.start.call_args.kwargs["session"]["input"], history)
                self.assertEqual(composer.text, "Unsent draft")
                self.assertTrue(app.live.microphone_muted)
                self.assertTrue(app.live.speaker_muted)
                connections[1].response.create.assert_not_called()
                clock.return_value = 2299.9
                app.check_inactivity()
                connections[1].session.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()