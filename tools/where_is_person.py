"""Where the person is right now, as a place name, from their open web page (see web/location.py).

On demand only: she checks when it matters to what they asked, and never otherwise. Only in the
person's own chats with her (voice, the desk, the web page); never on Signal, by email, or in
anyone else's chat, and never on phone calls.
"""

import json

SCHEMA = {
    "type": "function",
    "name": "where_is_person",
    "description": (
        "Find out roughly where the person is right now: a place name (area, nearby street, town) "
        "with the accuracy in metres, from the location of their phone or tablet while your web "
        "page is open on it with location sharing on. Use it only when where they are matters to "
        "what they asked, such as what's near them, how long to get somewhere, or the weather "
        "there. Never to keep track of them, and don't mention their location otherwise. They see "
        "a note each time you check. A place checked in the last 10 minutes is reused unless "
        "refresh is true."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "refresh": {
                "type": "boolean",
                "description": "True only if they may have moved since the last check (for example, they said they're driving).",
            },
        },
        "required": ["refresh"],
        "additionalProperties": False,
    },
    "strict": True,
}

NOTE = (
    "Where the person is, from their device; use it only for what they asked, and don't repeat "
    "the exact place back unless it helps."
)
# Why there's no place, said so she can tell them plainly (see web/location.py for the reasons).
WHY_NOT = {
    "no_page": "Your web page isn't open on any of their devices, so you can't check where they are. Ask them if it matters.",
    "off": ("Location sharing is off on your web page (they can turn it on with the 'where' switch "
            "at the top of the chat). Ask them where they are if it matters."),
    "denied": ("Their device didn't let your web page use its location; they can allow it in the "
               "device's settings. Ask them where they are if it matters."),
    "unavailable": ("Their device couldn't get a location just now (the page may be in the background). "
                    "Ask them where they are if it matters."),
    "on_call": ("They're on a phone call with you, and where they are is never checked during a call. "
                "Ask them where they are if it matters."),
    "unnamed": "Their location came through but the place couldn't be named just now. Ask them where they are if it matters.",
}
ERROR_RESULT = {"status": "error", "message": "Could not check where they are; say so rather than guessing."}


def validate_arguments(arguments: dict) -> None:
    if not isinstance(arguments, dict) or set(arguments) != {"refresh"} or not isinstance(arguments["refresh"], bool):
        raise ValueError("where_is_person requires exactly 'refresh' (true or false)")


class WhereIsPersonTool:
    def __init__(self, get_connection, is_closing, locate, write_debug):
        # locate(refresh) -> the result for Cora (see WebInterfaceMixin.locate_person).
        self.get_connection = get_connection
        self.is_closing = is_closing
        self.locate = locate
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
            validate_arguments(arguments)
            try:
                result = await self.locate(arguments["refresh"])
            except Exception as error:
                self.write_debug(f"Checking where you are failed ({type(error).__name__})", "Tool error")
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
            self.write_debug(f"where_is_person failed ({type(error).__name__})", "Tool error")
