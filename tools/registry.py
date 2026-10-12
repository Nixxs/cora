"""Custom-tool registration and dispatch, independent of Textual."""

from collections.abc import Callable, Coroutine
from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Any


@dataclass(frozen=True)
class Tool:
    schema: dict
    validate: Callable[[dict], None]
    handle: Callable[[str, dict], Coroutine[Any, Any, None]]


class ToolRegistry:
    def __init__(self, write_debug, is_closing, schedule):
        self.write_debug = write_debug
        self.is_closing = is_closing
        self.schedule = schedule
        self._tools: dict[str, Tool] = {}
        self.handled_call_ids: set[str] = set()

    def register(self, schema: dict, validate, handler) -> None:
        name = schema.get("name")
        if schema.get("type") != "function" or not isinstance(name, str) or not name.strip():
            raise ValueError("A custom tool needs a function schema with a name")
        if name in self._tools:
            raise ValueError(f"Tool already registered: {name}")
        self._tools[name] = Tool(deepcopy(schema), validate, handler)

    def schemas(self) -> list[dict]:
        """Export only registered tools; callers cannot mutate the registry."""
        return [deepcopy(tool.schema) for tool in self._tools.values()]

    def handle_function_call(self, item: dict) -> None:
        if self.is_closing():
            return
        name = item.get("name")
        if not isinstance(name, str) or name not in self._tools:
            self.write_debug(f"Unknown tool: {name}", "Tool error")
            return
        call_id = item.get("call_id")
        if (
            not isinstance(call_id, str)
            or not call_id.strip()
            or item.get("status") not in (None, "completed")
        ):
            self.write_debug(f"Incomplete {name} call rejected", "Tool error")
            return
        if call_id in self.handled_call_ids:
            return
        tool = self._tools[name]
        try:
            arguments = json.loads(item.get("arguments", ""))
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be a JSON object")
            tool.validate(arguments)
        except (TypeError, ValueError):
            self.write_debug(f"Invalid arguments for {name}", "Tool error")
            return

        # Reserve before scheduling so repeated events cannot execute twice.
        self.handled_call_ids.add(call_id)
        self.schedule(self.run_tool(name, tool, call_id, arguments))

    async def run_tool(self, name: str, tool: Tool, call_id: str, arguments: dict) -> None:
        try:
            # Each handler owns its result/continuation; closing is terminal.
            await tool.handle(call_id, arguments)
        except Exception as error:
            self.write_debug(f"{name} failed: {error}", "Tool error")