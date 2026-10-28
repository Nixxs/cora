"""The context tracker splits a Live connection's startup payload into the dashboard's items."""

import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock

from context_window import BACKEND, DELEGATED_NOTE, VOICE, ContextTracker, estimate_tokens, short_tokens
from live import CoraLive
from session_config import (
    BACKEND_MODEL, LIVE_INSTRUCTIONS, RECONNECT_INSTRUCTIONS, config_from_sections, instruction_sections,
)


TOOLS = [{"type": "function", "name": "close_app", "parameters": {"type": "object"}}]
HISTORY = [
    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Hi"}]},
    {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Hello"}]},
]


def started(history=None, **contexts):
    sections = instruction_sections(**contexts)
    config = config_from_sections(TOOLS, sections, history)
    tracker = ContextTracker()
    tracker.start(config, sections)
    return tracker, config


class ContextTrackerTests(unittest.TestCase):
    def test_nothing_is_tracked_before_a_connection(self):
        tracker = ContextTracker()
        self.assertEqual(tracker.items(VOICE), ())
        self.assertEqual(tracker.items(BACKEND), ())

    def test_voice_items_add_up_to_the_live_instructions_sent(self):
        tracker, config = started(
            memory_context="[1]", interest_context="[2]", person_name="Sam",
            identity_context="me", relationship_context="close", people_context="[3]",
        )
        voice = tracker.items(VOICE)
        self.assertEqual(
            [item.name for item in voice],
            ["instructions", "identity", "relationship", "people", "memories", "interests"],
        )
        # The name joins the instructions rather than taking a line of its own.
        self.assertTrue(voice[0].text.startswith(LIVE_INSTRUCTIONS))
        self.assertIn('"Sam"', voice[0].text)
        self.assertEqual("".join(item.text for item in voice), config["instructions"])
        self.assertEqual([item.tokens for item in voice], [estimate_tokens(item.text) for item in voice])

    def test_empty_sections_are_left_out(self):
        tracker, _ = started(memory_context="[1]")
        self.assertEqual([item.name for item in tracker.items(VOICE)], ["instructions", "memories"])

    def test_reconnect_history_is_its_own_item_with_its_note(self):
        tracker, _ = started(history=HISTORY)
        history = tracker.items(VOICE)[-1]
        self.assertEqual(history.name, "history")
        self.assertEqual(history.text, "You: Hi\n\nCora: Hello" + RECONNECT_INSTRUCTIONS)

    def test_backend_holds_its_instructions_and_the_tools_counted_compact(self):
        tracker, config = started(memory_context="[1]")
        instructions, tools = tracker.items(BACKEND)
        self.assertEqual(instructions.text, config["delegation"]["responses"]["instructions"])
        sent = config["delegation"]["responses"]["tools"]
        self.assertEqual(json.loads(tools.text), sent)
        self.assertEqual(tools.tokens, estimate_tokens(json.dumps(sent, separators=(",", ":"))))
        # Indented to read, so the shown text is longer than what is counted.
        self.assertGreater(estimate_tokens(tools.text), tools.tokens)

    def test_a_new_connection_replaces_the_last_ones_items(self):
        tracker, _ = started(history=HISTORY, memory_context="[1]")
        sections = instruction_sections()
        tracker.start(config_from_sections(TOOLS, sections), sections)
        self.assertEqual([item.name for item in tracker.items(VOICE)], ["instructions"])

    def test_speech_and_notes_build_the_voice_conversation(self):
        tracker, _ = started()
        for event_type, delta in (
            ("session.input_transcript.delta", "Hi "), ("session.input_transcript.delta", "there"),
            ("session.output_transcript.delta", "Hello"), ("session.output_transcript.delta", ""),
        ):
            tracker.observe(SimpleNamespace(type=event_type, delta=delta))
        tracker.note("Opening greeting note")
        tracker.observe(SimpleNamespace(type="session.output_transcript.delta", delta="Welcome"))
        conversation = tracker.items(VOICE)[-1]
        self.assertEqual(conversation.name, "conversation")
        self.assertEqual(
            conversation.text, "You: Hi there\n\nCora: Hello\n\n[note] Opening greeting note\n\nCora: Welcome",
        )
        self.assertEqual(conversation.tokens, estimate_tokens(conversation.text))

    def test_the_api_measures_both_totals(self):
        tracker, _ = started()
        tracker.observe(SimpleNamespace(type="session.usage.updated", context_window=None))
        self.assertIsNone(tracker.voice_ratio)
        tracker.observe(SimpleNamespace(
            type="session.usage.updated", context_window=SimpleNamespace(usage_ratio=0.62),
        ))
        self.assertEqual(tracker.voice_ratio, 0.62)
        # Only a finished response carries usage.
        tracker.observe(SimpleNamespace(type="response.event", event={"type": "response.created"}))
        self.assertIsNone(tracker.backend_tokens)
        tracker.observe(SimpleNamespace(type="response.event", event={
            "type": "response.completed", "response": {"usage": {"input_tokens": 18000}},
        }))
        self.assertEqual(tracker.backend_tokens, 18000)
        self.assertEqual(tracker.backend_model, BACKEND_MODEL)

    def test_delegated_turns_are_what_the_measured_input_holds_beyond_instructions_and_tools(self):
        tracker, _ = started()
        self.assertEqual([item.name for item in tracker.items(BACKEND)], ["instructions", "tools"])
        tracker.typed("What's the weather?")
        startup = sum(item.tokens for item in tracker.items(BACKEND))
        tracker.backend_tokens = startup + 500
        delegated = tracker.items(BACKEND)[-1]
        self.assertEqual(delegated.name, "delegated turns")
        self.assertEqual(delegated.tokens, 500)
        self.assertTrue(delegated.text.startswith("You: What's the weather?\n\n"))
        self.assertTrue(delegated.text.endswith(DELEGATED_NOTE))
        # An estimate above the measurement never goes negative.
        tracker.backend_tokens = 1
        self.assertEqual(tracker.items(BACKEND)[-1].tokens, 0)

    def test_ending_keeps_the_numbers_until_the_next_connection(self):
        tracker, _ = started()
        tracker.note("hello")
        tracker.voice_ratio = 0.4
        tracker.end()
        self.assertFalse(tracker.active)
        self.assertEqual(tracker.voice_ratio, 0.4)
        self.assertEqual(tracker.items(VOICE)[-1].name, "conversation")
        sections = instruction_sections()
        tracker.start(config_from_sections(TOOLS, sections), sections)
        self.assertTrue(tracker.active)
        self.assertIsNone(tracker.voice_ratio)
        self.assertEqual([item.name for item in tracker.items(VOICE)], ["instructions"])

    def test_tokens_are_about_four_bytes_each_and_shown_short(self):
        self.assertEqual(estimate_tokens(""), 0)
        self.assertEqual(estimate_tokens("abcde"), 2)
        self.assertEqual(estimate_tokens("é" * 4), 2)
        self.assertEqual(short_tokens(850), "850")
        self.assertEqual(short_tokens(4130), "4.1k")


class LiveRecordsWhatItSendsTests(unittest.IsolatedAsyncioTestCase):
    def ready_live(self):
        live = CoraLive()
        live.connection = MagicMock()
        live.connection.session.commentary.append = AsyncMock()
        live.connection.session.thinking.append = AsyncMock()
        live.connection.response.item.create = AsyncMock()
        live.connection.response.create = AsyncMock()
        live.ready = True
        return live

    async def test_each_note_is_recorded_exactly_as_sent(self):
        live = self.ready_live()
        await live.send_greeting("It's Friday.")
        await live.send_welcome_back()
        sent = [call.kwargs["content"] for call in live.connection.session.commentary.append.await_args_list]
        self.assertEqual(len(sent), 2)
        self.assertEqual(live.context.conversation, "\n\n".join(f"[note] {content}" for content in sent))

    async def test_a_typed_message_is_mirrored_to_voice_and_sent_to_the_backend(self):
        live = self.ready_live()
        await live.send_text("Hi Cora", image_seen="A cat.")
        mirror = live.connection.session.thinking.append.await_args.kwargs["content"]
        self.assertEqual(live.context.conversation, f"[note] {mirror}")
        sent = live.connection.response.item.create.await_args.kwargs["item"]["content"]
        self.assertEqual(live.context.typed_messages, ["\n\n".join(part["text"] for part in sent)])
        self.assertTrue(live.context.typed_messages[0].startswith("Hi Cora\n\nPasted image"))


if __name__ == "__main__":
    unittest.main()
