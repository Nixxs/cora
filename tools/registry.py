"""Custom-tool registration and dispatch, independent of Textual."""

from collections.abc import Callable, Coroutine
from contextvars import copy_context
from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Any

from .route import current_route


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

    def unregister(self, name: str) -> None:
        """Remove a tool, such as a service's tool switched off; the next connect leaves it out."""
        self._tools.pop(name, None)

    def names(self) -> set[str]:
        return set(self._tools)

    def schemas(self, excluding: frozenset[str] = frozenset()) -> list[dict]:
        """Export only registered tools; callers cannot mutate the registry."""
        return [deepcopy(tool.schema) for name, tool in self._tools.items() if name not in excluding]

    def handle_function_call(self, item: dict, route=None) -> bool:
        """Schedule a call; its result goes back to `route`, or to Live without one.

        Returns whether it was scheduled: a refused call gets no reply from its tool.
        """
        if self.is_closing():
            return False
        name = item.get("name")
        if not isinstance(name, str) or name not in self._tools or (
            route is not None and name in route.excluded
        ):
            self.write_debug(f"Unknown tool: {name}", "Tool error")
            return False
        call_id = item.get("call_id")
        if (
            not isinstance(call_id, str)
            or not call_id.strip()
            or item.get("status") not in (None, "completed")
        ):
            self.write_debug(f"Incomplete {name} call rejected", "Tool error")
            return False
        if call_id in self.handled_call_ids:
            return False
        tool = self._tools[name]
        try:
            arguments = json.loads(item.get("arguments", ""))
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be a JSON object")
            tool.validate(arguments)
        except (TypeError, ValueError):
            self.write_debug(f"Invalid arguments for {name}", "Tool error")
            return False

        # Reserve before scheduling so repeated events cannot execute twice.
        self.handled_call_ids.add(call_id)
        if route is None:
            self.schedule(self.run_tool(name, tool, call_id, arguments))
        else:
            # The tool's task starts in this context, so its connection is the route.
            context = copy_context()
            context.run(current_route.set, route)
            context.run(self.schedule, self.run_tool(name, tool, call_id, arguments))
        return True

    async def run_tool(self, name: str, tool: Tool, call_id: str, arguments: dict) -> None:
        try:
            # Each handler owns its result/continuation; closing is terminal.
            await tool.handle(call_id, arguments)
        except Exception as error:
            self.write_debug(f"{name} failed: {error}", "Tool error")