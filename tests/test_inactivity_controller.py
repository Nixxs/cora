"""Deterministic idle and heads-up timing without a Textual app."""

import unittest
from unittest.mock import MagicMock

from inactivity import InactivityController


class InactivityControllerTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()