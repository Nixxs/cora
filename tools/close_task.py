"""Close one of the person's tasks at their word: done some other way, or no longer wanted.

Only where the person is talking (the desk, voice, their own Signal chat): clients' chats are
offered only their own tools, so they can never close anything.
"""

from uuid import UUID

from .pass_on import PassOnTool

NOTE_MAX_CHARS = 1000

SCHEMA = {
    "type": "function",
    "name": "close_task",
    "description": (
        "Close one of the person's open tasks when they say so: 'done' when it's settled some "
        "other way (note what was settled), or 'cancelled' when they no longer want it (note why, "
        "briefly). The people involved stop being asked about it. Use the task_id from your tasks."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "status": {"type": "string", "enum": ["done", "cancelled"]},
            "note": {"type": "string", "minLength": 1, "maxLength": NOTE_MAX_CHARS},
        },
        "required": ["task_id", "status", "note"],
        "additionalProperties": False,
    },
    "strict": True,
}


def validate_arguments(arguments: dict) -> None:
    if not isinstance(arguments, dict) or set(arguments) != {"task_id", "status", "note"}:
        raise ValueError("Invalid close_task arguments")
    if not isinstance(arguments["task_id"], str):
        raise ValueError("task_id must be a task's ID")
    UUID(arguments["task_id"])
    if arguments["status"] not in ("done", "cancelled"):
        raise ValueError("status must be done or cancelled")
    note = arguments["note"]
    if not isinstance(note, str) or not 1 <= len(note.strip()) <= NOTE_MAX_CHARS:
        raise ValueError(f"note must contain 1 to {NOTE_MAX_CHARS} characters")


class CloseTaskTool(PassOnTool):
    """relay(task_id, status, note) -> None once closed, else why not."""

    async def result(self, connection, arguments: dict) -> dict:
        problem = await self.relay(UUID(arguments["task_id"]), arguments["status"], arguments["note"].strip())
        if problem:
            return {"status": "not_done", "message": problem}
        return {"status": "ok", "message": f"The task is {arguments['status']}; nobody will be asked about it now."}
