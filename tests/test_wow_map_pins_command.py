"""The CoraMapPins addon's other commands (wow_map_pins_command, game/map_pins.py command_line): the one
line typed for each, everything refused, and typing them in order with map pins only while WoW is in
front. Keyboard and window checks are mocked; nothing is typed into a game."""

import asyncio
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from backend_chat import SIGNAL_EXCLUDED, STANDBY_EXCLUDED, WEB_EXCLUDED
from game.chat_line import MAX_MESSAGE_CHARS
from game.map_pins import COMMANDS, MAX_LABEL_CHARS, MAX_ROUTE_CHARS, command_line, is_addon_line, pin_lines
from tools import create_tool_registry
from tools.wow_map_pins_command import ERROR_RESULT, SCHEMA, WowMapPinsCommandTool, validate_arguments
import test_keyboard_persistence as keyboard_tests

ADDON_COMMANDS = Path(os.environ.get(
    "CORA_MAP_PINS_ADDON", r"F:\World of Warcraft\_forever_\Interface\AddOns\CoraMapPins",
)) / "Commands.lua"

# (command, argument) -> the line typed.
TYPED = {
    ("clear", "all"): "/cmp clear all",
    ("clear", " ALL "): "/cmp clear all",
    ("clear", "Sleeping  bag test"): "/cmp clear Sleeping bag test",
    ("add", "Westfall 56.3 47.5 Sentinel Hill"): "/cmp add Westfall 56.3 47.5 Sentinel Hill",
    ("add", "Un\u2019Goro Crater 45, 50"): "/cmp add Un'Goro Crater 45.0 50.0",
    ("add", "56.25 47"): "/cmp add 56.2 47.0",
    ("add", "#1429 42.1 65.9 Goldshire inn"): "/cmp add #1429 42.1 65.9 Goldshire inn",
    ("here", " my \u201cspot\u201d "): '/cmp here my "spot"',
    ("here", ""): "/cmp here",
    ("list", ""): "/cmp list",
    ("list", "Sleeping"): "/cmp list Sleeping",
    ("route", "Sleeping bag test"): "/cmp route Sleeping bag test",
    ("remove", "12"): "/cmp remove 12",
    ("next", ""): "/cmp next",
    ("show", ""): "/cmp show",
    ("show", "2"): "/cmp show 2",
    ("done", ""): "/cmp done",
    ("done", "3"): "/cmp done 3",
    ("undo", "1"): "/cmp undo 1",
    ("reset", ""): "/cmp reset",
    ("way", "4"): "/cmp way 4",
    ("tracker", "Off"): "/cmp tracker off",
    ("tracker", "on"): "/cmp tracker on",
    ("tracker", "reset"): "/cmp tracker reset",
    ("labels", "off"): "/cmp labels off",
    ("status", ""): "/cmp status",
    ("mapdebug", " "): "/cmp mapdebug",
    ("help", ""): "/cmp help",
}

REFUSED = [
    # Not one of the addon's commands Cora may type: chat, other slash commands, the bridge, windows, aliases.
    ("say", "hello"), ("/s", "hello"), ("s", "hi"), ("cora", "end"), ("import", ""), ("export", ""),
    ("rm", "1"), ("ls", ""), ("CLEAR", "all"), ("clear all", ""), ("", ""), (None, ""), ("reload", ""),
    # A second line, WoW's escape character or other control characters: never cleaned, refused.
    ("clear", "all\n/s hello everyone"), ("clear", "all\r/g hi"), ("done", "1\n"), ("labels", "on|cffff0000"),
    ("here", "spot\u0007"), ("list", "\u200bSleeping"),
    # Arguments the command doesn't take.
    ("status", "now"), ("next", "1"), ("help", "add"), ("clear", ""), ("clear", "  "), ("remove", ""),
    ("remove", "x"), ("remove", "0"), ("remove", "-1"), ("remove", "1.5"), ("remove", "1234567"),
    ("done", "next"), ("show", "0"), ("tracker", ""), ("tracker", "maybe"), ("labels", "reset"),
    ("add", ""), ("add", "Westfall"), ("add", "/s 1 2"), ("add", "Westfall 200 3"), ("add", "Westfall 3 101"),
    ("add", "Westf4ll 1 2"), ("add", "#abc 1 2"), ("clear", 3), ("done", 2), ("tracker", None),
]


