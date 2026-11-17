"""Let Cora run one of the CoraMapPins addon's own /cmp commands in the person's World of Warcraft game:
clear pins, mark steps done, switch the tracker, add a single pin and the rest (game/map_pins.py
COMMANDS). Whole routes still go through place_wow_map_pins, which places them all at once.

The command is typed into the game's chat box as one "/cmp <command> <argument>" line, through the same
keyboard bridge and the same wait for WoW to be in front as the pins (app_areas/game.py). Nothing comes
back from the game: she learns that the line was typed, not what the addon answered.

No approval is asked, as for the pins: the person asks while playing, and all she can type is one of the
addon's commands, picked from a fixed list, with its argument checked for that command. Anything else,
such as chat, another slash command or a second line, is refused, never cleaned into something typed.
Not offered on Signal or the web page (backend_chat.py), where the person usually isn't at the game.
"""

import json

from game.map_pins import COMMANDS, MAX_LABEL_CHARS, MAX_ROUTE_CHARS, command_line

SCHEMA = {
    "type": "function",
    "name": "wow_map_pins_command",
    "description": (
        "Run one of the person's CoraMapPins addon commands in their World of Warcraft game (the addon "
        "behind place_wow_map_pins), typed as '/cmp <command> <argument>'. Use it when they ask you to "
        "change their map pins or the next-step box: clear a route or every pin, mark a step done or not "
        "done, pick the active route, switch the tracker box or map labels, open the map at a step, set "
        "the game's own waypoint, or add one pin. For a route of several steps, use place_wow_map_pins "
        "instead. Only clear pins, especially 'all', when they ask. The addon prints its answer (such as "
        "the list of routes) in their chat, which you don't see: you only learn whether the command was "
        "typed. Commands and their argument ('' when none):\n"
        "- add: '[zone] <x> <y> [label]', one pin in the 'Manual' route; without a zone, their current "
        "zone. Coordinates only from a reliable source, never guessed.\n"
        "- here: '[label]', a pin where they stand.\n"
        "- list: '[route]', routes, and the steps with pin ids.\n"
        "- route: '[route]', list routes, or make one the active route (a unique start of its name works).\n"
        "- remove: '<pin id>', one pin, by the id list shows.\n"
        "- clear: '<route>' or 'all', remove one route, or every pin.\n"
        "- next: '', print the next step with its distance and direction.\n"
        "- show: '[step]', open the map at a step (the next one by default); not in combat.\n"
        "- done: '[step]', mark a step done (the next one by default).\n"
        "- undo: '[step]', mark a step not done (the last done one by default).\n"
        "- reset: '[route]', mark every step of a route not done (the active one by default).\n"
        "- way: '[step]', also set the game's own map waypoint, where the game allows it.\n"
        "- tracker: 'on', 'off' or 'reset', show or hide the next-step box; reset moves it back to the top.\n"
        "- labels: 'on' or 'off', label text beside the pins on the map.\n"
        "- status: '', the addon's diagnostics, if they say pins don't appear.\n"
        "- mapdebug: '', with their map open: where the pins were drawn, for a pin in the wrong place.\n"
        "- help: '', the addon's list of commands, in their chat."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {"type": "string",
                        "enum": list(COMMANDS),
                        "description": "The addon's command."},
            "argument": {"type": "string",
                         "description": (f"What follows the command, as listed for it, or '' for none. One "
                                         f"line of plain text; route names up to {MAX_ROUTE_CHARS} "
                                         f"characters, labels up to {MAX_LABEL_CHARS}.")},
        },
        "required": ["command", "argument"],
        "additionalProperties": False,
    },
    "strict": True,
}

ERROR_RESULT = {"status": "error", "message": "The command couldn't be typed into the game; say so."}


def validate_arguments(arguments: dict) -> None:
    if not isinstance(arguments, dict) or set(arguments) != {"command", "argument"}:
        raise ValueError("wow_map_pins_command requires exactly 'command' and 'argument'")
    command_line(arguments["command"], arguments["argument"])


class WowMapPinsCommandTool:
    def __init__(self, get_connection, is_closing, run_command, write_debug):
        # run_command(line) -> a result dict: typed now, waiting for the game, or an error.
        self.get_connection = get_connection
        self.is_closing = is_closing
        self.run_command = run_command
        self.write_debug = write_debug

    def _stale(self, connection) -> bool:
        return self.is_closing() or self.get_connection() is not connection

    async def handle(self, call_id: str, arguments: dict) -> None:
        try:
            if self.is_closing():
                return
            connection = self.get_connection()
            if connection is None:
                return
            line = command_line(arguments["command"], arguments["argument"])
            try:
                result = await self.run_command(line)
            except Exception as error:
                self.write_debug(f"Running a CoraMapPins command failed ({type(error).__name__})", "Tool error")
                result = ERROR_RESULT
            if self._stale(connection):
                return
            await connection.response.item.create(item={
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps(result, ensure_ascii=False),
            })
            if self._stale(connection):
                return
            await connection.response.create()
        except Exception as error:
            self.write_debug(f"wow_map_pins_command failed ({type(error).__name__})", "Tool error")
