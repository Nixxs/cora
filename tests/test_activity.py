from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, call, patch

from ui.activity import Activity
from app import CoraApp


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.panel = MagicMock()
        self.panel.collapsed = True
        self.output = MagicMock()
        self.output.scrollable_content_region.width = 40
        self.activity = Activity(self.panel, self.output)
        clock = patch("ui.activity.datetime")
        clock.start().now.return_value.strftime.return_value = "12:34:56"
        self.addCleanup(clock.stop)

    def test_collapsed_logging_keeps_timestamps_title_and_error_state(self):
        for summary, error in (("API ERROR", True), ("Backend failed", True),
                               ("Backend incomplete", True), ("Connected", False)):
            with self.subTest(summary=summary):
                self.activity.write("A diagnostic", summary)
                self.assertEqual(self.activity.entries[-1], "12:34:56  A diagnostic")
                self.assertEqual(self.panel.title, f"Activity · {summary}")
                self.panel.set_class.assert_called_with(error, "activity-error")
        self.output.clear.assert_not_called()
        self.output.write.assert_not_called()

    def test_history_retains_only_the_latest_500_entries(self):
        for index in range(505):
            self.activity.write(f"Message {index}", "Ready")
        self.assertEqual(len(self.activity.entries), 500)
        self.assertEqual(self.activity.entries[0], "12:34:56  Message 5")
        self.assertEqual(self.activity.entries[-1], "12:34:56  Message 504")
        self.output.write.assert_not_called()

    def test_redraw_waits_for_visible_width_and_expanded_writes_refresh(self):
        self.activity.write("First", "Ready")
        self.activity.redraw()
        self.output.clear.assert_not_called()
        self.panel.collapsed = False
        self.output.scrollable_content_region.width = 0
        self.activity.redraw()
        self.output.clear.assert_not_called()
        self.output.scrollable_content_region.width = 40
        self.activity.write("Second", "Connected")
        self.output.clear.assert_called_once()
        self.assertEqual(self.output.write.call_args_list, [
            call("12:34:56  First", width=40, scroll_end=False),
            call("12:34:56  Second", width=40, scroll_end=False),
        ])
        self.output.scroll_end.assert_called_once_with(animate=False)


class ActivityPanelTests(unittest.IsolatedAsyncioTestCase):
    async def test_expansion_replays_history_without_duplicates_or_transcript_changes(self):
        with patch.object(CoraApp, "connect_to_cora", return_value=None), \
            patch.object(CoraApp, "record_chat_message"):
            app = CoraApp()
            async with app.run_test(size=(90, 35)) as pilot:
                await pilot.pause()
                activity = app.activity
                conversation = app.conversation
                conversation.append_transcript("Cora", "Still speaking.")
                timer = conversation.transcript_idle_timer
                active_index = conversation.active_transcript_index
                before = list(conversation.log_entries)
                activity.write("First event", "Ready")
                activity.write("Second event", "Backend error")
                self.assertTrue(activity.panel.collapsed)
                self.assertEqual(activity.output.lines, [])
                self.assertTrue(activity.panel.has_class("activity-error"))

                activity.panel.collapsed = False
                await pilot.pause()
                # RichLog may replay its first writes on a queued Resize message.
                await pilot.pause()
                self.assertGreater(activity.output.scrollable_content_region.width, 0)
                self.assertEqual([line.text for line in activity.output.lines], list(activity.entries))
                activity.write("Third event", "Connected")
                await pilot.pause()
                self.assertFalse(activity.panel.has_class("activity-error"))
                self.assertEqual([line.text for line in activity.output.lines], list(activity.entries))

                activity.panel.collapsed = True
                await pilot.pause()
                activity.write("Fourth event", "Ready")
                await pilot.resize_terminal(65, 35)
                activity.panel.collapsed = False
                await pilot.pause()
                await pilot.pause()
                self.assertEqual([line.text for line in activity.output.lines], list(activity.entries))
                self.assertEqual(conversation.log_entries, before)
                self.assertEqual(conversation.active_transcript_index, active_index)
                self.assertIs(conversation.transcript_idle_timer, timer)

    async def test_app_events_and_audio_diagnostics_use_activity_component(self):
        with patch.object(CoraApp, "connect_to_cora", return_value=None):
            app = CoraApp()
            async with app.run_test(size=(90, 35)):
                app.handle_live_event(SimpleNamespace(type="session.started"))
                self.assertEqual(app.activity.panel.title, "Activity · Connected")
                app.handle_live_event(SimpleNamespace(
                    type="error", error=SimpleNamespace(message="Mock failure", client_event_id="command_1")
                ))
                self.assertTrue(app.activity.panel.has_class("activity-error"))
                self.assertTrue(any("API error: Mock failure" in entry for entry in app.activity.entries))
                self.assertTrue(any("Rejected command: id=command_1" in entry for entry in app.activity.entries))
                app.handle_audio_status(False, "Mock speaker failure")
                self.assertEqual(app.activity.panel.title, "Activity · Audio playback error")
                app.handle_audio_status(True, "Speaker stream started")
                self.assertEqual(app.activity.panel.title, "Activity · Audio playback ready")
                self.assertFalse(app.activity.panel.has_class("activity-error"))
                self.assertEqual(app.conversation.log_entries, [])


if __name__ == "__main__":
    unittest.main()