class CommandLineTests(unittest.TestCase):
    def test_each_command_types_one_checked_line(self):
        for (command, argument), line in TYPED.items():
            with self.subTest(command=command, argument=argument):
                self.assertEqual(command_line(command, argument), line)
                self.assertTrue(is_addon_line(line))

    def test_every_command_is_covered(self):
        self.assertEqual({command for command, _ in TYPED}, set(COMMANDS))

    def test_anything_else_is_refused(self):
        for command, argument in REFUSED:
            with self.subTest(command=command, argument=argument), self.assertRaises(ValueError):
                command_line(command, argument)

    def test_long_names_and_labels_are_cut_to_fit_one_line(self):
        self.assertEqual(len(command_line("route", "r" * 300)), len("/cmp route ") + MAX_ROUTE_CHARS)
        self.assertEqual(len(command_line("here", "word " * 100)) <= len("/cmp here ") + MAX_LABEL_CHARS, True)
        line = command_line("add", "Westfall 56.3 47.5 " + "x" * 400)
        self.assertLessEqual(len(line), MAX_MESSAGE_CHARS)
        self.assertTrue(is_addon_line(line))

    def test_the_last_guard_takes_only_the_addons_lines(self):
        for line in pin_lines("r", [{"zone": "Westfall", "x": 1, "y": 2, "label": "a"}]):
            self.assertTrue(is_addon_line(line))
        for line in ("/s hello", "hello", "/cmp", "/cmp clear all\n/s hi", "/cmp clear all|r", "/cmpx status",
                     "/cmp import", "/cmp export", "/cmp Clear all", " /cmp status", "/cmp status ",
                     "/cmp  status", "/cmp " + "a" * MAX_MESSAGE_CHARS, None, b"/cmp status"):
            with self.subTest(line=line):
                self.assertFalse(is_addon_line(line))

    @unittest.skipUnless(ADDON_COMMANDS.exists(), "the CoraMapPins addon isn't on this PC")
    def test_the_addon_still_has_every_command(self):
        source = ADDON_COMMANDS.read_text(encoding="utf-8")
        defined = set(re.findall(r"^commands\.([a-z]+)\s*=", source, re.MULTILINE))
        self.assertLessEqual(set(COMMANDS), defined)
        self.assertIn('SLASH_CORAMAPPINS1 = "/cmp"', source)


class SchemaTests(unittest.TestCase):
    def test_schema_is_strict_with_the_commands_as_its_choices(self):
        parameters = SCHEMA["parameters"]
        self.assertTrue(SCHEMA["strict"])
        self.assertEqual(set(parameters["required"]), set(parameters["properties"]))
        self.assertFalse(parameters["additionalProperties"])
        self.assertEqual(parameters["properties"]["command"]["enum"], list(COMMANDS))
        for command in COMMANDS:
            self.assertIn(f"- {command}:", SCHEMA["description"])

    def test_validate_matches_command_line(self):
        validate_arguments({"command": "clear", "argument": "all"})
        for arguments in ({"command": "clear"}, {"command": "status", "argument": "", "more": 1},
                          {"command": "say", "argument": "hi"}, {"command": "clear", "argument": "all\n/s hi"}, []):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                validate_arguments(arguments)

    def test_offered_at_the_desk_but_not_on_signal_or_the_web_page(self):
        self.assertNotIn("wow_map_pins_command", STANDBY_EXCLUDED)
        self.assertIn("wow_map_pins_command", SIGNAL_EXCLUDED)
        self.assertIn("wow_map_pins_command", WEB_EXCLUDED)

    def test_registered_with_a_safe_default(self):
        registry = create_tool_registry(get_connection=lambda: None, is_closing=lambda: False, shutdown=None,
                                        request_standby=None, write_debug=MagicMock(), schedule=MagicMock())
        self.assertIn(SCHEMA, registry.schemas())


