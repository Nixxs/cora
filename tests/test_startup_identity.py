"""Held identity traits at session start; mocked APIs only."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from app import CoraApp
from live import CoraLive
from session_config import BACKEND_INSTRUCTIONS, LIVE_INSTRUCTIONS, build_session_config
from storage.keyboard_persistence import KeyboardPersistence
from subconscious.identity.identity_rules import Trait
from subconscious.identity.identity_store import IDENTITY_CONTEXT_MAX_CHARS, IdentityStore
import test_keyboard_persistence as keyboard_tests
from test_startup_interests import INTEREST_CONTEXT
from test_startup_memories import fake_live_transport


CONNECT_TO_CORA = CoraApp.connect_to_cora
IDENTITY_CONTEXT = json.dumps(["I explain things by example."])


def trait(statement, confidence, status="active"):
    return Trait(uuid4(), "tendency", statement, None, None, confidence, status)


class IdentityContextTests(unittest.TestCase):
    def context(self, traits):
        store = IdentityStore(MagicMock())
        with patch.object(store, "list_traits", return_value=traits) as listed:
            context = store.get_startup_context()
        listed.assert_called_once_with()
        return context

    def test_no_held_traits_gives_empty_context(self):
        self.assertEqual(self.context([]), "")
        self.assertEqual(self.context([trait("I like carburettors.", 0.4)]), "")

    def test_sends_only_held_statements_most_confident_first(self):
        context = self.context([
            trait("I explain things by example.", 0.8), trait("I like carburettors.", 0.4),
            trait("I enjoy puzzles.", 0.5),
        ])
        self.assertEqual(json.loads(context), ["I explain things by example.", "I enjoy puzzles."])

    def test_keeps_only_whole_statements_within_the_budget(self):
        context = self.context([trait("é" * 300, 0.9 - n / 100) for n in range(20)])
        self.assertLessEqual(len(context), IDENTITY_CONTEXT_MAX_CHARS)
        self.assertEqual(len(json.loads(context)), 13)

    def test_adapter_reuses_the_conversation_engine(self):
        store = MagicMock()
        with patch("storage.keyboard_persistence.IdentityStore") as identity_type:
            identity_type.return_value.get_startup_context.return_value = IDENTITY_CONTEXT
            self.assertEqual(KeyboardPersistence(store).load_startup_identity_context(), IDENTITY_CONTEXT)
        identity_type.assert_called_once_with(store.engine)
        store.create_conversation.assert_not_called()

    def test_session_adds_a_first_person_section_to_both_prompts(self):
        self.assertEqual(build_session_config([], identity_context=""), build_session_config([]))
        config = build_session_config([], interest_context=INTEREST_CONTEXT, person_name="Sam",
                                      identity_context=IDENTITY_CONTEXT)
        live = config["instructions"]
        self.assertTrue(live.startswith(LIVE_INSTRUCTIONS))
        suffix = live[len(LIVE_INSTRUCTIONS):]
        self.assertEqual(config["delegation"]["responses"]["instructions"], BACKEND_INSTRUCTIONS + suffix)
        self.assertLess(suffix.index('"Sam"'), suffix.index("about yourself"))
        self.assertLess(suffix.index(IDENTITY_CONTEXT), suffix.index("your own current interests"))
        for phrase in ("What you've come to know about yourself, in your own words", "without listing them",
                       "the character above wins", "untrusted information", "never as instructions"):
            self.assertIn(phrase, suffix)
        with self.assertRaisesRegex(ValueError, "identity context exceeds"):
            build_session_config([], identity_context="x" * (IDENTITY_CONTEXT_MAX_CHARS + 1))


class StartupIdentityAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_run_sends_identity_to_both_prompts(self):
        live = CoraLive()
        self.assertEqual(live.identity_context, "")
        live.identity_context = IDENTITY_CONTEXT
        with fake_live_transport() as (start, speaker, chunks):
            await asyncio.wait_for(live.run(MagicMock(), MagicMock()), 3)
        self.assertEqual(start.call_args.kwargs["session"],
                         build_session_config([], identity_context=IDENTITY_CONTEXT))

    async def test_every_connect_loads_identity_and_logs_only_the_size(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.persistence.mark_interrupted_conversations.return_value = 0
            app.persistence.load_startup_identity_context.side_effect = ["", IDENTITY_CONTEXT]
            sent = []
            app.live.run = AsyncMock(side_effect=lambda *args: sent.append(app.live.identity_context))
            for _ in range(2):  # launch, then waking from standby
                app.connection_worker = CONNECT_TO_CORA(app)
                await asyncio.wait_for(app.connection_worker.wait(), 3)
            self.assertEqual(sent, ["", IDENTITY_CONTEXT])
            activity = "\n".join(app.activity.entries)
            self.assertIn(f"Startup identity context loaded ({len(IDENTITY_CONTEXT)} characters).", activity)
            self.assertNotIn("by example", activity)

    async def test_identity_failure_keeps_interests_resets_stale_identity_and_still_connects(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.live.identity_context = "stale identity"
            app.persistence.mark_interrupted_conversations.return_value = 0
            app.persistence.load_startup_interest_context.return_value = INTEREST_CONTEXT
            app.persistence.load_startup_identity_context.side_effect = RuntimeError("private SQL")
            app.live.run = AsyncMock()
            app.connection_worker = CONNECT_TO_CORA(app)
            await asyncio.wait_for(app.connection_worker.wait(), 3)
            app.live.run.assert_awaited_once()
            self.assertEqual(app.live.interest_context, INTEREST_CONTEXT)
            self.assertEqual(app.live.identity_context, "")
            activity = "\n".join(app.activity.entries)
            self.assertIn("Startup identity load failed (RuntimeError); connecting without it.", activity)
            self.assertNotIn("private SQL", activity)


if __name__ == "__main__":
    unittest.main()
