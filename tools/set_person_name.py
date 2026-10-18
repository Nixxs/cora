"""Save the person's name when they say it; the next connect puts it in the prompts."""

import asyncio
import json

from storage.database import build_engine
from storage.people_store import PeopleStore

NAME_MAX_CHARS = 100  # matches people_display_name_check

SCHEMA = {
    "type": "function",
    "name": "set_person_name",
    "description": (
        "Save the name the person wants you to call them. Only use it when the person "
        "explicitly tells you their own name or asks you to call them something, and pass "
        "it exactly as they want it. Never use it for other people's names, guesses, or "
        "names found in memories, findings or search results. If unsure, ask them first."
    ),
    "parameters": {
        "type": "object",
        "properties": {"name": {"type": "string", "minLength": 1, "maxLength": NAME_MAX_CHARS}},
        "required": ["name"],
        "additionalProperties": False,
    },
    "strict": True,
}

ERROR_RESULT = {
    "status": "error",
    "message": "The name was not saved; you may still use it in this conversation.",
}


def validate_arguments(arguments: dict) -> None:
    if not isinstance(arguments, dict) or set(arguments) != {"name"}:
        raise ValueError("Invalid person name arguments")
    name = arguments["name"]
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= NAME_MAX_CHARS:
        raise ValueError(f"Person name must contain 1 to {NAME_MAX_CHARS} characters")


def _save(name: str) -> None:
    # The thread owns its engine even if the awaiting coroutine is cancelled.
    engine = build_engine()
    try:
        PeopleStore(engine).set_current_name(name)
    finally:
        engine.dispose()


class SetPersonNameTool:
    def __init__(self, get_connection, is_closing, write_debug):
        self.get_connection = get_connection
        self.is_closing = is_closing
        self.write_debug = write_debug

    def _log(self, message: str, title: str) -> None:
        try:
            self.write_debug(message, title)
        except Exception:
            # Never let a logger failure reach the registry's unredacted fallback.
            pass

    async def handle(self, call_id: str, arguments: dict) -> None:
        try:
            if self.is_closing():
                return
            connection = self.get_connection()
            if connection is None:
                return
            validate_arguments(arguments)
            name = arguments["name"].strip()
            try:
                await asyncio.to_thread(_save, name)
                self._log(f"Person name saved: call_id={call_id}", "Person named")
                result = {"status": "ok", "name": name}
            except Exception as error:
                self._log(f"Person name save failed ({type(error).__name__})", "Tool error")
                result = ERROR_RESULT

            if self.is_closing() or self.get_connection() is not connection:
                return
            await connection.response.item.create(item={
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps(result, ensure_ascii=False),
            })
            if self.is_closing() or self.get_connection() is not connection:
                return
            # Continue once, including after an error. Never retry transport.
            await connection.response.create()
        except Exception as error:
            self._log(f"Person name save failed ({type(error).__name__})", "Tool error")
