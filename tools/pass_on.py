"""In a client's Signal chat: pass something on to the person she works with, or report a task.

It only ever reaches the person, marked as coming from that client, so it needs no approval;
a daily limit per client keeps a chatty or hostile one from flooding them. Naming a task also
records their word on it, and the coordinator takes it to the person. Only offered in clients'
chats: the route says which client is asking.
"""

import json
from uuid import UUID

MESSAGE_MAX_CHARS = 3000
# Not offered at the desk, in Live or in the person's own chat: only clients have someone to pass things to.
CLIENT_ONLY = frozenset({"pass_on", "raise_concern", "stop_replying"})

SCHEMA = {
    "type": "function",
    "name": "pass_on",
    "description": (
        "Pass something on to the person you work with, marked as coming from this contact. Use "
        "it when they ask you to pass something on (a plan you worked out together, a question, "
        "a message), when something clearly needs the person's attention, or for a task you're "
        "asking them about: then name that task_id and give their word clearly and completely, "
        "so the task records it. settled is true when their part is settled (an answer, or a "
        "clear no), false when they need something from the person first (a question, a missing "
        "detail); without a task, task_id and settled are null. It reaches only the person you "
        "work with. Tell this contact you've passed it on; never promise a reply for them."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "message": {"type": "string", "minLength": 1, "maxLength": MESSAGE_MAX_CHARS},
            "task_id": {"type": ["string", "null"], "description": "The task this is about, or null."},
            "settled": {"type": ["boolean", "null"], "description": "With a task: is their part settled?"},
        },
        "required": ["message", "task_id", "settled"],
        "additionalProperties": False,
    },
    "strict": True,
}


def validate_arguments(arguments: dict) -> None:
    if not isinstance(arguments, dict) or set(arguments) != {"message", "task_id", "settled"}:
        raise ValueError("Invalid pass_on arguments")
    message = arguments["message"]
    if not isinstance(message, str) or not 1 <= len(message.strip()) <= MESSAGE_MAX_CHARS:
        raise ValueError(f"message must contain 1 to {MESSAGE_MAX_CHARS} characters")
    task_id, settled = arguments["task_id"], arguments["settled"]
    if task_id is None:
        if settled is not None:
            raise ValueError("settled is null without a task")
        return
    if not isinstance(task_id, str):
        raise ValueError("task_id must be a task's ID or null")
    UUID(task_id)
    if not isinstance(settled, bool):
        raise ValueError("settled must be true or false for a task")


class PassOnTool:
    def __init__(self, get_connection, is_closing, write_debug, relay):
        self.get_connection = get_connection
        self.is_closing = is_closing
        self.write_debug = write_debug
        # relay(client_id, client_name, text, task_id, settled) -> None or a reason it wasn't sent (such as the daily limit).
        self.relay = relay

    def _log(self, message: str, title: str) -> None:
        try:
            self.write_debug(message, title)
        except Exception:
            pass

    async def result(self, connection, arguments: dict) -> dict:
        # The client's chat carries who they are; nowhere else may pass things on.
        client = getattr(connection, "memory_scope", None)
        if not isinstance(client, tuple):
            return {"status": "error", "message": "This is only for passing things on from a contact's chat."}
        task_id = arguments["task_id"]
        problem = await self.relay(
            *client, arguments["message"].strip(), None if task_id is None else UUID(task_id), arguments["settled"],
        )
        if problem:
            return {"status": "not_done", "message": problem}
        return {"status": "ok", "message": "Passed on. Tell them you've passed it on; you can't promise a reply."}

    async def handle(self, call_id: str, arguments: dict) -> None:
        try:
            if self.is_closing():
                return
            connection = self.get_connection()
            if connection is None:
                return
            validate_arguments(arguments)
            try:
                result = await self.result(connection, arguments)
            except Exception as error:
                self._log(f"Passing on to the person failed ({type(error).__name__})", "Tool error")
                result = {"status": "error", "message": "It couldn't be passed on; don't say it was."}
            if self.is_closing() or self.get_connection() is not connection:
                return
            await connection.response.item.create(item={
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps(result, ensure_ascii=False),
            })
            if self.is_closing() or self.get_connection() is not connection:
                return
            await connection.response.create()
        except Exception as error:
            self._log(f"{type(self).__name__} failed ({type(error).__name__})", "Tool error")
