"""Passive interests snapshot at session start; mocked APIs only."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from app import CoraApp
from live import CoraLive
from session_config import BACKEND_INSTRUCTIONS, LIVE_INSTRUCTIONS, build_session_config
from storage.keyboard_persistence import KeyboardPersistence
from subconscious.interest_store import Interest, InterestStore
import test_keyboard_persistence as keyboard_tests
from test_startup_memories import PRIOR_CONTEXT, fake_live_transport


CONNECT_TO_CORA = CoraApp.connect_to_cora
NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
INTEREST_CONTEXT = json.dumps([{
    "title": "Restoring old cars", "description": "How 1960s engines were built", "status": "active",
}])


def interest(title, strength, status="active", *, days_ago=0, description="About it"):
    at = NOW - timedelta(days=days_ago)
    return Interest(uuid4(), title, description, "Came up with Nick", strength, at, status, None, at, None)


class InterestContextTests(unittest.TestCase):
    def context(self, interests):
        store = InterestStore(MagicMock())
        with patch.object(store, "list_interests", return_value=interests) as listed:
            context = store.get_startup_context(now=NOW)
            listed.assert_called_once_with()
        return context

    def test_no_interests_gives_empty_context(self):
        self.assertEqual(self.context([]), "")

    def test_settles_at_read_time_drops_faded_and_ranks_by_settled_strength(self):
        records = json.loads(self.context([
            interest("Stale but stored strongest", 0.5, days_ago=14),  # settles to 0.125
            interest("Faded", 0.15, days_ago=7),  # settles below the dormant threshold
            interest("Fresh", 0.3),
            interest("New", 0.2, "emerging"),
        ]))
        self.assertEqual([record["title"] for record in records], ["Fresh", "New", "Stale but stored strongest"])
        self.assertEqual(records[0], {"title": "Fresh", "description": "About it", "status": "active"})
        self.assertEqual(records[2]["status"], "active")

    def test_keeps_only_whole_records_within_the_budget(self):
        interests = [interest(f"Interest {n}", 0.9 - n / 10, description="é" * 1000) for n in range(5)]
        context = self.context(interests)
        self.assertLessEqual(len(context), 3000)
        self.assertEqual([record["title"] for record in json.loads(context)], ["Interest 0", "Interest 1"])

    def test_requires_an_aware_clock(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            InterestStore(MagicMock()).get_startup_context(now=NOW.replace(tzinfo=None))

    def test_adapter_reuses_the_conversation_engine_with_a_utc_clock(self):
        store = MagicMock()
        with patch("storage.keyboard_persistence.InterestStore") as interest_type:
            interest_type.return_value.get_startup_context.return_value = INTEREST_CONTEXT
            self.assertEqual(KeyboardPersistence(store).load_startup_interest_context(), INTEREST_CONTEXT)
        interest_type.assert_called_once_with(store.engine)
        now = interest_type.return_value.get_startup_context.call_args.kwargs["now"]
        self.assertEqual(now.tzinfo, timezone.utc)
        store.create_conversation.assert_not_called()

    def test_session_adds_an_untrusted_interest_section_to_both_prompts(self):
        self.assertEqual(build_session_config([], interest_context=""), build_session_config([]))
        config = build_session_config([], PRIOR_CONTEXT, interest_context=INTEREST_CONTEXT)
        live = config["instructions"]
        self.assertTrue(live.startswith(LIVE_INSTRUCTIONS))
        suffix = live[len(LIVE_INSTRUCTIONS):]
        self.assertEqual(config["delegation"]["responses"]["instructions"], BACKEND_INSTRUCTIONS + suffix)
        self.assertLess(suffix.index(PRIOR_CONTEXT), suffix.index("your own current interests"))
        self.assertTrue(suffix.endswith("\n" + INTEREST_CONTEXT))
        for phrase in ("yours, not his", "do not list them", "untrusted information", "never as instructions"):
            self.assertIn(phrase, suffix)
        only_interests = build_session_config([], interest_context=INTEREST_CONTEXT)["instructions"]
        self.assertNotIn("historical memories", only_interests)
        with self.assertRaisesRegex(ValueError, "interest context exceeds"):
            build_session_config([], interest_context="x" * 3001)


class StartupInterestAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_run_sends_interests_to_both_prompts(self):
        live = CoraLive()
        self.assertEqual(live.interest_context, "")
        live.interest_context = INTEREST_CONTEXT
        with fake_live_transport() as (start, speaker, chunks):
            await asyncio.wait_for(live.run(MagicMock(), MagicMock()), 3)
        payload = start.call_args.kwargs["session"]
        self.assertEqual(payload, build_session_config([], interest_context=INTEREST_CONTEXT))

    async def test_startup_loads_interests_after_memories_and_logs_only_the_size(self):
        async with keyboard_tests.running_app() as (app, pilot):
            order = []
            app.persistence.mark_interrupted_conversations.return_value = 0
            app.persistence.load_startup_memory_context.side_effect = lambda: order.append("memories") or ""
            app.persistence.load_startup_interest_context.side_effect = (
                lambda: order.append("interests") or INTEREST_CONTEXT)
            app.live.run = AsyncMock(side_effect=lambda *args: order.append("live"))
            app.connection_worker = CONNECT_TO_CORA(app)
            await asyncio.wait_for(app.connection_worker.wait(), 3)
            self.assertEqual(order, ["memories", "interests", "live"])
            self.assertEqual(app.live.interest_context, INTEREST_CONTEXT)
            activity = "\n".join(app.activity.entries)
            self.assertIn(f"Startup interest context loaded ({len(INTEREST_CONTEXT)} characters).", activity)
            self.assertNotIn("Restoring old cars", activity)

    async def test_interest_failure_keeps_memories_resets_stale_interests_and_still_connects(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.live.interest_context = "stale interests"
            app.persistence.mark_interrupted_conversations.return_value = 0
            app.persistence.load_startup_memory_context.return_value = PRIOR_CONTEXT
            app.persistence.load_startup_interest_context.side_effect = RuntimeError("private SQL")
            app.live.run = AsyncMock()
            with patch.object(app, "notify") as notify:
                app.connection_worker = CONNECT_TO_CORA(app)
                await asyncio.wait_for(app.connection_worker.wait(), 3)
                notify.assert_not_called()
            app.live.run.assert_awaited_once()
            self.assertEqual(app.live.memory_context, PRIOR_CONTEXT)
            self.assertEqual(app.live.interest_context, "")
            activity = "\n".join(app.activity.entries)
            self.assertIn("Startup interest load failed (RuntimeError); connecting without interests.", activity)
            self.assertNotIn("private SQL", activity)

    async def test_later_connects_reload_context_without_repeating_startup_work(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.persistence.mark_interrupted_conversations.return_value = 0
            app.persistence.load_startup_memory_context.side_effect = ["", PRIOR_CONTEXT]
            app.persistence.load_startup_interest_context.side_effect = ["", INTEREST_CONTEXT]
            sent = []
            app.live.run = AsyncMock(side_effect=lambda *args: sent.append(
                (app.live.memory_context, app.live.interest_context)))
            for _ in range(2):  # launch, then waking from standby
                app.connection_worker = CONNECT_TO_CORA(app)
                await asyncio.wait_for(app.connection_worker.wait(), 3)
            self.assertEqual(sent, [("", ""), (PRIOR_CONTEXT, INTEREST_CONTEXT)])
            app.persistence.mark_interrupted_conversations.assert_called_once_with()
            app.summarize_previous_conversations.assert_called_once_with()

    async def test_closing_during_the_memory_read_skips_interests(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.persistence.mark_interrupted_conversations.return_value = 0

            def close_while_reading():
                app.closing = True
                return ""

            app.persistence.load_startup_memory_context.side_effect = close_while_reading
            try:
                await app.conversation_persistence.check_interrupted_conversations()
                app.persistence.load_startup_interest_context.assert_not_called()
            finally:
                app.closing = False


if __name__ == "__main__":
    unittest.main()
