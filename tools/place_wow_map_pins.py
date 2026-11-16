"""Let Cora put numbered, labelled pins on the person's World of Warcraft map (their CoraMapPins addon).

The pins are typed into the game's chat box as the addon's /cmp cora commands (game/map_pins.py),
through the same keyboard bridge as push-to-talk, only while WoW is the window in front; otherwise
they wait until it is, for a couple of minutes. Nothing comes back from the game: she learns that the
commands were typed, not that the pins appeared.

No approval is asked, unlike other tools that change things outside Cora: the person asks for the pins
while playing, an approval would mean leaving the game, and all she can type is the addon's three
fixed commands built from checked values, never chat or any other command. Not offered on Signal or
the web page (backend_chat.py), where the person usually isn't at the game.
"""

import json

from game.map_pins import MAX_LABEL_CHARS, MAX_ROUTE_CHARS, MAX_STEPS, pin_lines, route_name

SCHEMA = {
    "type": "function",
    "name": "place_wow_map_pins",
    "description": (
        "Put numbered, labelled pins on the person's World of Warcraft world map (their CoraMapPins "
        "addon), with a box on their screen showing the next step's distance and direction. Use it "
        "when they ask you to mark something, or ask where something is in the game, such as a quest's "
        "destination or a quest chain's steps across zones (in order). Only use coordinates you have "
        "from a reliable source such as Wowhead or a guide, for their version of the game (classic "
        "era); never guess or estimate them, and say so instead if you can't find them. A route with "
        "the same name replaces the one placed before. You don't see the game: you only learn whether "
        "the pins were typed into it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "route": {"type": "string",
                      "description": f"A short name for the route, such as the quest's name (up to {MAX_ROUTE_CHARS} characters)."},
            "steps": {
                "type": "array",
                "description": f"1 to {MAX_STEPS} pins, in the order to visit them.",
                "items": {
                    "type": "object",
                    "properties": {
                        "zone": {"type": "string",
                                 "description": "The zone as the world map names it, such as 'Arathi Highlands' or "
                                                "'Stormwind City'; not a subzone or town such as Goldshire."},
                        "x": {"type": "number", "description": "The map x coordinate, 0 to 100, as Wowhead gives it."},
                        "y": {"type": "number", "description": "The map y coordinate, 0 to 100, as Wowhead gives it."},
                        "label": {"type": "string",
                                  "description": f"What to do there, in a few words (up to {MAX_LABEL_CHARS} characters)."},
                    },
                    "required": ["zone", "x", "y", "label"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["route", "steps"],
        "additionalProperties": False,
    },
    "strict": True,
}

NOT_SET_UP_RESULT = {"status": "error", "message": "Placing map pins in the game isn't available here."}
ERROR_RESULT = {"status": "error", "message": "The pins couldn't be typed into the game; say so."}


def validate_arguments(arguments: dict) -> None:
    if not isinstance(arguments, dict) or set(arguments) != {"route", "steps"}:
        raise ValueError("place_wow_map_pins requires exactly 'route' and 'steps'")
    pin_lines(arguments["route"], arguments["steps"])


class PlaceWowMapPinsTool:
    def __init__(self, get_connection, is_closing, place_pins, write_debug):
        # place_pins(route, lines) -> a result dict: typed now, waiting for the game, or an error.
        self.get_connection = get_connection
        self.is_closing = is_closing
        self.place_pins = place_pins
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
            lines = pin_lines(arguments["route"], arguments["steps"])
            try:
                result = await self.place_pins(route_name(arguments["route"]), lines)
            except Exception as error:
                self.write_debug(f"Placing WoW map pins failed ({type(error).__name__})", "Tool error")
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
            self.write_debug(f"place_wow_map_pins failed ({type(error).__name__})", "Tool error")
