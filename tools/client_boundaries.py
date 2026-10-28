"""In a client's Signal chat: telling the person at once about a serious concern, and ending a chat.

Like tell_nick, only offered in clients' chats, where the route says which client it is. A
concern is her own heads-up, not a relay of their words; a daily limit per client keeps a
hostile one from flooding the person with them.
"""

from .tell_nick import TellNickTool

CONCERN_MAX_CHARS = 500

RAISE_CONCERN_SCHEMA = {
    "type": "function",
    "name": "raise_concern",
    "description": (
        "Tell the person you work with straight away, in your own words, about something serious "
        "in this chat: threats of harm to anyone, or sustained abuse or harassment. Not for "
        "ordinary rudeness or questions you turn away; those wait for your usual heads-up after "
        "the chat. Say what happened in general terms, without quoting them. It reaches only the "
        "person you work with, as from you."
    ),
    "parameters": {
        "type": "object",
        "properties": {"concern": {"type": "string", "minLength": 1, "maxLength": CONCERN_MAX_CHARS}},
        "required": ["concern"],
        "additionalProperties": False,
    },
    "strict": True,
}

STOP_REPLYING_SCHEMA = {
    "type": "function",
    "name": "stop_replying",
    "description": (
        "End this chat when someone keeps being rude, abusive or threatening after you've asked "
        "them to stop: you won't see or answer their further messages until they've been quiet "
        "for a while and a new chat starts. Your reply to this message is your last, so make it "
        "one short, calm line setting the boundary, without arguing."
    ),
    "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    "strict": True,
}


def validate_concern_arguments(arguments: dict) -> None:
    if not isinstance(arguments, dict) or set(arguments) != {"concern"}:
        raise ValueError("Invalid raise_concern arguments")
    concern = arguments["concern"]
    if not isinstance(concern, str) or not 1 <= len(concern.strip()) <= CONCERN_MAX_CHARS:
        raise ValueError(f"concern must contain 1 to {CONCERN_MAX_CHARS} characters")


def validate_stop_arguments(arguments: dict) -> None:
    if arguments not in ({}, None):
        raise ValueError("stop_replying takes no arguments")


class RaiseConcernTool(TellNickTool):
    """relay(client_id, client_name, concern) -> None once on its way, else why not."""

    async def result(self, connection, arguments: dict) -> dict:
        client = getattr(connection, "memory_scope", None)
        if not isinstance(client, tuple):
            return {"status": "error", "message": "This is only for a concern in a contact's chat."}
        problem = await self.relay(*client, arguments["concern"].strip())
        if problem:
            return {"status": "not_done", "message": problem}
        return {"status": "ok", "message": "The person will hear about it. You needn't tell this contact."}


class StopReplyingTool(TellNickTool):
    """relay(client_id, client_name) -> None once she has stopped, else why not."""

    async def result(self, connection, arguments: dict) -> dict:
        client = getattr(connection, "memory_scope", None)
        if not isinstance(client, tuple):
            return {"status": "error", "message": "This is only for ending a contact's chat."}
        problem = await self.relay(*client)
        if problem:
            return {"status": "not_done", "message": problem}
        return {"status": "ok", "message": "Done. Now send your one short, calm closing line."}
