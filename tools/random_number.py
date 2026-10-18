"""an example of how to create tools for cora just generates a random number"""

import json
import random

SCHEMA = {
    "type": "function",
    "name": "random_number",
    "description": (
        "generates a random number between two given numbers and returns the number to Cora. "
        "This is an example to demonstrate how to create additional tools for Cora. "
    ),
    "parameters" : {
        "type": "object",
        "properties": {
            "minimum": {"type": "integer", "description": "Lowest number allowed."},
            "maximum": {"type": "integer", "description": "Highest number allowed."},
        },
        "required": ["minimum", "maximum"],
        "additionalProperties" : False
    },
    "strict": True
}

def validate_arguments(arguments: dict) -> None:
    if set(arguments) != {"minimum", "maximum"}:
        raise ValueError("random_number requires exactly 'minimum' and 'maximum'")
    low, high = arguments["minimum"], arguments["maximum"]
    for value in (low, high):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("minimum and maximum must be integers")
    if low > high:
        raise ValueError("minimum must not be greater than maximum")

class RandomNumberTool:
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

            # do the thing
            result = {"status": "ok", "random_number":random.randint(arguments["minimum"], arguments["maximum"])}

            if self.is_closing() or self.get_connection() is not connection:
                return
            await connection.response.item.create(item={
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps(result),
            })
            if self.is_closing() or self.get_connection() is not connection:
                return
            await connection.response.create()

        except Exception as error:
            self.write_debug(f"Random number generation failed ({type(error).__name__})", "Tool error")
