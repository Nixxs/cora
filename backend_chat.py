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

# Tool calls one turn may make before giving up; each round is one backend request.
MAX_ROUNDS = 8
# How long one tool result may take; long enough for an approval pop-up to be answered.
TOOL_RESULT_SECONDS = 180

# Typing in Standby: she is already resting, and closing the app from a written chat is not offered.
STANDBY_EXCLUDED = frozenset({"close_app", "enter_standby", *CLIENT_ONLY})
# Signal, away from the desk: nothing that needs the screen either. Tools that change things are
# offered: the person approves them in the chat.
SIGNAL_EXCLUDED = STANDBY_EXCLUDED | {"look_at_screen", "show_web_image"}

TOOL_REFUSED ='{"status": "error", "message": "That tool is not available here."}'
TOOL_NO_RESULT = '{"status": "error", "message": "The tool did not answer in time; do not guess its result."}'


class BackendRoute:
    """Looks like a Live connection to the tools: their results are kept for the next request."""

    def __init__(
        self, excluded: frozenset[str] = frozenset(), memory_scope: tuple | None = None,
        ask_approval=None, approval_seconds: float = 0, tool_seconds: float | None = None,
    ) -> None:
        # Tools not offered in this kind of conversation, and refused if called anyway.
        self.excluded = frozenset(excluded)
        # In a client's chat, (their id, their name): memory search only finds what they may know.
        self.memory_scope = memory_scope
        # In the person's own Signal chat: asks them to approve in the chat (returns their answer),
        # instead of the pop-up on the desk, waiting at most approval_seconds.
        self.ask_approval = ask_approval
        self.approval_seconds = approval_seconds
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
        route: BackendRoute, extra_instructions: str = "",
    ) -> str:
        instructions = BACKEND_INSTRUCTIONS + "".join(sections.values()) + TEXT_CHAT_INSTRUCTIONS + extra_instructions
        tools = [{"type": "web_search"}, *self.tools.schemas(excluding=route.excluded)]
        items = [*history, {"type": "message", "role": "user", "content": content}]
        async with self.client_factory() as client:
            for _ in range(MAX_ROUNDS):
                response = await client.responses.create(
                    model=model, instructions=instructions, input=items, tools=tools,
                    tool_choice="auto", parallel_tool_calls=False, store=False,
                    # Without storage, reasoning is handed back encrypted so tool rounds keep it.
                    include=["reasoning.encrypted_content"],
                )
                calls = [item for item in response.output if item.type == "function_call"]
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
