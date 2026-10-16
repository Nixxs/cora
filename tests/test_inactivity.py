import asyncio
import base64
from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from inactivity import InactivityController, read_idle_timeout
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
    def test_default_seconds_zero_disable_and_invalid_values(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(read_idle_timeout(), 300.0)
        for value, expected in (("0", 0), ("60", 60), ("0.5", 0.5)):
            with self.subTest(value=value), patch.dict("os.environ", {"CORA_IDLE_TIMEOUT_SECONDS": value}):
                self.assertEqual(read_idle_timeout(), expected)
        for value in ("", "invalid", "-1", "nan", "inf", "-inf", "1e309"):
            with self.subTest(value=value), patch.dict("os.environ", {"CORA_IDLE_TIMEOUT_SECONDS": value}):
                with self.assertRaises(ValueError):
                    read_idle_timeout()


class InactivityControllerTests(unittest.TestCase):
    """Deterministic idle and heads-up timing without a Textual app."""

    def setUp(self):
        self.clock = MagicMock(return_value=0.0)
        self.can_standby = MagicMock(return_value=True)
        self.speaker_muted = MagicMock(return_value=False)
        self.blockers = MagicMock(return_value=[])
        self.worker = MagicMock(is_finished=False)
        self.send_alert = MagicMock(return_value=self.worker)
        self.standby = MagicMock()
        self.write = MagicMock()
        self.controller = InactivityController(
            timeout_seconds=300, farewell_enabled=False, clock=self.clock,
            can_standby=self.can_standby, speaker_muted=self.speaker_muted,
            transition_blockers=self.blockers, send_alert=self.send_alert,
            enter_standby=self.standby, write_activity=self.write,
        )

    def start_alert(self):
        self.controller.farewell_enabled = True
        self.clock.return_value = 300.0
        self.controller.check()
        return self.controller.alert

    def test_deadline_and_activity_reset_use_injected_clock(self):
        self.clock.return_value = 299.9
        self.controller.check()
        self.standby.assert_not_called()
        self.controller.record_activity()
        self.clock.return_value = 599.8
        self.controller.check()
        self.standby.assert_not_called()
        self.clock.return_value = 600.0
        self.controller.check()
        self.standby.assert_called_once_with()
        self.send_alert.assert_not_called()

    def test_work_and_first_idle_check_grant_full_grace(self):
        self.clock.return_value = 400.0
        self.blockers.return_value = ["backend response"]
        self.controller.check()
        self.assertTrue(self.controller.was_busy)
        self.blockers.return_value = []
        self.clock.return_value = 500.0
        self.controller.check()
        self.assertFalse(self.controller.was_busy)
        self.assertEqual(self.controller.last_activity, 500.0)
        self.clock.return_value = 799.9
        self.controller.check()
        self.standby.assert_not_called()
        self.clock.return_value = 800.0
        self.controller.check()
        self.standby.assert_called_once_with()

    def test_heads_up_is_single_flight_and_requires_matching_ack_and_output(self):
        alert = self.start_alert()
        self.controller.check()
        self.send_alert.assert_called_once_with(alert)
        self.assertIs(self.controller.alert_worker, self.worker)
        self.controller.acknowledge("unrelated")
        self.assertFalse(alert.accepted)
        self.controller.acknowledge(alert.event_id)
        self.assertTrue(alert.accepted)
        self.clock.return_value = 310.0
        self.controller.check()
        self.assertIsNone(alert.quiet_since)
        self.standby.assert_not_called()

    def test_output_quiet_gap_and_response_window_precede_standby(self):
        alert = self.start_alert()
        self.controller.acknowledge(alert.event_id)
        self.controller.record_output()
        self.clock.return_value = 301.9
        self.controller.check()
        self.assertIsNone(alert.quiet_since)
        self.clock.return_value = 302.0
        self.controller.check()
        self.assertEqual(alert.quiet_since, 302.0)
        self.clock.return_value = 304.9
        self.controller.check()
        self.standby.assert_not_called()
        self.clock.return_value = 305.0
        self.controller.check()
        self.assertIsNone(self.controller.alert)
        self.standby.assert_called_once_with()

    def test_playback_and_later_output_restart_quiet_window(self):
        alert = self.start_alert()
        self.controller.acknowledge(alert.event_id)
        self.controller.record_output()
        self.clock.return_value = 302.0
        self.controller.check()
        self.blockers.return_value = ["standby heads-up", "audio playback", "recent speech"]
        self.clock.return_value = 304.0
        self.controller.check()
        self.assertIsNone(alert.quiet_since)
        self.blockers.return_value = ["standby heads-up"]
        self.controller.check()
        self.controller.record_output()
        self.assertEqual(alert.last_output_at, 304.0)
        self.assertIsNone(alert.quiet_since)
        self.standby.assert_not_called()

    def test_alert_timeout_cancels_send_without_closing_and_refreshes_idle(self):
        self.start_alert()
        self.clock.return_value = 330.0
        self.controller.check()
        self.assertIsNone(self.controller.alert)
        self.worker.cancel.assert_called_once_with()
        self.assertEqual(self.controller.last_activity, 330.0)
        self.assertIn("timed out", self.write.call_args.args[0])
        self.controller.check()
        self.standby.assert_not_called()
        self.send_alert.assert_called_once()

    def test_handled_send_failure_does_not_cancel_its_own_worker(self):
        self.start_alert()
        self.controller.was_busy = True
        self.controller.cancel_alert("Send failed", cancel_send=False)
        self.assertIsNone(self.controller.alert)
        self.assertFalse(self.controller.was_busy)
        self.worker.cancel.assert_not_called()
        self.controller.cancel_alert()
        self.standby.assert_not_called()

    def test_new_work_or_inactive_lifecycle_cancels_alert(self):
        for state in ("work", "lifecycle", "disabled"):
            with self.subTest(state=state):
                self.setUp()
                self.start_alert()
                if state == "work":
                    self.blockers.return_value = ["tool execution"]
                elif state == "lifecycle":
                    self.can_standby.return_value = False
                else:
                    self.controller.timeout_seconds = 0
                self.controller.check()
                self.assertIsNone(self.controller.alert)
                self.worker.cancel.assert_called_once_with()
                self.standby.assert_not_called()

    def test_muted_speaker_bypasses_heads_up(self):
        self.speaker_muted.return_value = True
        self.start_alert()
        self.send_alert.assert_not_called()
        self.standby.assert_called_once_with()


class InactivityTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_or_invalid_configuration_creates_no_timer(self):
        for value in ("0", "invalid"):
            with self.subTest(value=value), patch.dict("os.environ", {"CORA_IDLE_TIMEOUT_SECONDS": value}):
                async with running_app() as (app, pilot):
                    self.assertEqual(app.inactivity.timeout_seconds, 0)
                    self.assertIsNone(app.idle_standby_timer)
                    if value == "invalid":
                        self.assertIn("automatic standby disabled", "\n".join(app.activity.entries))

    @patch("app.CoraApp.notify")
    @patch("app.CoraApp.reconnect_live", return_value=None)
    async def test_deadline_reuses_standby_once_without_notifications(self, transition, notify):
        async with idle_app() as (app, pilot, clock):
            clock.return_value = 399.9
            app.inactivity.last_activity = 100
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
            app.inactivity.last_activity = 0
            for owner, attribute, value in ((app, "closing", True), (app, "reconnecting", True),
                                            (app, "standby", True), (app.inactivity, "timeout_seconds", 0)):
                with self.subTest(attribute=attribute):
                    previous = getattr(owner, attribute)
                    setattr(owner, attribute, value)
                    app.check_inactivity()
                    setattr(owner, attribute, previous)
            app.live.ready = False
            app.connection_worker = MagicMock(is_finished=False)
            app.check_inactivity()
            standby.assert_not_called()

    @patch("app.CoraApp.notify")
    @patch("app.CoraApp.action_standby")
    async def test_known_work_blocks_silently_and_gets_fresh_full_grace(self, standby, notify):
        async with idle_app() as (app, pilot, clock):
            cases = (
                ("backend", lambda: app.backend_work.pending.add("pending")),
                ("acknowledgement", lambda: app.live.pending_text_requests.add("typed")),
                ("submission", lambda: setattr(app, "keyboard_worker", MagicMock(is_finished=False))),
                ("playback", lambda: setattr(app.live, "speaker", MagicMock(is_playback_busy=lambda: True))),
                ("speech", lambda: setattr(app, "last_live_activity", 1000)),
            )
            for name, arrange in cases:
                with self.subTest(name=name):
                    clock.return_value = 1000
                    app.inactivity.last_activity = 0
                    app.inactivity.was_busy = False
                    arrange()
                    app.check_inactivity()
                    standby.assert_not_called()
                    app.backend_work.clear()
                    app.live.pending_text_requests.clear()
                    app.keyboard_worker = None
                    app.live.speaker = None
                    app.last_live_activity = 0
                    clock.return_value = 1001
                    app.check_inactivity()
                    self.assertEqual(app.inactivity.last_activity, 1001)
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
            app.inactivity.last_activity = 0
            app.check_inactivity()
            standby.assert_not_called()
            release.set()
            await asyncio.wait_for(tool.wait(), 3)
            clock.return_value = 1001
            app.check_inactivity()
            self.assertEqual(app.inactivity.last_activity, 1001)
            memory_release = asyncio.Event()
            memory = app.run_worker(memory_release.wait(), group="memory-summary")
            try:
                clock.return_value = 1301
                app.check_inactivity()
                standby.assert_called_once()
            finally:
                memory_release.set()
                await asyncio.wait_for(memory.wait(), 3)

    async def test_typing_pasting_cursor_movement_and_audio_buttons_reset_clock(self):
        async with idle_app() as (app, pilot, clock):
            composer = app.query_one("#chat-input", ChatComposer)
            await pilot.press("a", "b")
            self.assertEqual(app.inactivity.last_activity, 1000)
            clock.return_value = 1010
            await pilot.press("backspace")
            self.assertEqual(app.inactivity.last_activity, 1010)
            clock.return_value = 1020
            with patch("ui.composer.pyperclip.paste", return_value="pasted text"):
                await pilot.press("ctrl+v")
            self.assertIn("pasted text", composer.text)
            self.assertEqual(app.inactivity.last_activity, 1020)
            clock.return_value = 1030
            await pilot.press("left")
            self.assertEqual(app.inactivity.last_activity, 1030)
            clock.return_value = 1040
            await pilot.click("#mic-status")
            self.assertEqual(app.inactivity.last_activity, 1040)
            clock.return_value = 1050
            await pilot.click("#speaker-status")
            self.assertEqual(app.inactivity.last_activity, 1050)

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
            self.assertEqual(app.inactivity.last_activity, 1010)
            clock.return_value = 1020
            release.set()
            await asyncio.wait_for(app.keyboard_worker.wait(), 3)
            self.assertEqual(app.inactivity.last_activity, 1020)

    @patch("app.CoraApp.action_standby")
    async def test_backend_finishing_between_polls_gets_full_grace(self, standby):
        async with idle_app() as (app, pilot, clock):
            for terminal in ("response.completed", "response.failed", "response.incomplete"):
                with self.subTest(terminal=terminal):
                    clock.return_value = 1000
                    app.inactivity.last_activity = 0
                    app.handle_live_event(backend_event("response.created"))
                    app.handle_live_event(backend_event(terminal))
                    self.assertEqual(app.inactivity.last_activity, 1000)
                    self.assertFalse(app.backend_work.pending)
                    clock.return_value = 1299.9
                    app.check_inactivity()
                    standby.assert_not_called()

    async def test_transcripts_and_audible_audio_reset_but_silence_does_not(self):
        async with idle_app() as (app, pilot, clock):
            for kind in ("session.input_transcript.delta", "session.output_transcript.delta"):
                app.inactivity.last_activity = 0
                app.handle_live_event(SimpleNamespace(type=kind, delta="Speech"))
                self.assertEqual(app.inactivity.last_activity, 1000)
            speaker = Speaker()
            app.live.speaker = speaker
            with patch("devices.speaker.sd.RawOutputStream"):
                try:
                    silence = base64.b64encode(bytes(960)).decode("ascii")
                    speaker.play(silence)
                    clock.return_value = 1010
                    app.handle_live_event(SimpleNamespace(type="session.output_audio.delta", delta=silence))
                    self.assertEqual(app.inactivity.last_activity, 1000)
                    speech = base64.b64encode(b"\x00\x20" * 480).decode("ascii")
                    speaker.play(speech)
                    app.handle_live_event(SimpleNamespace(type="session.output_audio.delta", delta=speech))
                    self.assertEqual(app.inactivity.last_activity, 1010)
                    app.live.speaker_muted = True
                    clock.return_value = 1020
                    app.handle_live_event(SimpleNamespace(type="session.output_audio.delta", delta=speech))
                    self.assertEqual(app.inactivity.last_activity, 1010)
                finally:
                    speaker.close()
                    app.live.speaker = None

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
                app.inactivity.last_activity = 700
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
                self.assertEqual(app.inactivity.last_activity, 2000)
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