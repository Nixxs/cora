"""Conversations with Cora's backend without a Live session: written chat now, Signal later.

Each turn sends the backend instructions, the startup sections, recent history from Cora's
own records and the new message, with store=False, as her background calls do. Tool calls
run through the same registry as in Live; their results come back through a route.
"""

import asyncio
from types import SimpleNamespace

from openai import AsyncOpenAI

from session_config import BACKEND_INSTRUCTIONS, TEXT_CHAT_INSTRUCTIONS
from tools.pass_on import CLIENT_ONLY

# Tool rounds one turn may take; each round is one backend request. Browsing spends one per
# click or page, so this leaves room for a real lookup. Past it, she answers without tools.
MAX_ROUNDS = 20
OUT_OF_ROUNDS = (
    "You have used all the tool calls you can make for this message. Answer now from what you "
    "have, and say plainly what you couldn't finish or check."
)
# How long one tool result may take; long enough for an approval pop-up to be answered.
TOOL_RESULT_SECONDS = 180

# Typing in Standby: she is already resting, and closing the app from a written chat is not offered.
STANDBY_EXCLUDED = frozenset({"close_app", "enter_standby", *CLIENT_ONLY})
# Signal, away from the desk: nothing that needs the screen either, and not where they are (that's
# for the web page, as are maps). Tools that change things are offered: the person approves them in the chat.
SIGNAL_EXCLUDED = STANDBY_EXCLUDED | {"look_at_screen", "show_web_image", "where_is_person", "show_map"}
# The web page, often on a phone: like Signal, except that the page shows her pictures.
WEB_EXCLUDED = STANDBY_EXCLUDED | {"look_at_screen"}

TOOL_REFUSED ='{"status": "error", "message": "That tool is not available here."}'
TOOL_NO_RESULT = '{"status": "error", "message": "The tool did not answer in time; do not guess its result."}'


class BackendRoute:
    """Looks like a Live connection to the tools: their results are kept for the next request."""

    def __init__(
        self, excluded: frozenset[str] = frozenset(), memory_scope: tuple | None = None,
        ask_approval=None, approval_seconds: float = 0, tool_seconds: float | None = None,
        email_party: tuple | None = None, asked_where: str = "on Signal",
    ) -> None:
        # Tools not offered in this kind of conversation, and refused if called anyway.
        self.excluded = frozenset(excluded)
        # In a client's chat, (their id, their name): memory search only finds what they may know.
        self.memory_scope = memory_scope
        # Answering an email thread for a task, (party id, task id, who they are): pass_on reports from them.
        self.email_party = email_party
        # In the person's own chat (Signal, the web page): asks them to approve there (returns their
        # answer), instead of the pop-up on the desk, waiting at most approval_seconds. asked_where
        # says where, for Activity.
        self.ask_approval = ask_approval
        self.approval_seconds = approval_seconds
        self.asked_where = asked_where
        # How long one tool result may take here, if longer than usual (an approval by phone).
        self.tool_seconds = tool_seconds
        self.outputs: dict[str, str] = {}
        self.arrived = asyncio.Event()
        self.response = SimpleNamespace(item=SimpleNamespace(create=self._create_item), create=self._create)

    async def _create_item(self, item: dict, **_) -> None:
        if item.get("type") == "function_call_output":
            self.outputs[item["call_id"]] = item["output"]
            self.arrived.set()

    async def _create(self, **_) -> None:
        # Live needs telling to continue; here the turn continues once every result is in.
        pass

    async def wait_for(self, call_id: str, timeout: float) -> str:
        deadline = asyncio.get_running_loop().time() + timeout
        while call_id not in self.outputs:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return TOOL_NO_RESULT
            self.arrived.clear()
            try:
                await asyncio.wait_for(self.arrived.wait(), remaining)
            except TimeoutError:
                return TOOL_NO_RESULT
        return self.outputs[call_id]


