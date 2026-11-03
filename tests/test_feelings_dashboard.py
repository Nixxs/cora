"""The dashboard's Feelings box: a line per feeling over the last hours, pulsing on a jump, and the latest reason."""

import asyncio
from datetime import datetime, timedelta, timezone
import os
import unittest

from textual.widgets import Static

from storage.dashboard_snapshot import (
    FEELING_JUMP_WINDOW, FEELING_SPAN, FEELING_STEP, DashboardSnapshot, recent_feelings,
)
from subconscious.emotions.feeling_rules import FEELINGS, HALF_LIFE, Reaction, level_history
from subconscious.emotions.feeling_store import FeelingStore
from ui.braille import BrailleGraph
from ui.dashboard import BOX_TITLES
import test_conversation_summaries as summary_tests
import test_keyboard_persistence as keyboard_tests

NOW = datetime(2026, 11, 3, 10, 30, tzinfo=timezone.utc)
POINTS = int(FEELING_SPAN / FEELING_STEP) + 1


def snapshot(feelings=(), latest=None):
    return DashboardSnapshot(
        relationship=None, history=(), tones=(), memory_kinds={}, memory_days=(), memory_pending={}, interests=(),
        latest_finding=None, last_runs={}, next_due={}, message_days=(), traits=(), feelings=feelings,
        latest_feeling=latest,
    )


class HistoryTests(unittest.TestCase):
    def test_levels_every_step_ending_now_counting_what_came_before(self):
        history = level_history([Reaction("hurt", "strong", NOW - FEELING_SPAN - HALF_LIFE)], NOW,
                                FEELING_SPAN, FEELING_STEP)
        self.assertEqual(list(history), list(FEELINGS))
        hurt = history["hurt"]
        self.assertEqual(len(hurt), POINTS)
        # Reacted half an hour before the span: already halved at its start, and still fading.
        self.assertAlmostEqual(hurt[0], 0.15)
        self.assertTrue(all(earlier > later for earlier, later in zip(hurt, hurt[1:])))
        self.assertEqual(set(history["excitement"]), {0.0})


class BoxTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_line_per_feeling_pulsing_on_a_jump_with_the_latest_reason(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await pilot.click("#view-button")
            await pilot.pause()
            screen = app.screen
            await asyncio.wait_for(screen.workers.wait_for_complete(), 3)
            self.assertEqual(BOX_TITLES["feelings-box"], "feelings")
            self.assertEqual(screen.query_one("#feelings-box").border_title, "feelings")
            latest = screen.query_one("#feeling-latest", Static)
            self.assertEqual(str(latest.render()), "nothing felt lately")
            names = [str(screen.query_one(f"#{feeling}-name", Static).render()) for feeling in FEELINGS]
            self.assertEqual(names, ["excited", "content", "curious", "fulfilled", "irritated", "hurt"])
            hurt = (0.0,) * (POINTS - 1) + (0.3,)
            curious = (0.1,) + (0.0,) * (POINTS - 1)
            feelings = tuple((feeling, {"hurt": hurt, "curiosity": curious}.get(feeling, (0.0,) * POINTS),
                              feeling == "hurt") for feeling in FEELINGS)
            app.persistence.load_dashboard_snapshot.return_value = snapshot(
                feelings, Reaction("hurt", "strong", datetime.now(timezone.utc) - timedelta(minutes=5),
                                   "told I [b]don't[/b] care"))
            await screen.refresh_snapshot().wait()
            await pilot.pause()
            graphs = {feeling: screen.query_one(f"#{feeling}-feeling", BrailleGraph) for feeling in FEELINGS}
            self.assertEqual(graphs["hurt"].values, hurt)
            self.assertTrue(graphs["hurt"].pulse)
            self.assertFalse(graphs["curiosity"].pulse)
            # Bright only while it is still felt.
            self.assertTrue(screen.query_one("#hurt-name").has_class("feeling-felt"))
            self.assertFalse(screen.query_one("#curiosity-name").has_class("feeling-felt"))
            # Brackets in a reason are shown as written, not read as markup.
            self.assertEqual(str(latest.render()).splitlines(), ["hurt (strong) · 5m ago", "told I [b]don't[/b] care"])


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class SnapshotDatabaseTests(unittest.TestCase):
    setUpClass = classmethod(summary_tests.SummaryDatabaseTests.setUpClass.__func__)

    def setUp(self):
        from sqlalchemy import text
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM public.feeling_events"))
        summary_tests.SummaryDatabaseTests.setUp(self)
        self.feelings = FeelingStore(self.engine)

    def test_recent_levels_jumps_and_the_newest_reaction(self):
        self.assertEqual(recent_feelings(self.engine, NOW)[1], None)
        self.feelings.record("curiosity", "small", "the moss article", now=NOW - FEELING_SPAN - HALF_LIFE)
        self.feelings.record("excitement", "medium", "a new idea", now=NOW - FEELING_JUMP_WINDOW * 3)
        self.feelings.record("hurt", "strong", "told I don't care", now=NOW - FEELING_JUMP_WINDOW / 2)
        feelings, latest = recent_feelings(self.engine, NOW)
        self.assertEqual([feeling for feeling, _, _ in feelings], list(FEELINGS))
        by_name = {feeling: (levels, jumped) for feeling, levels, jumped in feelings}
        self.assertEqual({name for name, (_, jumped) in by_name.items() if jumped}, {"hurt"})
        # From before the span: counted at its start, but not the latest shown.
        self.assertAlmostEqual(by_name["curiosity"][0][0], 0.05)
        self.assertEqual(len(by_name["hurt"][0]), POINTS)
        self.assertEqual((latest.feeling, latest.reason), ("hurt", "told I don't care"))
        only_old = recent_feelings(self.engine, NOW + FEELING_SPAN)[1]
        self.assertIsNone(only_old)


if __name__ == "__main__":
    unittest.main()
