"""In a client's Signal chat: pass something on to the person she works with, such as a plan.

It only ever reaches the person, marked as coming from that client, so it needs no approval;
a daily limit per client keeps a chatty or hostile one from flooding them. Only offered in
clients' chats: the route says which client is asking.
"""

import json

MESSAGE_MAX_CHARS = 3000
# Not offered at the desk, in Live or in the person's own chat: only clients have someone to pass things to.
CLIENT_ONLY = frozenset({"tell_nick"})

SCHEMA = {
    "type": "function",
    "name": "tell_nick",
    "description": (
        "Pass something on to the person you work with, as a Signal message from you marked as "
        "coming from this contact. Use it when they ask you to pass something on (a plan you "
        "worked out together, a question, a message), or when something clearly needs the "
        "person's attention. Write it clearly and completely; it reaches only the person you "
        "work with. Tell this contact you've passed it on; never promise a reply for them."
    ),
    "parameters": {
        "type": "object",
        "properties": {"message": {"type": "string", "minLength": 1, "maxLength": MESSAGE_MAX_CHARS}},
        "required": ["message"],
        "additionalProperties": False,
    },
    "strict": True,
}


def validate_arguments(arguments: dict) -> None:
    if not isinstance(arguments, dict) or set(arguments) != {"message"}:
        raise ValueError("Invalid tell_nick arguments")
    message = arguments["message"]
    if not isinstance(message, str) or not 1 <= len(message.strip()) <= MESSAGE_MAX_CHARS:
        raise ValueError(f"message must contain 1 to {MESSAGE_MAX_CHARS} characters")


class TellNickTool:
    def __init__(self, get_connection, is_closing, write_debug, relay):
        self.get_connection = get_connection
        self.is_closing = is_closing
        self.write_debug = write_debug
        # relay(client_id, client_name, text) -> None or a reason it wasn't sent (such as the daily limit).
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
        problem = await self.relay(*client, arguments["message"].strip())
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
            self._log(f"tell_nick failed ({type(error).__name__})", "Tool error")
