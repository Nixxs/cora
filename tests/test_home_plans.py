"""Her home, step 4a: schedules and Climate React on the air conditioners. Mocked HTTP only."""

import json
import unittest
from unittest.mock import AsyncMock, patch

from home.sensibo import (
    days_words, parse_rooms, plan_climate_react, plans, reading, room_context, schedule_words,
)
from tools.aircon import validate_arguments
from ui.dashboard import plan_line
import test_home
from test_home import NOW, ROOMS, args, pod
import test_keyboard_persistence as keyboard_tests

# As Home Assistant's library reads a listed schedule (recurringDays, nextTime in UTC without a zone).
SCHEDULE = {"id": "Sch3dule1", "isEnabled": True, "name": None, "targetTimeLocal": "16:00",
            "timezone": "Australia/Perth", "recurringDays": ["Monday", "tuesday", "wednesday", "thursday", "friday"],
            "nextTime": "2026-11-23T08:00:00", "podUid": "Jyt7jM8c",
            "acState": {"on": True, "mode": "cool", "targetTemperature": 24, "temperatureUnit": "C", "fanLevel": "auto"}}
REACT = {"enabled": True, "type": "temperature", "lowTemperatureThreshold": 22, "highTemperatureThreshold": 25,
         "lowTemperatureState": {"on": False, "mode": "cool"},
         "highTemperatureState": {"on": True, "mode": "cool", "targetTemperature": 23, "fanLevel": "auto"}}


def planned_pod(**extra):
    return pod() | {"schedules": [SCHEDULE], "smartMode": REACT} | extra


class ParsingTests(unittest.TestCase):
    def test_schedules_and_climate_react_come_with_each_room(self):
        office = parse_rooms({"result": [planned_pod()]})[0]
        (schedule,) = office.schedules
        self.assertEqual((schedule.id, schedule.time, schedule.enabled), ("Sch3dule1", "16:00", True))
        self.assertEqual(schedule.days, ("monday", "tuesday", "wednesday", "thursday", "friday"))
        # A time without a zone is UTC, as Sensibo gives them.
        self.assertEqual(schedule.next_at.isoformat(), "2026-11-23T08:00:00+00:00")
        self.assertEqual((office.react.low, office.react.high, office.react.enabled), (22.0, 25.0, True))
        self.assertTrue(office.react.high_state["on"])

    def test_none_set_up_and_odd_ones(self):
        bare = parse_rooms({"result": [pod() | {"schedules": [], "smartMode": None}]})[0]
        self.assertEqual((bare.schedules, bare.react), ((), None))
        odd = parse_rooms({"result": [pod() | {
            "schedules": ["junk", {"id": "../x"}, {"id": "ok12", "recurOnDaysOfWeek": ["sunday", "funday"]}],
            "smartMode": {}}]})[0]
        self.assertEqual([(s.id, s.days, s.time) for s in odd.schedules], [("ok12", ("sunday",), "")])
        self.assertIsNone(odd.react)

    def test_a_timer_given_without_a_zone_is_utc(self):
        timed = parse_rooms({"result": [pod() | {"timer": {"targetTime": "2026-11-20T13:22:00",
                                                            "acState": {"on": False}}}]})[0]
        self.assertEqual(reading(timed, NOW)["timer"], "turns off in 40 minutes")

    def test_how_they_are_said(self):
        self.assertEqual(days_words([]), "once")
        self.assertEqual(days_words(["saturday", "sunday"]), "weekends")
        self.assertEqual(days_words(["monday", "wednesday"]), "mon, wed")
        office = parse_rooms({"result": [planned_pod()]})[0]
        self.assertEqual(schedule_words(office.schedules[0]), "16:00 weekdays: cool, 24°C, fan auto")
        self.assertEqual(plans(office), {
            "schedules": [{"id": "Sch3dule1", "when": "16:00 weekdays: cool, 24°C, fan auto"}],
            "climate_react": "keeps 22–25°C: cool, 23°C, fan auto above 25, off below 22"})
        # Not in her room's short line.
        self.assertNotIn("schedules", json.loads(room_context(office, NOW, NOW)))
        self.assertEqual(plan_line(office), "keeps 22–25° by cooling · next 16:00 weekdays: cool, 24°C, fan auto")
        self.assertEqual(plans(parse_rooms(ROOMS)[0]), {})


