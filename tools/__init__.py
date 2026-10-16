"""Wire custom tools once for both local execution and API schemas."""

from .close_app import CloseAppTool, SCHEMA, validate_arguments
from .enter_standby import StandbyTool, SCHEMA as STANDBY_SCHEMA, validate_arguments as validate_standby_arguments
from .recall_interest import RecallInterestTool, SCHEMA as RECALL_SCHEMA, validate_arguments as validate_recall_arguments
from .registry import ToolRegistry
from .search_memory import SearchMemoryTool, SCHEMA as SEARCH_SCHEMA, validate_arguments as validate_search_arguments
from .random_number import RandomNumberTool, SCHEMA as RANDOM_NUMBER_SCHEMA, validate_arguments as validate_random_number_arguments


def create_tool_registry(
    *, get_connection, is_closing, shutdown, request_standby, write_debug, schedule,
) -> ToolRegistry:
    registry = ToolRegistry(write_debug, is_closing, schedule)
    close_app = CloseAppTool(get_connection, is_closing, shutdown, write_debug)
    registry.register(SCHEMA, validate_arguments, close_app.handle)
    search = SearchMemoryTool(get_connection, is_closing, write_debug)
    registry.register(SEARCH_SCHEMA, validate_search_arguments, search.handle)
    recall = RecallInterestTool(get_connection, is_closing, write_debug)
    registry.register(RECALL_SCHEMA, validate_recall_arguments, recall.handle)
    standby = StandbyTool(get_connection, is_closing, request_standby, write_debug)
    registry.register(STANDBY_SCHEMA, validate_standby_arguments, standby.handle)
    random_number = RandomNumberTool(get_connection, is_closing, write_debug)
    registry.register(RANDOM_NUMBER_SCHEMA, validate_random_number_arguments, random_number.handle)
    return registry