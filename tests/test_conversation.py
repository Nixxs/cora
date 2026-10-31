from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from app import CoraApp
from ui.chat_widgets import ChatMarkdown
from ui.conversation import SPEAKER_STYLES, STREAM_REDRAW_SECONDS, Conversation, styled_entry


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.output = MagicMock()
        self.set_timer = MagicMock(side_effect=lambda *args: MagicMock())
        self.conversation = Conversation(self.output, self.set_timer, MagicMock())

    def test_speaker_labels_are_coloured_and_the_rest_left_alone(self):
        cora = styled_entry("Cora: hi")
        self.assertEqual(cora.plain, "Cora: hi")
        self.assertEqual([(span.start, span.end, span.style) for span in cora.spans], [(0, 5, SPEAKER_STYLES["Cora"])])
        signal = styled_entry("You · Signal: Cora: not a label")
        self.assertEqual(signal.spans[0].end, len("You · Signal:"))
        self.assertEqual(signal.spans[0].style, SPEAKER_STYLES["You"])
        self.assertNotEqual(SPEAKER_STYLES["Cora"], SPEAKER_STYLES["You"])
        for entry in ("", "From example.com", "Your turn: soon", ChatMarkdown("**x**")):
            self.assertIs(styled_entry(entry), entry)

    def test_drawn_lines_colour_the_speaker(self):
        self.conversation.write_log("You: Hello.")
        written = self.output.write.call_args.args[0]
        self.assertEqual(written.plain, "You: Hello.")
        self.assertEqual(written.spans[0].style, SPEAKER_STYLES["You"])
        # The stored entry stays plain, for history and saving.
        self.assertEqual(self.conversation.log_entries, ["You: Hello."])

    def test_a_written_draft_is_one_message_updated_in_place_then_finished(self):
        conversation = self.conversation
        conversation.record_message = MagicMock()
        draft = conversation.start_written_reply()
        draft.update("  ")
        self.assertEqual(conversation.log_entries, [])
        draft.update("Hel")
        # Fast fragments share a timed redraw.
        self.set_timer.assert_called_once_with(STREAM_REDRAW_SECONDS, conversation.redraw_log)
        draft.update("Hello **there**")
        self.assertEqual(self.set_timer.call_count, 1)
        conversation.write_log("You: meanwhile")
        draft.update("Hello **there**, Nick")
        entries = conversation.log_entries
        self.assertEqual(entries[:2], ["", "Cora · writing…:"])
        self.assertEqual(entries[2].markup, "Hello **there**, Nick")
        draft.finish("Hello **there**, Nick.")
        self.assertEqual(entries[1], "Cora · written:")
        self.assertEqual(entries[2].markup, "Hello **there**, Nick.")
        self.assertEqual(sum(isinstance(entry, ChatMarkdown) for entry in entries), 1)
        conversation.record_message.assert_called_once_with("assistant", "Hello **there**, Nick.", "screen")
        draft.update("late")
        draft.fail()
        self.assertEqual(entries[1], "Cora · written:")

    def test_a_failed_draft_keeps_its_text_marked_incomplete_and_unsaved(self):
        self.conversation.record_message = MagicMock()
        draft = self.conversation.start_written_reply()
        draft.update("Half")
        draft.fail()
        self.assertEqual(self.conversation.log_entries[1], "Cora · written · incomplete:")
        self.assertEqual(self.conversation.log_entries[2].markup, "Half")
        self.conversation.record_message.assert_not_called()
        # Failing before any text shows nothing at all.
        empty = self.conversation.start_written_reply()
        empty.fail()
        self.assertEqual(len(self.conversation.log_entries), 4)

    def test_a_draft_finished_without_streaming_is_an_ordinary_written_reply(self):
        self.conversation.start_written_reply().finish("All at once.")
        entries = self.conversation.log_entries
        self.assertEqual(entries[1], "Cora · written:")
        self.assertEqual(entries[2].markup, "All at once.")

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