class ClimateReactPlanTests(unittest.TestCase):
    def office(self, **state):
        return parse_rooms({"result": [pod(**state)]})[0]

    def test_cooling_above_the_top_and_off_below_the_bottom(self):
        body, notes = plan_climate_react(self.office(), 22, 25)
        self.assertEqual((body["lowTemperatureThreshold"], body["highTemperatureThreshold"]), (22, 25))
        self.assertEqual(body["type"], "temperature")
        # Aiming at the middle of the range, unless told.
        self.assertEqual(body["highTemperatureState"] | {}, {"on": True, "mode": "cool", "temperatureUnit": "C",
                                                              "fanLevel": "auto", "swing": "fixedTop",
                                                              "targetTemperature": 24})
        self.assertFalse(body["lowTemperatureState"]["on"])

    def test_heating_below_the_bottom(self):
        heats = parse_rooms({"result": [pod() | {"remoteCapabilities": {"modes": {
            "heat": {"temperatures": {"C": {"values": list(range(10, 31))}}, "fanLevels": ["auto"], "swing": []},
        }}}]})[0]
        body, _ = plan_climate_react(heats, 18, 21, mode="heat", temperature=20)
        self.assertEqual((body["lowTemperatureState"]["on"], body["lowTemperatureState"]["targetTemperature"]), (True, 20))
        self.assertFalse(body["highTemperatureState"]["on"])

    def test_ranges_and_modes_it_wont_take(self):
        for low, high, mode, words in ((25, 22, None, "between"), (22, 22.5, None, "at least"),
                                        (2, 25, None, "between"), (22, 25, "dry", "cooling or heating"),
                                        (22, 25, "heat", "no heat mode")):
            with self.subTest(low=low, high=high, mode=mode), self.assertRaises(ValueError) as caught:
                plan_climate_react(self.office(), low, high, mode=mode)
            self.assertIn(words, str(caught.exception))


class RequestTests(test_home.ClientTests):
    async def test_schedules_are_added_paused_and_removed_on_the_older_api(self):
        client = self.client((200, {"status": "success"}), (200, {}), (200, {}), (404, {}))
        await client.add_schedule("Jyt7jM8c", "16:00", "Australia/Perth", ["monday"], {"on": True, "mode": "cool"})
        request = self.requests[0]
        self.assertEqual((request.method, request.url.path), ("POST", "/api/v1/pods/Jyt7jM8c/schedules/"))
        self.assertEqual(json.loads(request.content), {
            "targetTimeLocal": "16:00", "timezone": "Australia/Perth", "acState": {"on": True, "mode": "cool"},
            "recurOnDaysOfWeek": ["monday"]})
        await client.switch_schedule("Jyt7jM8c", "Sch3dule1", False)
        self.assertEqual((self.requests[1].method, self.requests[1].url.path),
                         ("PUT", "/api/v1/pods/Jyt7jM8c/schedules/Sch3dule1"))
        self.assertEqual(json.loads(self.requests[1].content), {"isEnabled": False})
        self.assertTrue(await client.remove_schedule("Jyt7jM8c", "Sch3dule1"))
        self.assertFalse(await client.remove_schedule("Jyt7jM8c", "Sch3dule1"))
        with self.assertRaises(test_home.SensiboError):
            await client.remove_schedule("Jyt7jM8c", "../x")

    async def test_climate_react_is_set_and_switched_on_the_newer_api(self):
        client = self.client((200, {"status": "success"}), (200, {}), (400, {"reason": "Bad threshold"}))
        await client.set_climate_react("Jyt7jM8c", {"enabled": True})
        self.assertEqual((self.requests[0].method, self.requests[0].url.path), ("POST", "/api/v2/pods/Jyt7jM8c/smartmode"))
        await client.switch_climate_react("Jyt7jM8c", False)
        self.assertEqual((self.requests[1].method, json.loads(self.requests[1].content)), ("PUT", {"enabled": False}))
        with self.assertRaises(test_home.SensiboError) as caught:
            await client.switch_climate_react("Jyt7jM8c", True)
        self.assertIn("Bad threshold", str(caught.exception))


