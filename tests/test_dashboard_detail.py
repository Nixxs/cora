"""Clicking a feeling, an interest or a finding on the dashboard opens it in full. Mocked storage only."""

import asyncio
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import MagicMock, patch

from textual.widgets import Label, TextArea

from storage.dashboard_snapshot import DashboardSnapshot, interest_details
from subconscious.emotions.feeling_rules import Reaction
from ui.context_view import DetailScreen
from ui.meters import Openable, ReadableMeters
import test_keyboard_persistence as keyboard_tests

LONG = "Because the tide turned " + "and kept turning " * 40 + "at last."


def snapshot(now, **fields):
    base = dict(
        relationship=None, history=(), tones=(), memory_kinds={}, memory_days=(), memory_pending={},
        interests=(), latest_finding=None, last_runs={}, next_due={}, message_days=(), traits=(),
    )
    return DashboardSnapshot(**(base | fields))


class DetailTests(unittest.IsolatedAsyncioTestCase):
    async def open_dashboard(self, app, pilot, shown):
        app.persistence.load_dashboard_snapshot.return_value = shown
        await pilot.click("#view-button")
        await pilot.pause()
        await asyncio.wait_for(app.screen.workers.wait_for_complete(), 3)
        await pilot.pause()
        return app.screen

    async def detail(self, app, pilot):
        await pilot.pause()
        self.assertIsInstance(app.screen, DetailScreen)
        title = str(app.screen.query_one("#context-view-title", Label).render())
        return title, app.screen.query_one("#context-view-text", TextArea).text

    async def test_her_latest_feeling_opens_with_its_whole_reason(self):
        now = datetime.now(timezone.utc)
        reaction = Reaction("hurt", "strong", now - timedelta(minutes=5), LONG)
        async with keyboard_tests.running_app() as (app, pilot):
            screen = await self.open_dashboard(app, pilot, snapshot(now, latest_feeling=reaction))
            latest = screen.query_one("#feeling-latest", Openable)
            self.assertTrue(latest.has_class("openable"))
            latest.on_click(MagicMock())
            title, text = await self.detail(app, pilot)
            self.assertIn("(strong) · 5m ago", title)
            self.assertEqual(text, LONG)
            await pilot.press("escape")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, DetailScreen)

    async def test_nothing_to_open_without_a_feeling_or_a_finding(self):
        async with keyboard_tests.running_app() as (app, pilot):
            screen = await self.open_dashboard(app, pilot, snapshot(datetime.now(timezone.utc)))
            for widget_id in ("#feeling-latest", "#interest-finding"):
                widget = screen.query_one(widget_id, Openable)
                self.assertFalse(widget.has_class("openable"))
                widget.on_click(MagicMock())
                await pilot.pause()
                self.assertNotIsInstance(app.screen, DetailScreen)

    async def test_an_interest_row_and_the_latest_finding_open_in_full(self):
        now = datetime.now(timezone.utc)
        shown = snapshot(now, interests=(("Tides", 0.6, True), ("Owls", 0.3, False)),
                         latest_finding=("Tides", LONG),
                         interest_details={"Owls": "Night birds.\n\nHow it emerged: a talk about a barn."})
        async with keyboard_tests.running_app() as (app, pilot):
            screen = await self.open_dashboard(app, pilot, shown)
            meters = screen.query_one("#interest-strengths", ReadableMeters)
            meters.on_click(MagicMock(y=1))
            title, text = await self.detail(app, pilot)
            self.assertEqual(title, "Interest · Owls · 30% fading")
            self.assertIn("a talk about a barn", text)
            await pilot.press("escape")
            await pilot.pause()
            screen.query_one("#interest-finding", Openable).on_click(MagicMock())
            title, text = await self.detail(app, pilot)
            self.assertEqual(title, "Latest finding · Tides")
            self.assertEqual(text, LONG)


class InterestDetailsTests(unittest.TestCase):
    def test_each_live_interest_has_what_it_is_how_it_emerged_and_its_latest_finding(self):
        now = datetime(2026, 10, 20, tzinfo=timezone.utc)
        interests = [
            MagicMock(id=1, title="Tides", description="Why the sea moves.", why_emerged="A beach walk.",
                      strength=0.8, strength_at=now, status="active", last_reinforced_at=now, created_at=now,
                      origin="person"),
            MagicMock(id=2, title="Owls", description="Night birds.", why_emerged="A barn.",
                      strength=0.5, strength_at=now, status="active", last_reinforced_at=now, created_at=now,
                      origin="person"),
        ]
        with patch("storage.dashboard_snapshot.InterestStore") as store:
            store.return_value.list_interests.return_value = interests
            store.return_value.latest_findings.return_value = {1: ("Spring tides follow the moon.", now)}
            details = interest_details("engine", now)
        self.assertEqual(details["Tides"], "Why the sea moves.\n\nHow it emerged: A beach walk.\n\n"
                                           "Latest finding (20 Oct 2026): Spring tides follow the moon.")
        self.assertEqual(details["Owls"], "Night birds.\n\nHow it emerged: A barn.")


if __name__ == "__main__":
    unittest.main()
