"""Map pins in WoW (place_wow_map_pins, game/map_pins.py): the lines typed for the CoraMapPins addon,
what's refused, and typing them only while WoW is in front. Keyboard and window checks are mocked."""

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from backend_chat import SIGNAL_EXCLUDED, STANDBY_EXCLUDED, WEB_EXCLUDED
from game.chat_line import MAX_MESSAGE_CHARS
from game.map_pins import MAX_LABEL_CHARS, MAX_ROUTE_CHARS, MAX_STEPS, MAX_ZONE_CHARS, pin_lines, plain_text
from tools import create_tool_registry
from tools.place_wow_map_pins import ERROR_RESULT, SCHEMA, PlaceWowMapPinsTool, validate_arguments
import test_keyboard_persistence as keyboard_tests

# The example route the addon's own tests type through its /cmp cora handler (CoraMapPins/tests).
EXAMPLE_ROUTE = "Sleeping bag test"
EXAMPLE_STEPS = [
    {"zone": "Arathi Highlands", "x": 22.4, "y": 24.2, "label": "Talk to the quartermaster"},
    {"zone": "Wetlands", "x": 10, "y": 60.27, "label": "Menethil Harbor \u2014 boat"},
    {"zone": "Un\u2019Goro Crater".replace("\u2019", "'"), "x": 45, "y": 50,
     "label": "Label with | pipe and \u201cquotes\u201d"},
]
EXAMPLE_LINES = [
    "/cmp cora begin 3 Sleeping bag test",
    "/cmp cora pin Arathi Highlands 22.4 24.2 Talk to the quartermaster",
    "/cmp cora pin Wetlands 10.0 60.3 Menethil Harbor - boat",
    "/cmp cora pin Un'Goro Crater 45.0 50.0 Label with pipe and \"quotes\"",
    "/cmp cora end",
]
ADDON_FIXTURE = Path(os.environ.get(
    "CORA_MAP_PINS_ADDON", r"F:\World of Warcraft\_forever_\Interface\AddOns\CoraMapPins",
)) / "tests" / "bridge_lines.txt"


def step(**changes):
    return {"zone": "Westfall", "x": 56.3, "y": 47.5, "label": "Sentinel Hill", **changes}


