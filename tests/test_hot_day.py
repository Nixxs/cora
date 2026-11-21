"""Her home, step 4b-iii: on a hot day's forecast, she offers to cool the ticked rooms ahead of the heat."""

import asyncio
from datetime import datetime
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx2

from home.forecast import forecast, hot_day, rounded
from home.sensibo import parse_rooms
from storage.app_settings_store import AppSettingsStore
import test_home
from test_home import pod
import test_keyboard_persistence as keyboard_tests

MORNING = datetime(2026, 12, 3, 8, 0)


def day(*temperatures, date="2026-12-03"):
    """Open-Meteo's hourly answer for a day, from midnight."""
    return {"timezone": "Australia/Perth", "hourly": {
        "time": [f"{date}T{hour:02d}:00" for hour in range(len(temperatures))],
        "temperature_2m": list(temperatures)}}


HOT = day(*([24] * 13 + [33, 35, 37, 36, 30] + [26] * 6))  # 37° at 15:00


class ForecastTests(unittest.TestCase):
    def test_the_peak_and_a_start_two_hours_ahead_of_it(self):
        found = hot_day(HOT, 32, MORNING)
        self.assertEqual((found.peak, found.peak_at.hour, found.start_at.hour), (37.0, 15, 13))

    def test_a_mild_day_or_a_peak_already_past_offers_nothing(self):
        self.assertIsNone(hot_day(HOT, 38, MORNING))
        self.assertIsNone(hot_day(HOT, 32, datetime(2026, 12, 3, 16, 0)))
        # Late in the run-up, cooling starts at the next hour.
        self.assertEqual(hot_day(HOT, 32, datetime(2026, 12, 3, 13, 40)).start_at.hour, 14)
        self.assertIsNone(hot_day(day(40, 40, date="2026-12-04"), 32, MORNING))
        with self.assertRaises(ValueError):
            hot_day({"error": True}, 32, MORNING)

    def test_only_a_rough_location_is_sent(self):
        self.assertEqual(rounded(-31.95321, 115.86047), (-32.0, 115.9))
        sent = []

        def handle(request):
            sent.append(request)
            return httpx2.Response(200, json=HOT)

        asyncio.run(forecast(-31.95321, 115.86047, transport=httpx2.MockTransport(handle)))
        params = sent[0].url.params
        self.assertEqual((params["latitude"], params["longitude"]), ("-32.0", "115.9"))
        self.assertEqual((params["hourly"], params["timezone"], params["forecast_days"]), ("temperature_2m", "auto", "1"))


class LocationTests(test_home.ClientTests):
    async def test_the_home_location_is_rounded_as_soon_as_its_read(self):
        client = self.client((200, {"result": [{"id": "Jyt7jM8c", "location": {"latLon": [-31.95321, 115.86047]}}]}),
                             (200, {"result": [{"id": "Jyt7jM8c", "location": None}]}))
        self.assertEqual(await client.home_location(), (-32.0, 115.9))
        self.assertEqual(self.requests[0].url.params["fields"], "id,location")
        self.assertIsNone(await client.home_location())


class StoreTests(unittest.TestCase):
    def test_the_temperature_and_rooms_are_saved_together(self):
        store = AppSettingsStore(MagicMock())
        with patch.object(store, "_get", return_value="32|Jyt7jM8c"):
            self.assertEqual(store.hot_day(), (32, ["Jyt7jM8c"]))
        with patch.object(store, "_get", return_value="99|Jyt7jM8c"):
            self.assertEqual(store.hot_day(), (None, []))
        with patch.object(store, "_set") as saved:
            store.set_hot_day(32, ["Jyt7jM8c", "BpP9b5Ka"])
            saved.assert_called_once_with("hot_day", "32|Jyt7jM8c,BpP9b5Ka")
            with self.assertRaises(ValueError):
                store.set_hot_day(5, [])


class CheckTests(unittest.IsolatedAsyncioTestCase):
    def ready(self, app, *pods):
        test_home.AppTests.ready(self, app)
        app.sensibo.rooms = AsyncMock(return_value=(parse_rooms({"result": list(pods)}), 0.0))
        app.sensibo.home_location = AsyncMock(return_value=(-32.0, 115.9))
        app.hot_day, app.hot_day_rooms = 32, ["Jyt7jM8c", "BpP9b5Ka", "Kept1", "Sched1"]
        app.schedule_hot_day = MagicMock()

    def pods(self):
        return (pod(on=False), pod("BpP9b5Ka", "Declan’s Room", on=True),
                pod("Kept1", "Bedroom") | {"smartMode": {"enabled": True, "lowTemperatureThreshold": 22,
                                                         "highTemperatureThreshold": 25}},
                pod("Sched1", "Study") | {"schedules": [{"id": "Sch3dule1", "isEnabled": True, "targetTimeLocal": "12:00",
                                                         "recurringDays": ["Thursday"], "acState": {"on": True}}]})

    async def run_check(self, app, voice: bool):
        with patch("app_areas.home.forecast", AsyncMock(return_value=HOT)), \
                patch("app_areas.home.datetime") as clock, \
                patch.object(app, "can_reach_by_voice", return_value=voice), \
                patch.object(app, "signal_reaches_person", return_value=True):
            clock.now.return_value = MORNING
            await app.hot_day_check().wait()

    async def test_awake_she_offers_for_the_rooms_nothing_looks_after(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.ready(app, *self.pods())
            app.queue_observation = MagicMock()
            await self.run_check(app, voice=True)
            observation = app.queue_observation.call_args.args[0]
            # Declan's is already on, the bedroom kept by Climate React, the study due to come on.
            self.assertIn("reaches 37°C around 15:00. Office is off", observation)
            self.assertIn("from about 13:00", observation)

    async def test_in_standby_she_asks_on_signal(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.ready(app, *self.pods())
            app.tell_person_on_signal = AsyncMock()
            await self.run_check(app, voice=False)
            app.tell_person_on_signal.assert_awaited_once_with(
                "It's forecast 37°C around 15:00 today. Want me to cool Office from about 13:00?")

    async def test_a_mild_day_says_nothing(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.ready(app, *self.pods())
            app.hot_day = 40
            app.queue_observation = MagicMock()
            await self.run_check(app, voice=True)
            app.queue_observation.assert_not_called()
            self.assertIn("today stays under 40°C", "\n".join(app.activity.entries))


class SettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_temperature_is_saved_and_rooms_ticked(self):
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
            screen.query_one("#home-hot-day", Input).value = "60"
            screen.query_one("#home-save-hot-day", Button).press()
            await pilot.pause()
            app.persistence.save_hot_day.assert_not_called()
            screen.query_one("#home-hot-day", Input).value = "32"
            screen.query_one("#home-save-hot-day", Button).press()
            await pilot.pause()
            screen.query_one("#home-hot-day-rooms", SelectionList).select("Jyt7jM8c")
            await pilot.pause()
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            app.persistence.save_hot_day.assert_called_with(32, ["Jyt7jM8c"])
            self.assertEqual((app.hot_day, app.hot_day_rooms), (32, ["Jyt7jM8c"]))


if __name__ == "__main__":
    unittest.main()