def output_text(item) -> str:
    """The written text of one assistant message from the backend."""
    parts = []
    for content in getattr(item, "content", None) or []:
        if content.type == "output_text":
            parts.append(content.text)
        elif content.type == "refusal":
            parts.append(content.refusal)
    return "\n\n".join(parts)


# Streamed events that carry written text, as the non-streamed output_text reads it.
TEXT_DELTAS = ("response.output_text.delta", "response.refusal.delta")


async def stream_round(client, request: dict, on_text):
    """One backend request, streamed: on_text gets the round's text so far; returns the full response.

    The final reply is still read from the returned response, so what is shown at the end is
    exactly what a non-streamed request would have given.
    """
    # Each message part's text so far, in the order they began.
    parts: dict[tuple, str] = {}
    async with await client.responses.create(**request, stream=True) as stream:
        async for event in stream:
            if event.type in TEXT_DELTAS:
                key = (event.item_id, event.content_index)
                parts[key] = parts.get(key, "") + event.delta
                on_text("\n\n".join(text for part in parts.values() if (text := part.strip())))
            elif event.type in ("response.completed", "response.incomplete"):
                return event.response
            elif event.type in ("response.failed", "error"):
                raise RuntimeError(f"Streamed response ended with {event.type}")
    raise RuntimeError("Stream ended without a response")


class BackendChat:
    """Runs one turn of a chat without Live, tools included, and returns Cora's written reply."""

    def __init__(self, tools, write_activity, client_factory=AsyncOpenAI,
                 tool_seconds: float = TOOL_RESULT_SECONDS) -> None:
        self.tools = tools
        self.write_activity = write_activity
        self.client_factory = client_factory
        self.tool_seconds = tool_seconds

    async def reply(
        self, *, content: list[dict], history: list[dict], sections: dict[str, str], model: str,
        route: BackendRoute, extra_instructions: str = "", on_text=None,
    ) -> str:
        """on_text, if given, gets each round's written text so far as it streams in."""
        instructions = BACKEND_INSTRUCTIONS + "".join(sections.values()) + TEXT_CHAT_INSTRUCTIONS + extra_instructions
        tools = [{"type": "web_search"}, *self.tools.schemas(excluding=route.excluded)]
        items = [*history, {"type": "message", "role": "user", "content": content}]
        async with self.client_factory() as client:
            for round_number in range(MAX_ROUNDS + 1):
                last = round_number == MAX_ROUNDS
                if last:
                    # Out of rounds: one more request with tools off, so what she found isn't lost.
                    self.write_activity(f"Cora used all {MAX_ROUNDS} tool rounds; answering with what she has.",
                                        "Tool rounds")
                    items.append({"type": "message", "role": "developer", "content": OUT_OF_ROUNDS})
                request = dict(
                    model=model, instructions=instructions, input=items, tools=tools,
                    tool_choice="none" if last else "auto", parallel_tool_calls=False, store=False,
                    # Without storage, reasoning is handed back encrypted so tool rounds keep it.
                    include=["reasoning.encrypted_content"],
                )
                if on_text is None:
                    response = await client.responses.create(**request)
                else:
                    response = await stream_round(client, request, on_text)
                calls = [item for item in response.output if item.type == "function_call"]
                if last and calls:
                    break  # tools were off; it still asked for one
                if not calls:
                    return "\n\n".join(
                        text for item in response.output
                        if item.type == "message" and (text := output_text(item).strip())
                    )
                items += [item.model_dump(exclude_none=True) for item in response.output]
                for call in calls:
                    items.append({
                        "type": "function_call_output", "call_id": call.call_id,
                        "output": await self.run_tool(call, route),
                    })
        raise RuntimeError(f"No answer after {MAX_ROUNDS} tool rounds")

    async def run_tool(self, call, route: BackendRoute) -> str:
        item = {"type": "function_call", "name": call.name, "call_id": call.call_id,
                "arguments": call.arguments, "status": "completed"}
        if not self.tools.handle_function_call(item, route=route):
            return TOOL_REFUSED
        return await route.wait_for(call.call_id, route.tool_seconds or self.tool_seconds)