def connection():
    create, respond = AsyncMock(), AsyncMock()
    return SimpleNamespace(response=SimpleNamespace(item=SimpleNamespace(create=create), create=respond)), create, respond


class ToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_runs_the_line_and_returns_the_result(self):
        conn, create, respond = connection()
        run = AsyncMock(return_value={"status": "ok", "typed": "/cmp clear all"})
        tool = WowMapPinsCommandTool(lambda: conn, lambda: False, run, MagicMock())
        await tool.handle("call_1", {"command": "clear", "argument": " All "})
        run.assert_awaited_once_with("/cmp clear all")
        item = create.call_args.kwargs["item"]
        self.assertEqual((item["type"], item["call_id"]), ("function_call_output", "call_1"))
        self.assertEqual(json.loads(item["output"]), {"status": "ok", "typed": "/cmp clear all"})
        respond.assert_awaited_once()

    async def test_a_failure_is_one_error_result(self):
        conn, create, _ = connection()
        debug = MagicMock()
        tool = WowMapPinsCommandTool(lambda: conn, lambda: False, AsyncMock(side_effect=RuntimeError()), debug)
        await tool.handle("call_1", {"command": "status", "argument": ""})
        self.assertEqual(json.loads(create.call_args.kwargs["item"]["output"]), ERROR_RESULT)
        self.assertIn("RuntimeError", debug.call_args.args[0])

    async def test_nothing_happens_while_closing(self):
        conn, create, _ = connection()
        run = AsyncMock()
        await WowMapPinsCommandTool(lambda: conn, lambda: True, run, MagicMock()).handle(
            "c", {"command": "status", "argument": ""})
        run.assert_not_awaited()
        create.assert_not_awaited()

    async def test_the_registry_refuses_an_injected_line_before_the_tool_runs(self):
        run, schedule = AsyncMock(), MagicMock()
        registry = create_tool_registry(get_connection=lambda: None, is_closing=lambda: False, shutdown=None,
                                        request_standby=None, write_debug=MagicMock(), schedule=schedule,
                                        run_map_pins_command=run)
        for arguments in ({"command": "clear", "argument": "all\n/s hi"}, {"command": "say", "argument": "hi"}):
            scheduled = registry.handle_function_call({
                "type": "function_call", "name": "wow_map_pins_command", "call_id": f"c{len(arguments)}",
                "arguments": json.dumps(arguments),
            })
            self.assertFalse(scheduled)
        schedule.assert_not_called()
        run.assert_not_awaited()


ROUTE = ["/cmp cora begin 1 r", "/cmp cora pin Westfall 56.3 47.5 Sentinel Hill", "/cmp cora end"]