class ToolCheckTests(unittest.TestCase):
    def test_the_new_actions_and_their_checks(self):
        for good in (args("add_schedule", "office", time="16:00", days=["monday"], temperature=24),
                     args("add_schedule", time="07:30", on=False),
                     args("remove_schedule", schedule="Sch3dule1"), args("pause_schedule", "office", schedule="Sch3dule1"),
                     args("climate_react", "office", low=22, high=25.5), args("climate_react", "office"),
                     args("climate_react_off")):
            with self.subTest(good=good):
                validate_arguments(good)
        for bad in (args("add_schedule", time="4pm", temperature=24), args("add_schedule", time="16:00"),
                    args("add_schedule", time="16:00", on=True, days=["monday", "monday"]),
                    args("add_schedule", time="16:00", on=True, days=["funday"]),
                    args("resume_schedule"), args("remove_schedule", schedule="../x"),
                    args("climate_react", low=22), args("climate_react", low=True, high=25)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_arguments(bad)


class AppTests(unittest.IsolatedAsyncioTestCase):
    def ready(self, app, body=None):
        test_home.AppTests.ready(self, app)
        rooms = parse_rooms(body or {"result": [planned_pod(), pod("BpP9b5Ka", "Declan’s Room")]})
        app.sensibo.rooms = AsyncMock(return_value=(rooms, 0.0))

    async def test_status_lists_each_rooms_schedules_and_climate_react(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.ready(app)
            result = await app.use_aircon(args())
            office, declan = result["rooms"]
            self.assertEqual(office["schedules"][0]["id"], "Sch3dule1")
            self.assertIn("keeps 22–25°C", office["climate_react"])
            self.assertNotIn("schedules", declan)

    async def test_a_schedule_is_added_in_her_time_zone_and_turns_the_unit_on(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.ready(app)
            app.sensibo.add_schedule = AsyncMock()
            with patch("app_areas.home.local_time_zone", return_value="Australia/Perth"), \
                    patch.object(app, "reread_home") as reread:
                result = await app.use_aircon(args("add_schedule", "office", time="16:00",
                                                   days=["monday", "tuesday", "wednesday", "thursday", "friday"],
                                                   temperature=24))
            unit, at, zone, days, state = app.sensibo.add_schedule.await_args.args
            self.assertEqual((unit, at, zone, len(days)), ("Jyt7jM8c", "16:00", "Australia/Perth", 5))
            self.assertEqual((state["on"], state["targetTemperature"]), (True, 24))
            self.assertEqual(result["done"], ["Office: at 16:00 weekdays, cool, 24°C, fan auto"])
            self.assertIn("what's set up", result["note"])
            reread.assert_called_once()
            with patch("app_areas.home.local_time_zone", return_value=""):
                refused = await app.use_aircon(args("add_schedule", "office", time="16:00", temperature=24))
            self.assertIn("time zone", refused["message"])

    async def test_a_schedule_is_found_by_id_in_whichever_room_has_it(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.ready(app)
            app.sensibo.switch_schedule = AsyncMock()
            app.sensibo.remove_schedule = AsyncMock(return_value=True)
            with patch.object(app, "reread_home"):
                paused = await app.use_aircon(args("pause_schedule", schedule="Sch3dule1"))
                removed = await app.use_aircon(args("remove_schedule", schedule="Sch3dule1"))
                missing = await app.use_aircon(args("resume_schedule", schedule="Nope1234"))
            app.sensibo.switch_schedule.assert_awaited_once_with("Jyt7jM8c", "Sch3dule1", False)
            self.assertEqual(paused["done"], ["Office: paused: 16:00 weekdays"])
            self.assertEqual(removed["done"], ["Office: removed: 16:00 weekdays"])
            self.assertEqual(missing["status"], "not_found")

    async def test_climate_react_is_set_switched_off_and_back_on(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.ready(app)
            app.sensibo.set_climate_react = AsyncMock()
            app.sensibo.switch_climate_react = AsyncMock()
            with patch.object(app, "reread_home"):
                result = await app.use_aircon(args("climate_react", "declan", low=22, high=25))
                off = await app.use_aircon(args("climate_react_off", "office"))
                back = await app.use_aircon(args("climate_react", "office"))
                none_saved = await app.use_aircon(args("climate_react", "declan"))
            unit, body = app.sensibo.set_climate_react.await_args.args
            self.assertEqual((unit, body["highTemperatureThreshold"]), ("BpP9b5Ka", 25))
            self.assertEqual(result["done"], ["Declan’s Room: Climate React keeps 22–25°C: cool, 24°C, fan auto "
                                              "above 25, off on the other side"])
            self.assertEqual(off["done"], ["Office: Climate React off (its settings kept)"])
            self.assertEqual(app.sensibo.switch_climate_react.await_args_list[1].args, ("Jyt7jM8c", True))
            self.assertIn("Climate React back on: keeps 22–25°C", back["done"][0])
            self.assertEqual(none_saved["status"], "not_allowed")


if __name__ == "__main__":
    unittest.main()
