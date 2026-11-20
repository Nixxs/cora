"""Her home: the air conditioners through Sensibo, and the room she lives in. Mocked HTTP only."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx2

from home import sensibo
from home.sensibo import (
    BadKey, FRESH_SECONDS, MIN_GAP_SECONDS, RateLimited, Sensibo, find_room, parse_rooms, reading, room_context,
    save_sensibo_key,
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
            "cool": {"temperatures": {"C": {"isNative": True, "values": [18, 19, 30]}, "F": {"values": [64, 86]}},
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
        self.assertEqual(office.modes["cool"], {"temperatures": [18, 19, 30], "fan": ["quiet", "low", "auto"],
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


class ToolTests(unittest.IsolatedAsyncioTestCase):
    def test_only_status_for_now(self):
        self.assertEqual(SCHEMA["parameters"]["properties"]["action"]["enum"], ["status"])
        validate_arguments({"action": "status", "room": None})
        validate_arguments({"action": "status", "room": "Office"})
        for bad in ({"action": "set", "room": None}, {"action": "status", "room": ""}, {"action": "status"}):
            with self.assertRaises(ValueError):
                validate_arguments(bad)

    async def test_the_reading_goes_back_to_her(self):
        connection = MagicMock()
        connection.response.item.create = AsyncMock()
        connection.response.create = AsyncMock()
        read = AsyncMock(return_value={"status": "ok", "rooms": []})
        await AirconTool(lambda: connection, lambda: False, read, MagicMock()).handle(
            "call_1", {"action": "status", "room": " Office "})
        read.assert_awaited_once_with("Office")
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
            result = await app.read_aircon(None)
            self.assertEqual([room["room"] for room in result["rooms"]], ["Office", "Declan’s Room"])
            self.assertIn("can't change the air conditioners yet", result["note"])
            one = await app.read_aircon("declan")
            self.assertEqual(one["rooms"][0]["humidity"], "86%")
            missing = await app.read_aircon("kitchen")
            self.assertEqual(missing["status"], "not_found")
            self.assertIn("Office", missing["rooms"])
            app.sensibo.rooms = AsyncMock(side_effect=RateLimited("Sensibo is limiting requests just now."))
            refused = await app.read_aircon(None)
            self.assertEqual(refused["status"], "error")
            self.assertIn("rather than guessing", refused["message"])

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
