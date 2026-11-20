"""Let Cora switch which World of Warcraft chat channel push-to-talk speaks into (/say, /party,
/guild or /yell), as Settings > Game does, when the person asks ("put me on party chat").

It changes and saves the setting only; nothing is typed into the game. Desk only, like the map pins
(backend_chat.GAME_TOOLS): the person is at the game when they ask.
"""

import json

from game.chat_line import CHANNELS

SCHEMA = {
    "type": "function",
    "name": "set_wow_chat_channel",
    "description": (
        "Switch which World of Warcraft chat channel the person's push-to-talk speaks into, when they "
        "ask (such as 'put me on party chat' or 'switch to guild'): say (/s, players nearby), party "
        "(/p), guild (/g) or yell (/y, across the area). It's saved, like Settings > Game, and stays "
        "until changed. Nothing is typed into the game."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "channel": {"type": "string", "enum": list(CHANNELS), "description": "The channel to speak into."},
        },
        "required": ["channel"],
        "additionalProperties": False,
    },
    "strict": True,
}

ERROR_RESULT = {"status": "error", "message": "The chat channel couldn't be changed; say so."}


def validate_arguments(arguments: dict) -> None:
    if not isinstance(arguments, dict) or set(arguments) != {"channel"}:
        raise ValueError("set_wow_chat_channel requires exactly 'channel'")
    if arguments["channel"] not in CHANNELS:
        raise ValueError(f"channel must be one of {', '.join(CHANNELS)}")


class SetWowChatChannelTool:
    def __init__(self, get_connection, is_closing, set_channel, write_debug):
        # set_channel(channel) -> a result dict for her.
        self.get_connection = get_connection
        self.is_closing = is_closing
        self.set_channel = set_channel
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
                result = await self.set_channel(arguments["channel"])
            except Exception as error:
                self.write_debug(f"Changing the WoW chat channel failed ({type(error).__name__})", "Tool error")
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
            self.write_debug(f"set_wow_chat_channel failed ({type(error).__name__})", "Tool error")
