"""Wire custom tools once for both local execution and API schemas."""

from .close_app import CloseAppTool, SCHEMA, validate_arguments
from .registry import ToolRegistry
from .search_memory import SearchMemoryTool, SCHEMA as SEARCH_SCHEMA, validate_arguments as validate_search_arguments


def create_tool_registry(*, get_connection, is_closing, shutdown, write_debug, schedule) -> ToolRegistry:
    registry = ToolRegistry(write_debug, is_closing, schedule)
    close_app = CloseAppTool(get_connection, is_closing, shutdown, write_debug)
    registry.register(SCHEMA, validate_arguments, close_app.handle)
    search = SearchMemoryTool(get_connection, is_closing, write_debug)
    registry.register(SEARCH_SCHEMA, validate_search_arguments, search.handle)
    return registry