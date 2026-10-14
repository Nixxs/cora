import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from openai.types.live.commentary_appended_event import CommentaryAppendedEvent
from textual.widgets import Button

from app import CoraApp
from live import CoraLive
from test_inactivity import idle_app
from test_keyboard_persistence import running_app
from test_reconnect import backend_event, restarting_app
from ui.composer import ChatComposer


def acknowledge(app, event_id=None):
    app.handle_live_event(CommentaryAppendedEvent(
        type="session.commentary.appended", event_id="accepted", start_ms=0, end_ms=0,
        client_event_id=event_id or app.inactivity.alert.event_id,
    ))


def output(app):
    app.handle_live_event(SimpleNamespace(
        type="session.output_transcript.delta", delta="I'll leave you to it—going into standby.",
    ))


@asynccontextmanager
async def alert_app():
    async with idle_app() as (app, pilot, clock):
        app.inactivity.farewell_enabled = True
        app.live.send_standby_alert = AsyncMock()
        app.live.speaker = MagicMock()
        app.live.speaker.get_playback_level.return_value = 0.0
        app.live.speaker.is_playback_busy.return_value = False
        app.set_connection_state("active")
        app.inactivity.last_activity = 700
        app.check_inactivity()
        await asyncio.wait_for(app.inactivity.alert_worker.wait(), 3)
        yield app, pilot, clock


class StandbyAlertTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_commentary_does_not_create_backend_work(self):
        live = CoraLive()
        live.ready = True
        live.connection = MagicMock()
        live.connection.session.commentary.append = AsyncMock()
        await live.send_standby_alert("alert-1")
        command = live.connection.session.commentary.append.call_args.kwargs
        self.assertEqual(command["event_id"], "alert-1")
        self.assertIsNone(command["delegation_id"])
        self.assertIn("standby", command["content"])
        live.connection.response.create.assert_not_called()
        live.connection.response.item.create.assert_not_called()
        live.connection.session.thinking.append.assert_not_called()
        self.assertFalse(live.pending_text_requests)
        live.speaker_muted = True
        await live.send_standby_alert("alert-2")
        live.connection.session.commentary.append.assert_awaited_once()

    async def test_disconnected_transport_cannot_send_alert(self):
        with self.assertRaises(RuntimeError):
            await CoraLive().send_standby_alert("alert-1")


