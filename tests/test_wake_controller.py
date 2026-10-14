"""Wake coordination without microphone capture, network, or Textual startup."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from wake.controller import WakeController


class WakeControllerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.live = SimpleNamespace(
            microphone_muted=False, connection=None, ready=False, opening_microphone=None,
        )
        self.button = MagicMock(is_mounted=True)
        self.clock = MagicMock(return_value=100.0)
        self.can_listen = MagicMock(return_value=True)
        self.closing = MagicMock(return_value=False)
        self.reconnecting = MagicMock(return_value=False)
        self.request_connection = MagicMock(return_value=True)
        self.abort = MagicMock()
        self.activity = MagicMock()
        self.blockers = MagicMock(return_value=[])
        self.standby = MagicMock()
        self.write = MagicMock()
        self.set_timer = MagicMock()
        self.set_interval = MagicMock()
        self.controller = WakeController(
            live=self.live, control=self.button, enabled=True, debug=True,
            clock=self.clock, can_listen=self.can_listen, is_closing=self.closing,
            is_reconnecting=self.reconnecting, request_connection=self.request_connection,
            abort_connection=self.abort, record_idle_activity=self.activity,
            transition_blockers=self.blockers, enter_standby=self.standby,
            write_activity=self.write, set_timer=self.set_timer, set_interval=self.set_interval,
        )
        self.listener = SimpleNamespace(start=MagicMock(), stop=AsyncMock(), error=None)

    def test_listener_guards_prevent_capture(self):
        for owner, name, value in (
            (self.controller, "enabled", False),
            (self.controller, "failed", True),
            (self.controller, "can_listen", lambda: False),
            (self.live, "microphone_muted", True),
            (self.live, "connection", object()),
            (self.controller, "listener", self.listener),
        ):
            with self.subTest(guard=name), patch.object(owner, name, value):
                with patch("wake.controller.WakeListener") as factory:
                    self.controller.start_listener()
                    factory.assert_not_called()

    def test_start_is_single_flight_with_remaining_cooldown_and_debug_callback(self):
        self.controller.retry_at = 103.0
        with patch("wake.controller.WakeListener", return_value=self.listener) as factory:
            self.controller.start_listener()
            self.controller.start_listener()
        factory.assert_called_once_with(
            self.controller.handle_wake, self.controller.handle_status,
            cooldown=3.0, on_debug=self.controller.handle_diagnostic,
        )
        self.listener.start.assert_called_once_with()

    def test_disabled_debug_and_expired_cooldown(self):
        self.controller.debug = False
        with patch("wake.controller.WakeListener", return_value=self.listener) as factory:
            self.controller.start_listener()
        self.assertEqual(factory.call_args.kwargs["cooldown"], 0.0)
        self.assertIsNone(factory.call_args.kwargs["on_debug"])

    def test_wake_guards_reject_without_handoff_or_transition(self):
        self.controller.listener = self.listener
        self.assertFalse(self.controller.handle_wake(object()))
        for owner, name, value in (
            (self.controller, "enabled", False),
            (self.live, "microphone_muted", True),
            (self.controller, "can_listen", lambda: False),
        ):
            with self.subTest(guard=name), patch.object(owner, name, value):
                self.assertFalse(self.controller.handle_wake(self.listener))
        self.assertIsNone(self.live.opening_microphone)
        self.request_connection.assert_not_called()
        self.set_timer.assert_not_called()

    def test_wake_hands_off_listener_before_requesting_connection(self):
        self.controller.listener = self.listener
        self.controller.listening = True

        def request():
            self.assertIs(self.live.opening_microphone, self.listener)
            self.activity.assert_called_once_with()
            return True

        self.request_connection.side_effect = request
        self.assertTrue(self.controller.handle_wake(self.listener))
        self.assertFalse(self.controller.listening)
        self.assertEqual(self.controller.retry_at, 105.0)
        self.set_timer.assert_called_once_with(15.0, self.abort)
        self.assertIs(self.controller.connection_timer, self.set_timer.return_value)
        self.listener.stop.assert_not_awaited()

    def test_blocked_transition_discards_opening_handoff_without_abort_timer(self):
        self.controller.listener = self.listener
        self.request_connection.return_value = False
        self.assertFalse(self.controller.handle_wake(self.listener))
        self.assertIsNone(self.live.opening_microphone)
        self.set_timer.assert_not_called()

    async def test_stop_detaches_listener_before_awaiting_and_clears_its_handoff(self):
        self.controller.listener = self.listener
        self.controller.listening = True
        self.live.opening_microphone = self.listener

        async def stop():
            self.assertIsNone(self.controller.listener)
            self.assertFalse(self.controller.listening)

        self.listener.stop.side_effect = stop
        await self.controller.stop_listener()
        self.listener.stop.assert_awaited_once_with()
        self.assertIsNone(self.live.opening_microphone)
        # A different microphone owner must not be cleared by listener cleanup.
        other = object()
        self.controller.listener = self.listener
        self.live.opening_microphone = other
        await self.controller.stop_listener()
        self.assertIs(self.live.opening_microphone, other)

    def test_listener_failure_aborts_wake_reconnect_and_closing_ignores_status(self):
        self.controller.reconnecting = True
        self.controller.handle_status(False, "Capture failed")
        self.assertTrue(self.controller.failed)
        self.assertFalse(self.controller.listening)
        self.abort.assert_called_once_with()
        self.write.assert_called_once_with("Capture failed", "Local wake unavailable")
        self.closing.return_value = True
        self.controller.handle_status(True, "Late status")
        self.assertFalse(self.controller.listening)
        self.write.assert_called_once()

    def test_diagnostics_obey_privacy_enable_and_closing_guards(self):
        for owner, name, value in (
            (self.controller, "enabled", False),
            (self.live, "microphone_muted", True),
            (self.controller, "is_closing", lambda: True),
        ):
            with self.subTest(guard=name), patch.object(owner, name, value):
                self.controller.handle_diagnostic("Local text")
        self.write.assert_not_called()
        self.controller.handle_diagnostic("Local text")
        self.write.assert_called_once_with("[wake] Local text", "Local wake diagnostics")

    def test_wake_session_starts_ten_second_window_without_reset_on_other_sessions(self):
        timer = MagicMock()
        self.controller.connection_timer = timer
        self.controller.reconnecting = True
        self.controller.transcript = "Old text"
        self.controller.session_started()
        self.assertFalse(self.controller.reconnecting)
        self.assertEqual(self.controller.response_deadline, 110.0)
        self.assertEqual(self.controller.transcript, "")
        timer.stop.assert_called_once_with()
        self.clock.return_value = 200.0
        self.controller.session_started()
        self.assertEqual(self.controller.response_deadline, 110.0)
        timer.stop.assert_called_once()

    def test_partial_wake_aliases_and_punctuation_are_not_requests(self):
        for name in ("Cora", "Kora", "Korra"):
            for prefix in ("", "Hey "):
                with self.subTest(name=name, prefix=prefix):
                    self.controller.response_deadline = 110.0
                    self.controller.transcript = ""
                    for char in f"{prefix}{name}!":
                        self.assertFalse(self.controller.has_request(char))
                    self.assertTrue(self.controller.has_request(" What time is it?"))
        self.controller.response_deadline = None
        self.assertTrue(self.controller.has_request("Hey Cora"))

    def test_false_wake_waits_for_ready_idle_connection_and_runs_only_once(self):
        self.controller.response_deadline = 110.0
        self.live.ready = True
        self.controller.check_timeout()
        self.standby.assert_not_called()
        self.clock.return_value = 110.0
        for owner, name, value in (
            (self.controller, "is_closing", lambda: True),
            (self.controller, "is_reconnecting", lambda: True),
            (self.live, "ready", False),
            (self.controller, "transition_blockers", lambda: ["audio playback"]),
        ):
            with self.subTest(guard=name), patch.object(owner, name, value):
                self.controller.check_timeout()
                self.standby.assert_not_called()
                self.assertEqual(self.controller.response_deadline, 110.0)
        self.controller.check_timeout()
        self.controller.check_timeout()
        self.assertIsNone(self.controller.response_deadline)
        self.standby.assert_called_once_with()

    def test_timer_cleanup_and_detached_control_are_safe(self):
        self.controller.connection_timer = MagicMock()
        self.controller.stop_timers()
        self.set_interval.assert_called_once_with(1.0, self.controller.check_timeout)
        self.set_interval.return_value.stop.assert_called_once_with()
        self.controller.connection_timer.stop.assert_called_once_with()
        self.button.is_mounted = False
        self.controller.update_control()
        self.button.refresh.assert_not_called()


if __name__ == "__main__":
    unittest.main()