class PinLineTests(unittest.TestCase):
    def test_the_example_route_makes_the_addons_lines(self):
        self.assertEqual(pin_lines(EXAMPLE_ROUTE, EXAMPLE_STEPS), EXAMPLE_LINES)

    @unittest.skipUnless(ADDON_FIXTURE.exists(), "the CoraMapPins addon isn't on this PC")
    def test_the_lines_match_what_the_addons_tests_type(self):
        fixture = [line.strip() for line in ADDON_FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(pin_lines(EXAMPLE_ROUTE, EXAMPLE_STEPS), fixture)

    def test_only_the_addons_commands_are_ever_typed_one_line_each(self):
        lines = pin_lines("x\n/s hello everyone", [step(label="ok\n/g spam|cffff0000 \u0007bell \U0001F600")])
        self.assertEqual(lines, ["/cmp cora begin 1 x /s hello everyone",
                                 "/cmp cora pin Westfall 56.3 47.5 ok /g spam cffff0000 bell",
                                 "/cmp cora end"])
        for line in lines:
            self.assertTrue(line.startswith("/cmp cora "))
            self.assertNotIn("\n", line)
            self.assertNotIn("|", line)

    def test_longest_values_still_fit_wows_chat_box(self):
        lines = pin_lines("r" * 100, [step(zone="Z" * MAX_ZONE_CHARS, label="word " * 50)] * MAX_STEPS)
        self.assertEqual(len(lines), MAX_STEPS + 2)
        self.assertTrue(all(len(line) <= MAX_MESSAGE_CHARS for line in lines))
        self.assertEqual(len(lines[0]), len("/cmp cora begin 10 ") + MAX_ROUTE_CHARS)
        label = lines[1].split("56.3 47.5 ", 1)[1]
        self.assertLessEqual(len(label), MAX_LABEL_CHARS)
        self.assertTrue(label.endswith("word"), "cut at a word")

    def test_an_empty_label_is_allowed(self):
        self.assertEqual(pin_lines("r", [step(label="  ")])[1], "/cmp cora pin Westfall 56.3 47.5")

    def test_bad_routes_and_steps_are_refused(self):
        bad = [
            ("", [step()]), ("ok", []), ("ok", [step()] * (MAX_STEPS + 1)), ("ok", "Westfall 1 1"),
            ("ok", [step(zone="Goldshire 2")]), ("ok", [step(zone="#1436")]), ("ok", [step(zone="/s hi")]),
            ("ok", [step(zone="Elwynn|cffForest")]), ("ok", [step(zone="X")]), ("ok", [step(zone="Z" * 41)]),
            ("ok", [step(x=-1)]), ("ok", [step(y=100.5)]), ("ok", [step(x=True)]), ("ok", [step(x="50")]),
            ("ok", [step(y=float("nan"))]), ("ok", [step(x=float("inf"))]), ("ok", [step(label=None)]),
            ("ok", [{"zone": "Westfall", "x": 1, "y": 1}]), ("ok", [step(extra=1)]), (None, [step()]),
        ]
        for route, steps in bad:
            with self.subTest(route=route, steps=steps), self.assertRaises(ValueError):
                pin_lines(route, steps)

    def test_coordinates_are_rounded_and_edges_allowed(self):
        self.assertEqual(pin_lines("r", [step(x=0, y=100), step(x=12.349, y=7.96)])[1:3],
                         ["/cmp cora pin Westfall 0.0 100.0 Sentinel Hill", "/cmp cora pin Westfall 12.3 8.0 Sentinel Hill"])

    def test_plain_text_keeps_ordinary_punctuation(self):
        self.assertEqual(plain_text("Kill 10 + loot $5 = win?", 80, "label"), "Kill 10 + loot $5 = win?")
        self.assertEqual(plain_text("Ras\u2019s caf\u00e9 \u2026 ok", 80, "label"), "Ras's caf\u00e9 ... ok")


class SchemaTests(unittest.TestCase):
    def test_schema_is_strict_all_the_way_down(self):
        parameters = SCHEMA["parameters"]
        self.assertTrue(SCHEMA["strict"])
        self.assertEqual(set(parameters["required"]), set(parameters["properties"]))
        self.assertFalse(parameters["additionalProperties"])
        item = parameters["properties"]["steps"]["items"]
        self.assertEqual(set(item["required"]), {"zone", "x", "y", "label"})
        self.assertFalse(item["additionalProperties"])

    def test_validate_matches_pin_lines(self):
        validate_arguments({"route": EXAMPLE_ROUTE, "steps": EXAMPLE_STEPS})
        for arguments in ({"route": "r"}, {"route": "r", "steps": [step()], "more": 1}, {"route": "r", "steps": [step(x=200)]}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                validate_arguments(arguments)

    def test_offered_at_the_desk_but_not_on_signal_or_the_web_page(self):
        self.assertNotIn("place_wow_map_pins", STANDBY_EXCLUDED)
        self.assertIn("place_wow_map_pins", SIGNAL_EXCLUDED)
        self.assertIn("place_wow_map_pins", WEB_EXCLUDED)

    def test_registered_with_a_safe_default(self):
        registry = create_tool_registry(get_connection=lambda: None, is_closing=lambda: False, shutdown=None,
                                        request_standby=None, write_debug=MagicMock(), schedule=MagicMock())
        self.assertIn(SCHEMA, registry.schemas())


def connection():
    create, respond = AsyncMock(), AsyncMock()
    return SimpleNamespace(response=SimpleNamespace(item=SimpleNamespace(create=create), create=respond)), create, respond


class ToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_places_the_lines_and_returns_the_result(self):
        conn, create, respond = connection()
        place = AsyncMock(return_value={"status": "ok", "placed": 3})
        tool = PlaceWowMapPinsTool(lambda: conn, lambda: False, place, MagicMock())
        await tool.handle("call_1", {"route": "  Sleeping   bag test ", "steps": EXAMPLE_STEPS})
        place.assert_awaited_once_with(EXAMPLE_ROUTE, EXAMPLE_LINES)
        item = create.call_args.kwargs["item"]
        self.assertEqual((item["type"], item["call_id"]), ("function_call_output", "call_1"))
        self.assertEqual(json.loads(item["output"]), {"status": "ok", "placed": 3})
        respond.assert_awaited_once()

    async def test_a_failure_is_one_error_result(self):
        conn, create, respond = connection()
        debug = MagicMock()
        tool = PlaceWowMapPinsTool(lambda: conn, lambda: False, AsyncMock(side_effect=RuntimeError()), debug)
        await tool.handle("call_1", {"route": "r", "steps": [step()]})
        self.assertEqual(json.loads(create.call_args.kwargs["item"]["output"]), ERROR_RESULT)
        self.assertIn("RuntimeError", debug.call_args.args[0])

    async def test_nothing_happens_while_closing(self):
        conn, create, _ = connection()
        place = AsyncMock()
        await PlaceWowMapPinsTool(lambda: conn, lambda: True, place, MagicMock()).handle("c", {"route": "r", "steps": [step()]})
        place.assert_not_awaited()
        create.assert_not_awaited()


class PlacingTests(unittest.IsolatedAsyncioTestCase):
    def prepare(self, in_front):
        """in_front: a list of answers to "is WoW in front?", the last one repeating."""
        answers = list(in_front)
        self.enterContext(patch("app_areas.game.game_in_front",
                                side_effect=lambda: answers.pop(0) if len(answers) > 1 else answers[0]))
        self.enterContext(patch("app_areas.game.PIN_LINE_GAP_SECONDS", 0))
        self.enterContext(patch("app_areas.game.PIN_CHECK_SECONDS", 0.01))
        return self.enterContext(patch("app_areas.game.type_into_game"))

    @staticmethod
    def notes(app):
        return "\n".join(map(str, app.conversation.log_entries))

    async def test_typed_straight_away_while_wow_is_in_front(self):
        async with keyboard_tests.running_app() as (app, pilot):
            typed = self.prepare([True])
            result = await app.place_game_map_pins(EXAMPLE_ROUTE, EXAMPLE_LINES)
            self.assertEqual([call.args[0] for call in typed.call_args_list], EXAMPLE_LINES)
            self.assertEqual((result["status"], result["placed"]), ("ok", 3))
            self.assertIn("doesn't answer back", result["message"])
            self.assertIn("Cora put 3 map pins in WoW: Sleeping bag test", self.notes(app))

    async def test_waits_for_wow_to_come_to_the_front(self):
        async with keyboard_tests.running_app() as (app, pilot):
            typed = self.prepare([False, False, False, True])
            result = await app.place_game_map_pins("r", EXAMPLE_LINES)
            self.assertTrue(result["waiting"])
            typed.assert_not_called()
            self.assertIn("typed when the game is in front: r", self.notes(app))
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            self.assertEqual([call.args[0] for call in typed.call_args_list], EXAMPLE_LINES)
            self.assertEqual(app.game_pending_pins, [])
            self.assertIn("Cora put 3 map pins in WoW: r", self.notes(app))

    async def test_gives_up_if_wow_never_comes_to_the_front(self):
        async with keyboard_tests.running_app() as (app, pilot):
            typed = self.prepare([False])
            with patch("app_areas.game.PIN_WAIT_SECONDS", 0.05):
                await app.place_game_map_pins("r", EXAMPLE_LINES)
                await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            typed.assert_not_called()
            self.assertIn("Map pins not placed (WoW wasn't brought to the front): r", self.notes(app))

    async def test_wow_leaving_the_front_part_way_stops_before_the_end_line(self):
        async with keyboard_tests.running_app() as (app, pilot):
            typed = self.prepare([True, True, True, False])
            result = await app.place_game_map_pins("r", EXAMPLE_LINES)
            self.assertEqual(result["status"], "error")
            self.assertEqual([call.args[0] for call in typed.call_args_list], EXAMPLE_LINES[:3])
            self.assertNotIn("/cmp cora end", [call.args[0] for call in typed.call_args_list])
            self.assertIn("Map pins not placed in WoW: r", self.notes(app))

    async def test_a_keyboard_error_is_reported(self):
        async with keyboard_tests.running_app() as (app, pilot):
            typed = self.prepare([True])
            typed.side_effect = OSError("Windows took 0 of 4 key events")
            result = await app.place_game_map_pins("r", EXAMPLE_LINES)
            self.assertEqual(result["status"], "error")
            self.assertIn("Windows took 0 of 4", result["message"])

    async def test_a_newer_route_replaces_one_still_waiting(self):
        async with keyboard_tests.running_app() as (app, pilot):
            typed = self.prepare([False])
            await app.place_game_map_pins("old", ["/cmp cora begin 1 old", "/cmp cora pin Westfall 1.0 1.0", "/cmp cora end"])
            with patch("app_areas.game.game_in_front", return_value=True):
                result = await app.place_game_map_pins("new", EXAMPLE_LINES)
                await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            self.assertEqual(result["placed"], 3)
            self.assertEqual([call.args[0] for call in typed.call_args_list], EXAMPLE_LINES, "the old one is never typed")

    async def test_live_offers_the_tool_while_the_game_is_in_use(self):
        async with keyboard_tests.running_app() as (app, pilot):
            # Not while nothing of the game is on and it isn't running: it would only cost tokens.
            self.assertNotIn("place_wow_map_pins", [schema["name"] for schema in app.live.tool_schemas()])
            app.wow_running = lambda: True
            self.assertIn("place_wow_map_pins", [schema["name"] for schema in app.live.tool_schemas()])
            app.wow_running = lambda: False
            app.game_push_to_talk = True
            self.assertIn("place_wow_map_pins", [schema["name"] for schema in app.live.tool_schemas()])


if __name__ == "__main__":
    unittest.main()
