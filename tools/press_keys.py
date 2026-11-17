"""Press keys on the phone's keypad, on a call she placed for a task: an automated menu ("press 3
for new purchases") or a recording asking her to key in a number.

Only that call's route can press keys (it carries press_keys); everywhere else the tool isn't
offered, and if called anyway it says there's no call. The keys go down the line as keypad tones
(phone/dtmf.py), and she waits silently for what the line says next.
"""

import json
import re

KEYS = re.compile(r"[0-9*#]{1,32}")

SCHEMA = {
    "type": "function",
    "name": "press_keys",
    "description": (
        "Press keys on the phone keypad during this call: for an automated phone menu (\"press 3 for "
        "new purchases\") or when a recording asks you to key in a number. keys are the digits, * or # "
        "to press, in order. Choose the option that fits your brief; then say nothing and wait for "
        "what the line says next. Never for a person, who'd rather you just talk."
    ),
    "parameters": {
        "type": "object",
        "properties": {"keys": {"type": "string", "description": "The keys to press in order: digits, * and #."}},
        "required": ["keys"],
        "additionalProperties": False,
    },
    "strict": True,
}

NO_CALL = {"status": "error", "message": "There's no phone call here to press keys on."}


def validate_arguments(arguments: dict) -> None:
    if set(arguments) != {"keys"} or not isinstance(arguments["keys"], str) or not KEYS.fullmatch(arguments["keys"]):
        raise ValueError("press_keys takes keys: 1 to 32 of 0-9, * and #")


class PressKeysTool:
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
            press = getattr(connection, "press_keys", None)
            keys = arguments["keys"]
            if press is not None:
                press(keys)
            result = NO_CALL if press is None else {
                "status": "ok",
                "message": f"Pressed {keys}. Say nothing about it: wait silently for what the line says next.",
            }
            await connection.response.item.create(item={
                "type": "function_call_output", "call_id": call_id, "output": json.dumps(result),
            })
            await connection.response.create()
        except Exception as error:
            try:
                self.write_debug(f"press_keys failed ({type(error).__name__})", "Tool error")
            except Exception:
                pass
