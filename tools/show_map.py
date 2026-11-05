"""Let Cora show a map on the web page: a place, a route, or an area search (WB7).

The page shows an interactive Google map with an "Open in Google Maps" link; the desk's chat
shows a line with the link. She doesn't see the map; she only learns it was shown. Offered only
when a Google Maps key is set (Settings › Services › Web interface).
"""

import json

from web.maps import DIRECTIONS, MODES, TRAVEL_MODES, MapView

PLACE_MAX_CHARS = 300

SCHEMA = {
    "type": "function",
    "name": "show_map",
    "description": (
        "Show the person a small interactive Google map in the chat on your web page, with a link "
        "that opens Google Maps. Use it when a map would help: where a place is (place), a route "
        "between two places (directions), or places of a kind in an area (search, such as 'cafes "
        "near Leederville, Perth'). Use names or addresses a map search would find; for a route "
        "from where the person is, check with where_is_person first. You do not see the map; you "
        "only learn whether it was shown, so give any times or distances only if you know them."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": list(MODES),
                     "description": "place, directions or search."},
            "place": {"type": "string", "minLength": 1, "maxLength": PLACE_MAX_CHARS,
                      "description": "The place (place), the destination (directions), or what to search for, with the area (search)."},
            "origin": {"type": ["string", "null"], "maxLength": PLACE_MAX_CHARS,
                       "description": "Where the route starts (directions only); null otherwise."},
            "travel_mode": {"type": ["string", "null"], "enum": [*TRAVEL_MODES, None],
                            "description": "driving, walking, bicycling or transit (directions only; driving if null)."},
        },
        "required": ["mode", "place", "origin", "travel_mode"],
        "additionalProperties": False,
    },
    "strict": True,
}

SHOWN_RESULT = {
    "status": "ok",
    "message": "The map is in the chat on the web page, with a Google Maps link. You have not seen it.",
}
NOT_SET_UP_RESULT = {
    "status": "error",
    "message": "Maps aren't set up. Give a Google Maps link instead (https://www.google.com/maps/search/?api=1&query=…).",
}
ERROR_RESULT = {"status": "error", "message": "The map could not be shown; say so, or give a Google Maps link instead."}


def map_view(arguments: dict) -> MapView:
    """The map asked for; ValueError if the arguments don't make one."""
    if not isinstance(arguments, dict) or set(arguments) != {"mode", "place", "origin", "travel_mode"}:
        raise ValueError("show_map requires exactly 'mode', 'place', 'origin' and 'travel_mode'")
    mode, place, origin, travel = (arguments[key] for key in ("mode", "place", "origin", "travel_mode"))
    if mode not in MODES:
        raise ValueError("mode must be place, directions or search")
    if not isinstance(place, str) or not 1 <= len(place.strip()) <= PLACE_MAX_CHARS:
        raise ValueError(f"place must contain 1 to {PLACE_MAX_CHARS} characters")
    if origin is not None and (not isinstance(origin, str) or len(origin) > PLACE_MAX_CHARS):
        raise ValueError("origin must be a place or null")
    if travel is not None and travel not in TRAVEL_MODES:
        raise ValueError("travel_mode must be driving, walking, bicycling, transit or null")
    if mode == DIRECTIONS:
        if not (origin or "").strip():
            raise ValueError("directions need an origin")
        return MapView(DIRECTIONS, place.strip(), origin.strip(), travel or "driving")
    return MapView(mode, place.strip())


def validate_arguments(arguments: dict) -> None:
    map_view(arguments)


class ShowMapTool:
    def __init__(self, get_connection, is_closing, show_map, write_debug):
        # show_map(view) -> False when maps aren't set up (no key).
        self.get_connection = get_connection
        self.is_closing = is_closing
        self.show_map = show_map
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
            view = map_view(arguments)
            try:
                result = SHOWN_RESULT if self.show_map(view) else NOT_SET_UP_RESULT
            except Exception as error:
                self.write_debug(f"Showing a map failed ({type(error).__name__})", "Tool error")
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
            self.write_debug(f"show_map failed ({type(error).__name__})", "Tool error")