class StandbyFarewellTests(unittest.IsolatedAsyncioTestCase):
    async def test_configuration_is_opt_in(self):
        for value, enabled in (("0", False), ("1", True), ("invalid", False)):
            with self.subTest(value=value), patch.dict("os.environ", {"CORA_STANDBY_FAREWELL": value}):
                async with running_app() as (app, pilot):
                    self.assertEqual(app.inactivity.farewell_enabled, enabled)

    async def test_single_alert_keeps_input_mic_and_live_available(self):
        async with alert_app() as (app, pilot, clock):
            app.check_inactivity()
            app.live.send_standby_alert.assert_awaited_once_with(app.inactivity.alert.event_id)
            app.live.close.assert_not_called()
            self.assertTrue(app.live.ready)
            self.assertFalse(app.reconnecting)
            self.assertFalse(app.query_one("#chat-input", ChatComposer).disabled)
            self.assertFalse(app.query_one("#mic-status", Button).disabled)
            self.assertIn("standby heads-up", app.live_transition_blockers())

    @patch("app.CoraApp.action_standby")
    async def test_acceptance_and_silence_are_not_playback_completion(self, standby):
        async with alert_app() as (app, pilot, clock):
            acknowledge(app, "unrelated")
            self.assertFalse(app.inactivity.alert.accepted)
            acknowledge(app)
            clock.return_value = 1020
            app.handle_live_event(SimpleNamespace(type="session.output_audio.delta", delta="AAA="))
            app.check_inactivity()
            standby.assert_not_called()
            self.assertIsNone(app.inactivity.alert.last_output_at)
            clock.return_value = 1030
            app.check_inactivity()
            self.assertIsNone(app.inactivity.alert)
            standby.assert_not_called()
            self.assertIn("timed out", app.activity.entries[-1])

    @patch("app.CoraApp.action_standby")
    async def test_waits_for_playback_quiet_gap_and_response_window(self, standby):
        async with alert_app() as (app, pilot, clock):
            acknowledge(app)
            output(app)
            self.assertIsNotNone(app.inactivity.alert)  # Cora's own speech is not user activity.
            app.live.speaker.is_playback_busy.return_value = True
            clock.return_value = 1004
            app.check_inactivity()
            self.assertIsNone(app.inactivity.alert.quiet_since)
            standby.assert_not_called()
            app.live.speaker.is_playback_busy.return_value = False
            app.check_inactivity()
            clock.return_value = 1006.9
            app.check_inactivity()
            standby.assert_not_called()
            clock.return_value = 1007
            app.check_inactivity()
            standby.assert_called_once()
            self.assertIsNone(app.inactivity.alert)

    @patch("app.CoraApp.action_standby")
    async def test_missing_ack_or_long_playback_times_out_without_closing(self, standby):
        for accepted in (False, True):
            with self.subTest(accepted=accepted):
                async with alert_app() as (app, pilot, clock):
                    if accepted:
                        acknowledge(app)
                        app.live.speaker.is_playback_busy.return_value = True
                    output(app)
                    clock.return_value = 1029
                    app.check_inactivity()
                    self.assertIsNotNone(app.inactivity.alert)
                    clock.return_value = 1030
                    app.check_inactivity()
                    self.assertIsNone(app.inactivity.alert)
                    standby.assert_not_called()

    async def test_empty_input_transcript_is_not_a_user_response(self):
        async with alert_app() as (app, pilot, clock):
            app.handle_live_event(SimpleNamespace(type="session.input_transcript.delta", delta=""))
            self.assertIsNotNone(app.inactivity.alert)
            self.assertIsNone(app.inactivity.alert.last_output_at)

    @patch("app.CoraApp.action_standby")
    async def test_later_output_restarts_quiet_and_response_window(self, standby):
        async with alert_app() as (app, pilot, clock):
            acknowledge(app)
            output(app)
            clock.return_value = 1002
            app.check_inactivity()
            clock.return_value = 1004
            output(app)
            app.check_inactivity()
            self.assertIsNone(app.inactivity.alert.quiet_since)
            clock.return_value = 1006
            app.check_inactivity()
            clock.return_value = 1008.9
            app.check_inactivity()
            standby.assert_not_called()
            clock.return_value = 1009
            app.check_inactivity()
            standby.assert_called_once()

    async def test_oh_wait_during_response_window_cancels_and_grants_full_idle(self):
        async with alert_app() as (app, pilot, clock):
            acknowledge(app)
            output(app)
            clock.return_value = 1002
            app.check_inactivity()
            clock.return_value = 1004
            app.handle_live_event(SimpleNamespace(type="session.input_transcript.delta", delta="Oh wait"))
            self.assertIsNone(app.inactivity.alert)
            self.assertEqual(app.inactivity.last_activity, 1004)
            clock.return_value = 1008
            app.check_inactivity()
            app.live.close.assert_not_called()
            app.live.send_standby_alert.assert_awaited_once()
            self.assertTrue(app.live.ready)

    async def test_typing_and_audio_control_cancel_pending_sleep(self):
        for interaction in ("typing", "mute"):
            with self.subTest(interaction=interaction):
                async with alert_app() as (app, pilot, clock):
                    if interaction == "typing":
                        await pilot.press("a")
                    else:
                        await pilot.click("#speaker-status")
                    self.assertIsNone(app.inactivity.alert)
                    app.check_inactivity()
                    app.live.close.assert_not_called()
                    app.live.send_standby_alert.assert_awaited_once()

    async def test_new_backend_work_cancels_even_between_checks(self):
        async with alert_app() as (app, pilot, clock):
            app.handle_live_event(backend_event("response.created"))
            app.handle_live_event(backend_event("response.completed"))
            self.assertIsNone(app.inactivity.alert)
            app.check_inactivity()
            app.live.close.assert_not_called()

    async def test_new_tool_work_cancels_at_check(self):
        async with alert_app() as (app, pilot, clock):
            release = asyncio.Event()
            tool = app.run_worker(release.wait(), group="tool-actions")
            try:
                app.check_inactivity()
                self.assertIsNone(app.inactivity.alert)
                app.live.close.assert_not_called()
            finally:
                release.set()
                await asyncio.wait_for(tool.wait(), 3)

    async def test_rejection_and_playback_failure_do_not_close_or_retry(self):
        for failure in ("rejected", "audio"):
            with self.subTest(failure=failure):
                async with alert_app() as (app, pilot, clock):
                    if failure == "rejected":
                        app.handle_live_event(SimpleNamespace(type="error", error=SimpleNamespace(
                            message="Rejected", client_event_id=app.inactivity.alert.event_id,
                        )))
                    else:
                        app.handle_audio_status(False, "Playback failed")
                    self.assertIsNone(app.inactivity.alert)
                    app.check_inactivity()
                    app.live.close.assert_not_called()
                    app.live.send_standby_alert.assert_awaited_once()

    async def test_send_failure_preserves_draft_and_stays_active(self):
        async with idle_app() as (app, pilot, clock):
            app.inactivity.farewell_enabled = True
            app.live.send_standby_alert = AsyncMock(side_effect=RuntimeError("private error details"))
            composer = app.query_one("#chat-input", ChatComposer)
            composer.load_text("Unsent draft")
            await pilot.pause()
            app.inactivity.last_activity = 700
            app.check_inactivity()
            await app.workers.wait_for_complete()
            self.assertIsNone(app.inactivity.alert)
            self.assertEqual(composer.text, "Unsent draft")
            self.assertNotIn("private error details", "\n".join(app.activity.entries))
            app.live.close.assert_not_called()

    @patch("app.CoraApp.action_standby")
    async def test_muted_speaker_skips_alert_and_response_window(self, standby):
        async with idle_app() as (app, pilot, clock):
            app.inactivity.farewell_enabled = True
            app.live.speaker_muted = True
            app.live.send_standby_alert = AsyncMock()
            app.inactivity.last_activity = 700
            app.check_inactivity()
            standby.assert_called_once()
            app.live.send_standby_alert.assert_not_called()

    async def test_manual_standby_does_not_send_a_heads_up(self):
        async with idle_app() as (app, pilot, clock):
            app.inactivity.farewell_enabled = True
            app.live.send_standby_alert = AsyncMock()
            with patch.object(app, "reconnect_live", return_value=None) as transition:
                app.action_standby()
                transition.assert_called_once_with(restart=False)
            app.live.send_standby_alert.assert_not_called()

    async def test_lifecycle_change_cancels_heads_up(self):
        for state in ("closing", "reconnecting", "standby", "disconnected"):
            with self.subTest(state=state):
                async with alert_app() as (app, pilot, clock):
                    if state == "disconnected":
                        app.live.ready = False
                    else:
                        setattr(app, state, True)
                    app.check_inactivity()
                    self.assertIsNone(app.inactivity.alert)
                    app.live.close.assert_not_called()

    @patch("app.CoraApp.exit")
    async def test_quit_cancels_inflight_alert_without_waiting_for_send(self, exit_app):
        async with idle_app() as (app, pilot, clock):
            app.inactivity.farewell_enabled = True
            entered = asyncio.Event()

            async def send(event_id):
                entered.set()
                await asyncio.Event().wait()

            app.live.send_standby_alert = AsyncMock(side_effect=send)
            app.inactivity.last_activity = 700
            app.check_inactivity()
            await asyncio.wait_for(entered.wait(), 3)
            await asyncio.wait_for(app.action_disconnect_and_quit(), 3)
            self.assertIsNone(app.inactivity.alert)
            self.assertTrue(app.inactivity.alert_worker.is_cancelled)
            exit_app.assert_called_once()

    @patch("app.monotonic", return_value=1000)
    async def test_actual_mocked_live_close_after_heads_up_preserves_local_state(self, clock):
        with patch.dict("os.environ", {"CORA_STANDBY_FAREWELL": "1", "CORA_IDLE_TIMEOUT_SECONDS": "300"}):
            async with restarting_app() as (app, pilot, connections, speakers, started):
                app.idle_standby_timer.stop()
                await app.live.set_microphone_muted(True)
                conversation_id = app.persistence.conversation_id
                composer = app.query_one("#chat-input", ChatComposer)
                composer.load_text("Unsent draft")
                await pilot.pause()
                connections[0].session.commentary = SimpleNamespace(append=AsyncMock())
                app.inactivity.last_activity = 700
                app.check_inactivity()
                await asyncio.wait_for(app.inactivity.alert_worker.wait(), 3)
                acknowledge(app)
                output(app)
                clock.return_value = 1002
                app.check_inactivity()
                connections[0].session.close.assert_not_called()
                clock.return_value = 1005
                app.check_inactivity()
                await asyncio.wait_for(app.reconnect_worker.wait(), 3)
                self.assertTrue(app.standby)
                self.assertIsNone(app.live.connection)
                self.assertEqual(composer.text, "Unsent draft")
                self.assertEqual(app.persistence.conversation_id, conversation_id)
                self.assertTrue(app.live.microphone_muted)
                connections[0].session.commentary.append.assert_awaited_once()
                connections[0].response.create.assert_not_called()
                connections[0].session.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()