class TypingTests(unittest.IsolatedAsyncioTestCase):
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

    @staticmethod
    def typed_lines(typed):
        return [call.args[0] for call in typed.call_args_list]

    async def test_typed_straight_away_while_wow_is_in_front(self):
        async with keyboard_tests.running_app() as (app, pilot):
            typed = self.prepare([True])
            result = await app.run_game_map_pins_command("/cmp clear all")
            self.assertEqual(self.typed_lines(typed), ["/cmp clear all"])
            self.assertEqual((result["status"], result["typed"]), ("ok", "/cmp clear all"))
            self.assertIn("doesn't answer back", result["message"])
            self.assertIn("Cora typed into WoW: /cmp clear all", self.notes(app))

    async def test_waits_for_wow_and_keeps_the_order_asked(self):
        async with keyboard_tests.running_app() as (app, pilot):
            typed = self.prepare([False, False, False, False, True])
            first = await app.run_game_map_pins_command("/cmp clear all")
            second = await app.place_game_map_pins("r", ROUTE)
            third = await app.run_game_map_pins_command("/cmp tracker on")
            self.assertTrue(first["waiting"] and second["waiting"] and third["waiting"])
            self.assertEqual(first["command"], "/cmp clear all")
            typed.assert_not_called()
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            self.assertEqual(self.typed_lines(typed), ["/cmp clear all", *ROUTE, "/cmp tracker on"])
            self.assertEqual(app.game_pending_pins, [])
            notes = self.notes(app)
            self.assertIn("typed when the game is in front: /cmp clear all", notes)
            self.assertIn("Cora put 1 map pin in WoW: r", notes)
            self.assertIn("Cora typed into WoW: /cmp tracker on", notes)

    async def test_waiting_commands_go_first_once_wow_is_in_front(self):
        async with keyboard_tests.running_app() as (app, pilot):
            typed = self.prepare([False])
            await app.run_game_map_pins_command("/cmp clear all")
            with patch("app_areas.game.game_in_front", return_value=True):
                result = await app.place_game_map_pins("r", ROUTE)
                await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            self.assertEqual(result["placed"], 1)
            self.assertEqual(self.typed_lines(typed), ["/cmp clear all", *ROUTE])

    async def test_a_newer_route_replaces_a_waiting_route_but_not_waiting_commands(self):
        async with keyboard_tests.running_app() as (app, pilot):
            typed = self.prepare([False])
            await app.place_game_map_pins("old", ["/cmp cora begin 1 old", "/cmp cora pin Westfall 1.0 1.0", "/cmp cora end"])
            await app.run_game_map_pins_command("/cmp done")
            await app.place_game_map_pins("r", ROUTE)
            self.assertEqual([job.what for job in app.game_pending_pins], ["/cmp done", "r"])
            with patch("app_areas.game.game_in_front", return_value=True):
                await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            self.assertEqual(self.typed_lines(typed), ["/cmp done", *ROUTE], "the old route is never typed")

    async def test_gives_up_if_wow_never_comes_to_the_front(self):
        async with keyboard_tests.running_app() as (app, pilot):
            typed = self.prepare([False])
            with patch("app_areas.game.PIN_WAIT_SECONDS", 0.05):
                await app.run_game_map_pins_command("/cmp status")
                await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            typed.assert_not_called()
            self.assertEqual(app.game_pending_pins, [])
            self.assertIn("Not typed into WoW (it wasn't brought to the front): /cmp status", self.notes(app))

    async def test_a_keyboard_error_is_reported(self):
        async with keyboard_tests.running_app() as (app, pilot):
            typed = self.prepare([True])
            typed.side_effect = OSError("Windows took 0 of 4 key events")
            result = await app.run_game_map_pins_command("/cmp clear all")
            self.assertEqual(result["status"], "error")
            self.assertIn("Windows took 0 of 4", result["message"])
            self.assertIn("Not typed into WoW: /cmp clear all", self.notes(app))

    async def test_anything_but_the_addons_lines_is_never_typed(self):
        async with keyboard_tests.running_app() as (app, pilot):
            typed = self.prepare([True])
            for line in ("/s hello everyone", "/cmp clear all\n/s hi", "/cmp import", "/script print(1)"):
                with self.subTest(line=line):
                    result = await app.run_game_map_pins_command(line)
                    self.assertEqual(result["status"], "error")
            result = await app.place_game_map_pins("r", [*ROUTE[:2], "/g spam", ROUTE[2]])
            self.assertEqual(result["status"], "error")
            typed.assert_not_called()
            self.assertEqual(app.game_pending_pins, [])

    async def test_live_offers_the_tool(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.assertIn("wow_map_pins_command", [schema["name"] for schema in app.live.tool_schemas()])


if __name__ == "__main__":
    unittest.main()
