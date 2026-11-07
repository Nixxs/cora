"""Hang up a phone call that isn't the person's (a contact's, or one she placed for a task), once
she's said goodbye.

Only those calls' routes can end one (they carry end_call); everywhere else the tool isn't offered,
and if called anyway it says there's no call to end. The call ends after her goodbye has finished
playing down the line.
"""

import json

# Offered only on those calls: never in the person's own sessions and chats.
CALL_ONLY = frozenset({"end_call"})

SCHEMA = {
    "type": "function",
    "name": "end_call",
    "description": (
        "Hang up this phone call. Only once the conversation is over and you've said goodbye (or left "
        "your voicemail message); the call ends as soon as your goodbye has been heard. Never mid-conversation."
    ),
    "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    "strict": True,
}

ENDING = {"status": "ok", "message": "The call is ending. Say nothing more."}
NO_CALL = {"status": "error", "message": "There's no call here to end."}


def validate_arguments(arguments: dict) -> None:
    if arguments != {}:
        raise ValueError("end_call takes no arguments")


class EndCallTool:
    def __init__(self, get_connection, is_closing, write_debug):
        self.get_connection = get_connection
        self.is_closing = is_closing
        self.write_debug = write_debug

    async def handle(self, call_id: str, arguments: dict) -> None:
        try:
            if self.is_closing():
                return
            connection = self.get_connection()
            if connection is None:
                return
            end = getattr(connection, "end_call", None)
            await connection.response.item.create(item={
                "type": "function_call_output", "call_id": call_id,
                "output": json.dumps(ENDING if end is not None else NO_CALL),
            })
            if end is None:
                await connection.response.create()
                return
            # No continuation: she has said goodbye, and the call closes once that's been heard.
            end()
        except Exception as error:
            try:
                self.write_debug(f"end_call failed ({type(error).__name__})", "Tool error")
            except Exception:
                pass
