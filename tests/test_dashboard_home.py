"""The dashboard's home box: each room's readings and air conditioner, from what the app holds."""

import asyncio
from dataclasses import replace
from datetime import timedelta
import unittest

from textual.widgets import Static

from home.sensibo import parse_rooms
from ui.dashboard import COLD_STYLE, HOT_STYLE, OFFLINE_STYLE, DashboardScreen, room_lines
import test_home
from test_home import NOW, ROOMS, pod
import test_keyboard_persistence as keyboard_tests


def styles_of(text, word):
    """The styles on the span of `text` that covers `word`."""
    start = text.plain.index(word)
    return {str(span.style) for span in text.spans if span.start <= start < span.end}


class RoomLinesTests(unittest.TestCase):
    def test_each_room_with_hers_marked_and_what_its_unit_is_doing(self):
        office, declan = parse_rooms({"result": [
            pod(on=True, targetTemperature=23, fanLevel="low", temperature=24.5, humidity=55),
            pod("BpP9b5Ka", "Declan’s Room", temperature=21.2, humidity=86),
        ]})
        text = room_lines([office, declan], "Jyt7jM8c", NOW)
        lines = text.plain.splitlines()
        self.assertEqual(lines[0], "◆ Office · her room")
        self.assertEqual(lines[1], "  24.5°C  55% · just now")
        self.assertEqual(lines[2], "  cooling to 23° · fan low")
        self.assertEqual(lines[4], "Declan’s Room")
        self.assertEqual(lines[6], "  off")

    def test_heat_cold_offline_timers_and_feels_like(self):
        hot, cold, gone = parse_rooms({"result": [
            pod(temperature=31, mode="heat", on=True), pod("Cold1234", "Garage", temperature=14),
            pod("Gone1234", "Shed", alive=False),
        ]})
        hot = replace(hot, feels_like=33.0, timer_at=NOW + timedelta(hours=2), timer_on=False)
        text = room_lines([hot, cold, gone], "", NOW)
        self.assertIn(HOT_STYLE, styles_of(text, "31°C"))
        self.assertIn(COLD_STYLE, styles_of(text, "14°C"))
        self.assertIn(OFFLINE_STYLE, styles_of(text, "offline"))
        self.assertIn("heating to 22° · fan auto · off in 2h", text.plain)
        self.assertIn("feels 33°", text.plain)
        self.assertNotIn("◆", text.plain)
        # A timer that's already gone off isn't shown.
        self.assertNotIn(" · on ", room_lines([replace(cold, timer_at=NOW - timedelta(minutes=1))], "", NOW).plain)

    def test_no_rooms(self):
        self.assertEqual(room_lines([], "", NOW).plain, "no rooms on the account")


class AppViewTests(unittest.IsolatedAsyncioTestCase):
    ready = test_home.AppTests.ready

    async def test_the_view_is_from_memory_and_follows_every_rooms_measurements(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.assertIsNone(app.home_view())
            self.ready(app)
            self.assertEqual(app.home_view(), (None, "Jyt7jM8c", ""))
            await app.update_her_room()
            rooms, mine, problem = app.home_view()
            self.assertEqual([room.name for room in rooms], ["Office", "Declan’s Room"])
            calls = app.sensibo.rooms.await_count
            await app.on_home_event("measurements", {"id": "BpP9b5Ka", "measurements": {"temperature": 25.5}})
            await app.on_home_event("measurements", {"id": "Jyt7jM8c", "measurements": {"humidity": 60}})
            rooms, _, _ = app.home_view()
            self.assertEqual((rooms[1].temperature, rooms[0].humidity), (25.5, 60.0))
            self.assertIn("60%", app.live.room_context)
            # Nothing asked of Sensibo to show it.
            self.assertEqual(app.sensibo.rooms.await_count, calls)
            await app.on_home_event("measurements", {"id": "Unknown1", "measurements": {"temperature": 40}})
            self.assertEqual(len(app.home_view()[0]), 2)


class DashboardHomeTests(unittest.IsolatedAsyncioTestCase):
    ready = test_home.AppTests.ready

    async def open(self, app, pilot):
        await pilot.click("#view-button")
        await pilot.pause()
        self.assertIsInstance(app.screen, DashboardScreen)
        return app.screen

    async def test_left_out_until_set_up_then_shown_with_each_room(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.dashboard_minimised = ["home-box"]
            screen = await self.open(app, pilot)
            await pilot.pause()
            box = screen.query_one("#home-box")
            self.assertFalse(box.display)
            # Minimised while it isn't set up: no chip for it either.
            self.assertEqual(len(screen.query(".box-chip")), 0)
            self.ready(app)
            await app.update_her_room()
            screen.show_home()
            await pilot.pause()
            self.assertFalse(box.display)
            self.assertEqual([chip.box_id for chip in screen.query(".box-chip")], ["home-box"])
            screen.restore_box("home-box")
            await pilot.pause()
            await pilot.pause()
            self.assertTrue(box.display)
            self.assertGreaterEqual(box.outer_size.width, 30)
            self.assertIn("◆ Office · her room", str(screen.query_one("#home-rooms", Static).render()))
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            app.persistence.save_dashboard_minimised.assert_called_with([])
            # Switched off: gone again, and the rest of the row closes up.
            app.sensibo_enabled = False
            screen.show_home()
            await pilot.pause()
            await pilot.pause()
            self.assertFalse(box.display)
            self.assertIn("home-box", screen.absent)

    async def test_before_the_first_reading_and_after_a_problem(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.ready(app)
            screen = await self.open(app, pilot)
            rooms = screen.query_one("#home-rooms", Static)
            self.assertEqual(str(rooms.render()), "reading…")
            app.home_state, app.home_status = "problem", "Sensibo is limiting requests just now."
            screen.show_home()
            self.assertEqual(str(rooms.render()), "Sensibo is limiting requests just now.")
            await app.update_her_room()
            app.home_state, app.home_status = "problem", "Sensibo answered 500."
            screen.show_home()
            # The last good reading stays, marked as such.
            self.assertIn("Office", str(rooms.render()))
            self.assertEqual(screen.query_one("#home-box").border_subtitle, "last reading")


if __name__ == "__main__":
    unittest.main()
