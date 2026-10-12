"""Wire custom tools once for both local execution and API schemas."""

from .close_app import CloseAppTool, SCHEMA, validate_arguments
from .registry import ToolRegistry


def create_tool_registry(*, get_connection, is_closing, shutdown, write_debug, schedule) -> ToolRegistry:
    registry = ToolRegistry(write_debug, is_closing, schedule)
    close_app = CloseAppTool(get_connection, is_closing, shutdown, write_debug)
    registry.register(SCHEMA, validate_arguments, close_app.handle)
    return registry