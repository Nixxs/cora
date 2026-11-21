"""Her home, step 4b-ii: at bedtime, an air conditioner still on in a ticked room is raised: in a word
if she's awake, on Signal if she's in standby."""

import asyncio
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app_areas.home import left_on, names_of, seconds_until
from home.sensibo import parse_rooms
from storage.app_settings_store import AppSettingsStore
import test_home
from test_home import pod
import test_keyboard_persistence as keyboard_tests

NOW = datetime(2026, 11, 21, 14, 0, tzinfo=timezone.utc)


def rooms_of(*pods):
    return parse_rooms({"result": list(pods)})


class TimingTests(unittest.TestCase):
    def test_the_next_bedtime_is_today_or_tomorrow(self):
        evening = datetime(2026, 11, 21, 20, 0)
        self.assertEqual(seconds_until("22:30", evening), 2.5 * 3600)
        self.assertEqual(seconds_until("20:00", evening), 24 * 3600)
        self.assertEqual(seconds_until("07:00", evening), 11 * 3600)


class LeftOnTests(unittest.TestCase):
    def test_only_ticked_rooms_on_and_meant_to_stop(self):
        rooms = rooms_of(
            pod("Office1", "Office", on=True),
            pod("Declan1", "Declan’s Room", on=True),
            pod("Lounge1", "Lounge", on=False),
            pod("Gone1", "Shed", on=True, alive=False),
            pod("Timed1", "Study", on=True) | {"timer": {"targetTime": "2026-11-21T15:00:00", "acState": {"on": False}}},
            pod("Kept1", "Bedroom", on=True) | {"smartMode": {"enabled": True, "lowTemperatureThreshold": 22,
                                                              "highTemperatureThreshold": 25}},
        )
        ticked = ["Office1", "Lounge1", "Gone1", "Timed1", "Kept1"]
        self.assertEqual([room.name for room in left_on(rooms, ticked, NOW)], ["Office"])
        self.assertEqual(names_of(rooms[:2]), "Office (cool, 22°C, fan auto) and Declan’s Room (cool, 22°C, fan auto)")


class StoreTests(unittest.TestCase):
    def test_the_time_and_rooms_are_saved_together_and_cleared_without_a_time(self):
        store = AppSettingsStore(MagicMock())
        with patch.object(store, "_get", return_value="22:30|Jyt7jM8c,BpP9b5Ka"):
            self.assertEqual(store.bedtime(), ("22:30", ["Jyt7jM8c", "BpP9b5Ka"]))
        with patch.object(store, "_get", return_value="25:00|Jyt7jM8c"):
            self.assertEqual(store.bedtime(), ("", []))
        with patch.object(store, "_set") as saved:
            store.set_bedtime("22:30", ["Jyt7jM8c"])
            saved.assert_called_once_with("bedtime", "22:30|Jyt7jM8c")
            with self.assertRaises(ValueError):
                store.set_bedtime("10pm", [])
        with patch.object(store, "_set") as saved, patch("storage.app_settings_store.Session") as session:
            store.set_bedtime("", ["Jyt7jM8c"])
            saved.assert_not_called()
            session.return_value.__enter__.return_value.execute.assert_called_once()


class CheckTests(unittest.IsolatedAsyncioTestCase):
    def ready(self, app, *pods):
        test_home.AppTests.ready(self, app)
        app.sensibo.rooms = AsyncMock(return_value=(rooms_of(*pods), 0.0))
        app.bedtime, app.bedtime_rooms = "22:30", ["Jyt7jM8c"]
        app.schedule_bedtime = MagicMock()

    async def test_awake_she_is_told_to_offer_in_a_word(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.ready(app, pod(on=True))
            app.queue_observation = MagicMock()
            with patch.object(app, "can_reach_by_voice", return_value=True):
                await app.bedtime_check().wait()
            observation = app.queue_observation.call_args.args[0]
            self.assertIn("bedtime (22:30) and Office (cool, 22°C, fan auto) is still on", observation)
            app.schedule_bedtime.assert_called_once()

    async def test_in_standby_she_asks_on_signal(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.ready(app, pod(on=True))
            app.tell_person_on_signal = AsyncMock()
            with patch.object(app, "can_reach_by_voice", return_value=False), \
                    patch.object(app, "signal_reaches_person", return_value=True):
                await app.bedtime_check().wait()
            app.tell_person_on_signal.assert_awaited_once_with(
                "It's 22:30 and Office (cool, 22°C, fan auto) is still on. Want me to turn it off?")

    async def test_nothing_on_nothing_said(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.ready(app, pod(on=False))
            app.queue_observation = MagicMock()
            app.tell_person_on_signal = AsyncMock()
            await app.bedtime_check().wait()
            app.queue_observation.assert_not_called()
            app.tell_person_on_signal.assert_not_awaited()
            self.assertIn("Bedtime check: nothing left on.", "\n".join(app.activity.entries))

    async def test_a_saved_bedtime_runs_at_its_time(self):
        async with keyboard_tests.running_app() as (app, pilot):
            with patch("app_areas.home.seconds_until", return_value=3600.0), \
                    patch.object(app, "set_timer") as set_timer:
                app.save_bedtime("22:30", ["Jyt7jM8c"])
                await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            set_timer.assert_called_once()
            self.assertEqual(set_timer.call_args.args[0], 3600.0)
            app.persistence.save_bedtime.assert_called_once_with("22:30", ["Jyt7jM8c"])
            with patch.object(app, "set_timer") as set_timer:
                app.save_bedtime("", [])
                await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            set_timer.assert_not_called()
            self.assertIsNone(app.bedtime_timer)


class SettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_time_is_saved_and_rooms_ticked_under_her_home(self):
        from textual.widgets import Button, Input, SelectionList
        from test_model_settings_screen import MODELS
        from ui.model_settings import ModelSettingsScreen

        async with keyboard_tests.running_app() as (app, pilot):
            app.persistence.load_models.return_value = MODELS
            app.sensibo.set_key("k" * 30)
            app.home_rooms = [("Jyt7jM8c", "Office"), ("BpP9b5Ka", "Declan’s Room")]
            await pilot.click("#settings-button")
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            await pilot.pause()
            self.assertIsInstance(app.screen, ModelSettingsScreen)
            await asyncio.wait_for(app.screen.all_built.wait(), 5)
            screen = app.screen
            ticks = screen.query_one("#home-bedtime-rooms", SelectionList)
            self.assertEqual(ticks.option_count, 2)
            screen.query_one("#home-bedtime", Input).value = "10pm"
            screen.query_one("#home-save-bedtime", Button).press()
            await pilot.pause()
            app.persistence.save_bedtime.assert_not_called()
            screen.query_one("#home-bedtime", Input).value = "22:30"
            screen.query_one("#home-save-bedtime", Button).press()
            await pilot.pause()
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            app.persistence.save_bedtime.assert_called_with("22:30", [])
            ticks.select("Jyt7jM8c")
            await pilot.pause()
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            app.persistence.save_bedtime.assert_called_with("22:30", ["Jyt7jM8c"])
            self.assertEqual((app.bedtime, app.bedtime_rooms), ("22:30", ["Jyt7jM8c"]))


if __name__ == "__main__":
    unittest.main()
