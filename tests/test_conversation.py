from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from app import CoraApp
from ui.conversation import Conversation


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.output = MagicMock()
        self.set_timer = MagicMock(side_effect=lambda *args: MagicMock())
        self.conversation = Conversation(self.output, self.set_timer, MagicMock())

    def test_empty_delta_does_not_change_history_or_start_timer(self):
        self.conversation.append_transcript("You", "")
        self.assertEqual(self.conversation.log_entries, [])
        self.assertIsNone(self.conversation.active_transcript_index)
        self.set_timer.assert_not_called()
        self.output.clear.assert_not_called()

    def test_fragments_preserve_spacing_and_idle_timer_flushes_once(self):
        conversation = self.conversation
        conversation.append_transcript("Cora", "Hel")
        first_timer = conversation.transcript_idle_timer
        conversation.append_transcript("Cora", "lo world ")
        first_timer.stop.assert_called_once()
        self.assertEqual(conversation.log_entries, ["Cora: Hello world"])
        self.assertEqual(conversation.transcript_text, "Hello world ")
        self.assertEqual(self.set_timer.call_args.args[0], 2.0)

        idle_timer = conversation.transcript_idle_timer
        idle_callback = self.set_timer.call_args.args[1]
        idle_callback()
        idle_timer.stop.assert_called_once()
        self.assertIsNone(conversation.transcript_idle_timer)
        self.assertIsNone(conversation.active_transcript_index)
        self.assertIsNone(conversation.transcript_speaker)
        self.assertEqual(conversation.transcript_text, "")
        self.assertEqual(conversation.log_entries, ["Cora: Hello world", ""])
        conversation.flush_transcript()
        self.assertEqual(conversation.log_entries, ["Cora: Hello world", ""])
        conversation.append_transcript("Cora", "Next answer.")
        self.assertEqual(conversation.log_entries, ["Cora: Hello world", "", "Cora: Next answer."])

    def test_speaker_switch_and_typed_message_keep_single_separators(self):
        conversation = self.conversation
        conversation.append_transcript("You", "Hello.")
        conversation.append_transcript("Cora", "Hi.")
        conversation.write_log("You: Typed message.")
        conversation.write_log("")
        self.assertEqual(conversation.log_entries, [
            "You: Hello.", "", "Cora: Hi.", "", "You: Typed message.", "",
        ])
        self.assertIsNone(conversation.transcript_idle_timer)
        self.output.scroll_end.assert_called_with(animate=False)


class ConversationRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_transcripts_route_to_conversation_without_activity_flushing_them(self):
        with patch.object(CoraApp, "connect_to_cora", return_value=None), \
            patch.object(CoraApp, "record_chat_message"):
            app = CoraApp()
            async with app.run_test(size=(90, 35)):
                app.handle_live_event(SimpleNamespace(type="session.input_transcript.delta", delta="Hello"))
                app.handle_live_event(SimpleNamespace(type="session.input_transcript.delta", delta=" there."))
                self.assertEqual(app.conversation.log_entries, ["You: Hello there."])
                app.handle_live_event(SimpleNamespace(type="session.output_transcript.delta", delta="Hi Nick."))
                timer = app.conversation.transcript_idle_timer
                before = list(app.conversation.log_entries)
                app.activity.write("Unrelated activity", "Ready")
                self.assertEqual(app.conversation.log_entries, before)
                self.assertIs(app.conversation.transcript_idle_timer, timer)
                app.handle_live_event(SimpleNamespace(type="session.closed", reason="client_requested"))
                self.assertEqual(app.conversation.log_entries, ["You: Hello there.", "", "Cora: Hi Nick.", ""])
                self.assertIsNone(app.conversation.transcript_idle_timer)
                self.assertIsNone(app.conversation.active_transcript_index)


if __name__ == "__main__":
    unittest.main()