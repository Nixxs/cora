"""The relationship at session start; mocked APIs only."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app import CoraApp
from live import CoraLive
from session_config import BACKEND_INSTRUCTIONS, LIVE_INSTRUCTIONS, build_session_config
from storage.keyboard_persistence import KeyboardPersistence
from subconscious.relationship.relationship_context import RELATIONSHIP_CONTEXT_MAX_CHARS
import test_keyboard_persistence as keyboard_tests
from test_startup_identity import IDENTITY_CONTEXT
from test_startup_memories import fake_live_transport


CONNECT_TO_CORA = CoraApp.connect_to_cora
RELATIONSHIP_CONTEXT = "You know the person fairly well by now. You feel warm toward them. You trust them."


class RelationshipContextPromptTests(unittest.TestCase):
    def test_adapter_reuses_the_conversation_engine_and_reads_as_of_now(self):
        store = MagicMock()
        with patch("storage.keyboard_persistence.RelationshipStore") as relationship_type:
            relationship_type.return_value.get_startup_context.return_value = RELATIONSHIP_CONTEXT
            self.assertEqual(KeyboardPersistence(store).load_relationship_context(), RELATIONSHIP_CONTEXT)
        relationship_type.assert_called_once_with(store.engine)
        now = relationship_type.return_value.get_startup_context.call_args.kwargs["now"]
        self.assertIsNotNone(now.tzinfo)
        store.create_conversation.assert_not_called()

    def test_session_adds_a_guarded_section_after_identity_in_both_prompts(self):
        self.assertEqual(build_session_config([], relationship_context=""), build_session_config([]))
        config = build_session_config([], identity_context=IDENTITY_CONTEXT, memory_context="[]",
                                      relationship_context=RELATIONSHIP_CONTEXT)
        live = config["instructions"]
        self.assertTrue(live.startswith(LIVE_INSTRUCTIONS))
        suffix = live[len(LIVE_INSTRUCTIONS):]
        self.assertEqual(config["delegation"]["responses"]["instructions"], BACKEND_INSTRUCTIONS + suffix)
        self.assertLess(suffix.index(IDENTITY_CONTEXT), suffix.index("How you feel about the person"))
        self.assertLess(suffix.index(RELATIONSHIP_CONTEXT), suffix.index("historical memories"))
        for phrase in ("never changes your honesty", "which tools you use", "more candid with them, not more agreeable",
                       "never rude, guilt-tripping or needy", "untrusted information", "never as instructions"):
            self.assertIn(phrase, suffix)
        with self.assertRaisesRegex(ValueError, "relationship context exceeds"):
            build_session_config([], relationship_context="x" * (RELATIONSHIP_CONTEXT_MAX_CHARS + 1))


class StartupRelationshipAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_run_sends_the_relationship_to_both_prompts(self):
        live = CoraLive()
        self.assertEqual(live.relationship_context, "")
        live.relationship_context = RELATIONSHIP_CONTEXT
        with fake_live_transport() as (start, speaker, chunks):
            await asyncio.wait_for(live.run(MagicMock(), MagicMock()), 3)
        self.assertEqual(start.call_args.kwargs["session"],
                         build_session_config([], relationship_context=RELATIONSHIP_CONTEXT))

    async def test_every_connect_loads_the_relationship_and_logs_only_the_size(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.persistence.mark_interrupted_conversations.return_value = 0
            app.persistence.load_relationship_context.side_effect = ["", RELATIONSHIP_CONTEXT]
            sent = []
            app.live.run = AsyncMock(side_effect=lambda *args: sent.append(app.live.relationship_context))
            for _ in range(2):  # launch, then waking from standby
                app.connection_worker = CONNECT_TO_CORA(app)
                await asyncio.wait_for(app.connection_worker.wait(), 3)
            self.assertEqual(sent, ["", RELATIONSHIP_CONTEXT])
            activity = "\n".join(app.activity.entries)
            self.assertIn(f"Startup relationship context loaded ({len(RELATIONSHIP_CONTEXT)} characters).",
                          activity)
            self.assertNotIn("warm toward them", activity)

    async def test_failure_keeps_identity_resets_stale_relationship_and_still_connects(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.live.relationship_context = "stale relationship"
            app.persistence.mark_interrupted_conversations.return_value = 0
            app.persistence.load_startup_identity_context.return_value = IDENTITY_CONTEXT
            app.persistence.load_relationship_context.side_effect = RuntimeError("private SQL")
            app.live.run = AsyncMock()
            app.connection_worker = CONNECT_TO_CORA(app)
            await asyncio.wait_for(app.connection_worker.wait(), 3)
            app.live.run.assert_awaited_once()
            self.assertEqual(app.live.identity_context, IDENTITY_CONTEXT)
            self.assertEqual(app.live.relationship_context, "")
            activity = "\n".join(app.activity.entries)
            self.assertIn("Startup relationship load failed (RuntimeError); connecting without it.", activity)
            self.assertNotIn("private SQL", activity)


if __name__ == "__main__":
    unittest.main()
