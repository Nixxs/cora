"""Her home: the air conditioners through Sensibo, and the room she lives in. Mocked HTTP only."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx2

from home import sensibo
from home.sensibo import (
    BadKey, FRESH_SECONDS, MIN_GAP_SECONDS, RateLimited, Sensibo, SensiboError, describe, find_room, parse_rooms,
    plan_state, reading, room_context, save_sensibo_key,
)
from session_config import ROOM_CONTEXT_MAX_CHARS, instruction_sections
from tools.aircon import SCHEMA, AirconTool, validate_arguments
import test_keyboard_persistence as keyboard_tests

NOW = datetime(2026, 11, 20, 12, 42, tzinfo=timezone.utc)


def pod(unit_id="Jyt7jM8c", name="Office", on=False, temperature=20.8, humidity=75, alive=True, **state):
    return {
        "id": unit_id, "room": {"uid": "r1", "name": name},
        "acState": {"on": on, "mode": "cool", "targetTemperature": 22, "temperatureUnit": "C",
                    "fanLevel": "auto", "swing": "fixedTop"} | state,
        "measurements": {"time": {"time": "2026-11-20T12:41:19.512464Z", "secondsAgo": 14},
                         "temperature": temperature, "humidity": humidity, "feelsLike": temperature},
        "connectionStatus": {"isAlive": alive},
        "remoteCapabilities": {"modes": {
            "cool": {"temperatures": {"C": {"isNative": True, "values": list(range(18, 31))}, "F": {"values": [64, 86]}},
                     "fanLevels": ["quiet", "low", "auto"], "swing": ["stopped", "fixedTop"]},
            "fan": {"temperatures": {}, "fanLevels": ["low"], "swing": ["stopped"]},
        }},
    }


ROOMS = {"status": "success", "result": [pod(), pod("BpP9b5Ka", "Declan’s Room", temperature=21.2, humidity=86)]}


class ParsingTests(unittest.TestCase):
    def test_every_unit_with_its_state_readings_and_what_it_supports(self):
        office, declan = parse_rooms(ROOMS)
        self.assertEqual((office.id, office.name, office.on, office.mode, office.target), ("Jyt7jM8c", "Office", False, "cool", 22))
        self.assertEqual((office.temperature, office.humidity, office.feels_like), (20.8, 75.0, 20.8))
        self.assertEqual(office.measured_at, datetime(2026, 11, 20, 12, 41, 19, 512464, tzinfo=timezone.utc))
        self.assertEqual(office.modes["cool"], {"temperatures": list(range(18, 31)), "fan": ["quiet", "low", "auto"],
                                                "swing": ["stopped", "fixedTop"]})
        self.assertEqual(office.modes["fan"]["temperatures"], [])
        self.assertEqual(declan.name, "Declan’s Room")

    def test_odd_answers_are_refused_or_skipped(self):
        with self.assertRaises(ValueError):
            parse_rooms({"status": "error"})
        rooms = parse_rooms({"result": ["junk", {"no": "id"}, {"id": "x1", "acState": "bad", "measurements": None}]})
        self.assertEqual(len(rooms), 1)
        self.assertEqual((rooms[0].name, rooms[0].on, rooms[0].temperature), ("x1", None, None))

    def test_rooms_are_found_loosely_but_never_ambiguously(self):
        rooms = parse_rooms(ROOMS)
        self.assertEqual(find_room(rooms, "declan's room").id, "BpP9b5Ka")
        self.assertEqual(find_room(rooms, "  OFFICE ").id, "Jyt7jM8c")
        self.assertEqual(find_room(rooms, "decl").id, "BpP9b5Ka")
        self.assertIsNone(find_room(rooms, "kitchen"))
        two = parse_rooms({"result": [pod("a1", "Bedroom 1"), pod("b2", "Bedroom 2")]})
        self.assertIsNone(find_room(two, "bedroom"))
        self.assertIsNone(find_room(two, ""))

    def test_a_reading_says_how_old_it_is_and_what_the_aircon_is_doing(self):
        office = parse_rooms(ROOMS)[0]
        self.assertEqual(reading(office, NOW), {"room": "Office", "temperature": "20.8°C", "humidity": "75%",
                                                "measured": "just now", "aircon": "off"})
        on = parse_rooms({"result": [pod(on=True, fanLevel="low")]})[0]
        self.assertEqual(reading(on, NOW)["aircon"], "cool, set to 22°C, fan low")
        self.assertEqual(reading(on, NOW + timedelta(minutes=20))["measured"], "21 minutes ago")
        offline = parse_rooms({"result": [pod(alive=False)]})[0]
        self.assertIn("offline", reading(offline, NOW)["aircon"])

    def test_her_room_is_one_short_line(self):
        office = parse_rooms(ROOMS)[0]
        line = room_context(office, NOW, datetime(2026, 11, 20, 20, 42))
        self.assertEqual(json.loads(line)["as_of"], "2026-11-20 20:42")
        self.assertLess(len(line), ROOM_CONTEXT_MAX_CHARS)
        long_name = parse_rooms({"result": [pod(name="x" * 500)]})[0]
        self.assertLess(len(room_context(long_name, NOW, NOW)), ROOM_CONTEXT_MAX_CHARS)


class KeyTests(unittest.TestCase):
    def test_a_key_is_checked_saved_and_forgotten(self):
        with patch.object(sensibo, "keyring") as keyring:
            self.assertEqual(save_sensibo_key("  abcDEF1234567890abcdef1234 "), "abcDEF1234567890abcdef1234")
            keyring.set_password.assert_called_once_with("cora", "home:sensibo-key", "abcDEF1234567890abcdef1234")
            with self.assertRaises(ValueError):
                save_sensibo_key("not a key!")
            self.assertEqual(save_sensibo_key(""), "")
            keyring.delete_password.assert_called_once()


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def client(self, *answers):
        self.requests = []
        self.answers = list(answers)
        self.now = [1000.0]
        self.slept = []

        def handle(request):
            self.requests.append(request)
            status, body = self.answers.pop(0)
            return httpx2.Response(status, json=body)

        async def sleep(seconds):
            self.slept.append(seconds)
            self.now[0] += seconds

        return Sensibo("k" * 30, clock=lambda: self.now[0], sleep=sleep, transport=httpx2.MockTransport(handle))

    async def test_every_unit_in_one_request_reused_while_fresh(self):
        client = self.client((200, ROOMS), (200, ROOMS))
        rooms, age = await client.rooms()
        self.assertEqual([room.name for room in rooms], ["Office", "Declan’s Room"])
        self.assertEqual(age, 0.0)
        request = self.requests[0]
        self.assertEqual(request.url.path, "/api/v2/users/me/pods")
        self.assertIn("remoteCapabilities", request.url.params["fields"])
        self.now[0] += 30
        _, age = await client.rooms()
        self.assertEqual((len(self.requests), age), (1, 30.0))
        self.now[0] += FRESH_SECONDS
        await client.rooms()
        self.assertEqual(len(self.requests), 2)

    async def test_requests_are_spaced_out(self):
        client = self.client((200, ROOMS), (200, ROOMS))
        await client.rooms()
        await client.rooms(max_age=0)
        self.assertEqual(self.slept, [MIN_GAP_SECONDS])

    async def test_a_refusal_falls_back_to_the_last_answer_or_says_why(self):
        client = self.client((429, {"status": 429, "message": "API Limit exceeded"}))
        with self.assertRaises(RateLimited):
            await client.rooms()
        client = self.client((200, ROOMS), (429, {}))
        await client.rooms()
        self.now[0] += 120
        rooms, age = await client.rooms()
        self.assertEqual((len(rooms), age), (2, 120.0))

    async def test_a_bad_or_missing_key_is_said_plainly(self):
        with self.assertRaises(BadKey):
            await Sensibo("").rooms()
        client = self.client((401, {}))
        with self.assertRaises(BadKey):
            await client.rooms()
        client.set_key("")
        with self.assertRaises(BadKey):
            await client.rooms()


class ContextTests(unittest.TestCase):
    def test_her_room_joins_her_context_framed_as_where_she_is(self):
        line = room_context(parse_rooms(ROOMS)[0], NOW, NOW)
        section = instruction_sections(room_context=line)["her_room"]
        self.assertIn("The room you live in", section)
        self.assertIn("You have no body", section)
        self.assertIn("never as a matter of course", section)
        self.assertTrue(section.endswith(line))
        self.assertNotIn("her_room", instruction_sections())
        with self.assertRaises(ValueError):
            instruction_sections(room_context="x" * (ROOM_CONTEXT_MAX_CHARS + 1))


def args(action="status", room=None, **changes) -> dict:
    return {"action": action, "room": room, "on": None, "mode": None, "temperature": None, "fan": None,
            "swing": None, "minutes": None, "time": None, "days": None, "schedule": None, "low": None,
            "high": None} | changes


class PlanTests(unittest.TestCase):
    def office(self, **state):
        return parse_rooms({"result": [pod(**state)]})[0]

    def test_a_change_keeps_the_rest_of_the_state(self):
        state, notes = plan_state(self.office(), on=True, temperature=19)
        self.assertEqual(state, {"on": True, "mode": "cool", "temperatureUnit": "C", "fanLevel": "auto",
                                 "swing": "fixedTop", "targetTemperature": 19})
        self.assertEqual(notes, [])
        off, _ = plan_state(self.office(on=True), on=False)
        self.assertFalse(off["on"])
        self.assertEqual(off["targetTemperature"], 22)

    def test_what_a_unit_doesnt_take_is_refused_saying_whats_allowed(self):
        office = self.office()
        for change, words in (({"temperature": 31}, "18 to 30°C"), ({"mode": "heat"}, "no heat mode"),
                              ({"fan": "turbo"}, "quiet, low, auto"), ({"mode": "fan", "temperature": 20},
                                                                       "no temperature"), ({}, "Say what")):
            with self.subTest(change=change), self.assertRaises(ValueError) as caught:
                plan_state(office, **change)
            self.assertIn(words, str(caught.exception))

    def test_switching_mode_adjusts_what_the_new_mode_lacks_and_says_so(self):
        state, notes = plan_state(self.office(fanLevel="quiet", swing="fixedTop"), mode="fan")
        self.assertEqual((state["fanLevel"], state["swing"]), ("low", "stopped"))
        self.assertNotIn("targetTemperature", state)
        self.assertEqual(len(notes), 2)
        self.assertIn("fan mode has no quiet", notes[0])
        # A setpoint the mode doesn't take (left from another mode, such as heat at 10°) moves to the nearest.
        state, notes = plan_state(self.office(targetTemperature=12), on=True)
        self.assertEqual(state["targetTemperature"], 18)
        self.assertIn("nearest", notes[0])

    def test_a_state_is_said_simply(self):
        self.assertEqual(describe({"on": True, "mode": "cool", "targetTemperature": 23, "temperatureUnit": "C",
                                   "fanLevel": "auto"}), "cool, 23°C, fan auto")
        self.assertEqual(describe({"on": False, "mode": "cool"}), "off")

    def test_a_timer_shows_in_the_reading(self):
        timed = parse_rooms({"result": [pod() | {"timer": {"targetTime": "2026-11-20T13:22:00Z",
                                                            "acState": {"on": False}}}]})[0]
        self.assertEqual(reading(timed, NOW)["timer"], "turns off in 40 minutes")
        self.assertNotIn("timer", reading(parse_rooms(ROOMS)[0], NOW))


class ChangeRequestTests(ClientTests):
    async def test_a_state_is_posted_whole_and_the_reading_forgotten(self):
        client = self.client((200, ROOMS), (200, {"status": "success", "result": {
            "status": "Success", "acState": {"on": True, "mode": "cool", "targetTemperature": 23}}}))
        await client.rooms()
        resulting = await client.set_state("Jyt7jM8c", {"on": True, "mode": "cool", "targetTemperature": 23})
        self.assertEqual(resulting["targetTemperature"], 23)
        request = self.requests[1]
        self.assertEqual((request.method, request.url.path), ("POST", "/api/v2/pods/Jyt7jM8c/acStates"))
        self.assertEqual(json.loads(request.content), {"acState": {"on": True, "mode": "cool", "targetTemperature": 23}})
        self.assertIsNone(client.cached)
        self.assertEqual(self.slept, [MIN_GAP_SECONDS])

    async def test_a_unit_that_doesnt_take_it_says_why(self):
        client = self.client((200, {"result": {"status": "Failed", "failureReason": "IR not received"}}))
        with self.assertRaises(SensiboError) as caught:
            await client.set_state("Jyt7jM8c", {"on": True})
        self.assertIn("IR not received", str(caught.exception))
        with self.assertRaises(SensiboError):
            await client.set_state("../etc", {"on": True})

    async def test_timers_are_set_and_cancelled_on_the_older_api(self):
        client = self.client((200, {"status": "success"}), (200, {}), (404, {}))
        await client.set_timer("Jyt7jM8c", 120, {"on": False})
        request = self.requests[0]
        self.assertEqual((request.method, request.url.path), ("PUT", "/api/v1/pods/Jyt7jM8c/timer/"))
        self.assertEqual(json.loads(request.content), {"minutesFromNow": 120, "acState": {"on": False}})
        self.assertTrue(await client.cancel_timer("Jyt7jM8c"))
        self.assertEqual(self.requests[1].method, "DELETE")
        self.assertFalse(await client.cancel_timer("Jyt7jM8c"))


class ToolTests(unittest.IsolatedAsyncioTestCase):
    def test_the_actions_and_their_checks(self):
        self.assertEqual(SCHEMA["parameters"]["properties"]["action"]["enum"][:4], ["status", "set", "timer", "cancel_timer"])
        self.assertEqual(set(SCHEMA["parameters"]["required"]), set(SCHEMA["parameters"]["properties"]))
        for good in (args(), args(room="Office"), args("set", on=False), args("set", "office", temperature=23),
                     args("timer", "office", on=False, minutes=120), args("cancel_timer", "office")):
            validate_arguments(good)
        for bad in (args("set"), args("timer", on=False), args("timer", on=False, minutes=2000),
                    args("set", mode="turbo"), args("set", on="yes"), args("set", temperature=True),
                    args(room=""), args("explode"), {"action": "status", "room": None}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_arguments(bad)

    async def test_the_answer_goes_back_to_her(self):
        connection = MagicMock()
        connection.response.item.create = AsyncMock()
        connection.response.create = AsyncMock()
        use = AsyncMock(return_value={"status": "ok", "rooms": []})
        await AirconTool(lambda: connection, lambda: False, use, MagicMock()).handle(
            "call_1", args(room=" Office ", fan=" low "))
        self.assertEqual(use.await_args.args[0]["room"], "Office")
        self.assertEqual(use.await_args.args[0]["fan"], "low")
        connection.response.create.assert_awaited_once_with()


class AppTests(unittest.IsolatedAsyncioTestCase):
    def ready(self, app, her_room="Jyt7jM8c"):
        app.sensibo.set_key("k" * 30)
        app.sensibo.rooms = AsyncMock(return_value=(parse_rooms(ROOMS), 0.0))
        app.sensibo_enabled = True
        app.her_room_id = her_room

    async def test_aircon_is_offered_only_once_set_up(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.assertIn("aircon", app.unavailable_tools())
            self.ready(app)
            self.assertNotIn("aircon", app.unavailable_tools())

    async def test_she_reads_every_room_or_one_by_name(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.ready(app)
            result = await app.use_aircon(args())
            self.assertEqual([room["room"] for room in result["rooms"]], ["Office", "Declan’s Room"])
            one = await app.use_aircon(args(room="declan"))
            self.assertEqual(one["rooms"][0]["humidity"], "86%")
            missing = await app.use_aircon(args(room="kitchen"))
            self.assertEqual(missing["status"], "not_found")
            self.assertIn("Office", missing["rooms"])
            app.sensibo.rooms = AsyncMock(side_effect=RateLimited("Sensibo is limiting requests just now."))
            refused = await app.use_aircon(args())
            self.assertEqual(refused["status"], "error")
            self.assertIn("rather than guessing", refused["message"])

    async def test_she_changes_one_room_from_a_fresh_reading(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.ready(app)
            app.sensibo.set_state = AsyncMock(side_effect=lambda unit, state: state)
            with patch.object(app, "refresh_her_room") as refresh:
                result = await app.use_aircon(args("set", "office", on=True, temperature=23))
            self.assertEqual(app.sensibo.rooms.await_args.kwargs["max_age"], 10.0)
            unit, state = app.sensibo.set_state.await_args.args
            self.assertEqual((unit, state["on"], state["targetTemperature"]), ("Jyt7jM8c", True, 23))
            self.assertEqual(result["done"], ["Office: now cool, 23°C, fan auto"])
            self.assertIn("tell the person plainly", result["note"])
            refresh.assert_called_once()
            self.assertIn("Aircon Office: now cool, 23°C, fan auto.", "\n".join(app.activity.entries))

    async def test_every_room_at_once_and_nothing_sent_when_one_cant_take_it(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.ready(app)
            app.sensibo.set_state = AsyncMock(side_effect=lambda unit, state: state)
            result = await app.use_aircon(args("set", on=False))
            self.assertEqual(app.sensibo.set_state.await_count, 2)
            self.assertEqual(result["done"], ["Office: now off", "Declan’s Room: now off"])
            app.sensibo.set_state.reset_mock()
            refused = await app.use_aircon(args("set", temperature=35))
            self.assertEqual(refused["status"], "not_allowed")
            self.assertIn("18 to 30°C", refused["message"])
            app.sensibo.set_state.assert_not_awaited()

    async def test_one_room_failing_is_said_alongside_the_ones_that_changed(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.ready(app)
            app.sensibo.set_state = AsyncMock(side_effect=[{"on": False}, SensiboError("The unit didn't take it.")])
            result = await app.use_aircon(args("set", on=False))
            self.assertEqual(result["status"], "partly")
            self.assertEqual(result["failed"], ["Declan’s Room: The unit didn't take it."])
            self.assertIn("which didn't change", result["message"])

    async def test_timers_are_set_and_cancelled(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.ready(app)
            app.sensibo.set_timer = AsyncMock()
            app.sensibo.cancel_timer = AsyncMock(return_value=False)
            result = await app.use_aircon(args("timer", "declan", on=False, minutes=120))
            unit, minutes, state = app.sensibo.set_timer.await_args.args
            self.assertEqual((unit, minutes, state["on"]), ("BpP9b5Ka", 120, False))
            self.assertEqual(result["done"], ["Declan’s Room: in 120 minutes, off"])
            cancelled = await app.use_aircon(args("cancel_timer", "declan"))
            self.assertEqual(cancelled["done"], ["Declan’s Room: had no timer"])

    async def test_her_room_reaches_her_context_and_leaves_it_when_switched_off(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.ready(app)
            await app.update_her_room()
            self.assertEqual(json.loads(app.live.room_context)["room"], "Office")
            self.assertEqual(app.home_state, "ok")
            self.assertIn("Office 20.8°C 75%", app.home_status)
            self.assertEqual(app.home_rooms, [("Jyt7jM8c", "Office"), ("BpP9b5Ka", "Declan’s Room")])
            app.sensibo_enabled = False
            await app.update_her_room()
            self.assertEqual(app.live.room_context, "")

    async def test_a_room_gone_from_the_account_is_said_once(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.ready(app, her_room="Gone1234")
            await app.update_her_room()
            await app.update_her_room()
            self.assertEqual(app.live.room_context, "")
            self.assertEqual(app.home_state, "problem")
            self.assertEqual(sum("no longer on the Sensibo account" in entry for entry in app.activity.entries), 1)

    async def test_the_sensibo_row_switches_it_on_tests_and_picks_her_room(self):
        from textual.widgets import Button, Select, Static, Switch
        from test_model_settings_screen import MODELS
        from ui.model_settings import ModelSettingsScreen

        async with keyboard_tests.running_app() as (app, pilot):
            app.persistence.load_models.return_value = MODELS
            app.sensibo.set_key("k" * 30)
            app.sensibo.rooms = AsyncMock(return_value=(parse_rooms(ROOMS), 0.0))
            await pilot.click("#settings-button")
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            await pilot.pause()
            self.assertIsInstance(app.screen, ModelSettingsScreen)
            await asyncio.wait_for(app.screen.all_built.wait(), 5)
            screen = app.screen
            self.assertIn("off", str(screen.query_one("#home-status", Static).render()))
            screen.query_one("#home-enabled", Switch).value = True
            await pilot.pause()
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            self.assertTrue(app.sensibo_enabled)
            app.persistence.save_sensibo_enabled.assert_called_once_with(True)
            self.assertTrue(app.service_tools_changed)
            # Its details open from its row.
            await pilot.click("#service-title-home")
            await pilot.pause()
            self.assertTrue(screen.query_one("#service-details-home").display)
            # Below the fold of the scrolling tab, so pressed rather than clicked.
            screen.query_one("#home-test", Button).press()
            await pilot.pause()
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            await screen.show_services()
            await pilot.pause()
            self.assertIn("2 rooms", str(screen.query_one("#home-status", Static).render()))
            choice = screen.query_one("#home-her-room", Select)
            choice.value = "Jyt7jM8c"
            await pilot.pause()
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            app.persistence.save_her_room.assert_called_once_with("Jyt7jM8c")
            self.assertEqual(app.her_room_id, "Jyt7jM8c")
            self.assertIn("20.8°C", app.live.room_context)

    async def test_settings_are_loaded_at_launch_with_her_room_read(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.persistence.load_home_settings.return_value = {"sensibo": True, "her_room": "Jyt7jM8c"}
            with patch("app_areas.home.sensibo_key", return_value="k" * 30), \
                    patch.object(Sensibo, "rooms", AsyncMock(return_value=(parse_rooms(ROOMS), 0.0))):
                await app.load_home_settings()
            self.assertTrue(app.sensibo_ready())
            self.assertIn("20.8°C", app.live.room_context)
            self.assertIsNotNone(app.her_room_timer)


if __name__ == "__main__":
    unittest.main()
