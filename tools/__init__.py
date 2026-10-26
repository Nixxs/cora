"""Wire custom tools once for both local execution and API schemas."""

from .close_app import CloseAppTool, SCHEMA, validate_arguments
from .enter_standby import StandbyTool, SCHEMA as STANDBY_SCHEMA, validate_arguments as validate_standby_arguments
from .look_at_screen import LookAtScreenTool, SCHEMA as SCREEN_SCHEMA, validate_arguments as validate_screen_arguments
from .recall_interest import RecallInterestTool, SCHEMA as RECALL_SCHEMA, validate_arguments as validate_recall_arguments
from .recall_self import RecallSelfTool, SCHEMA as SELF_SCHEMA, validate_arguments as validate_self_arguments
from .registry import ToolRegistry
from .search_memory import SearchMemoryTool, SCHEMA as SEARCH_SCHEMA, validate_arguments as validate_search_arguments
from .set_person_name import SetPersonNameTool, SCHEMA as NAME_SCHEMA, validate_arguments as validate_name_arguments
from .show_web_image import ShowWebImageTool, SCHEMA as WEB_IMAGE_SCHEMA, validate_arguments as validate_web_image_arguments
from .random_number import RandomNumberTool, SCHEMA as RANDOM_NUMBER_SCHEMA, validate_arguments as validate_random_number_arguments
from .send_signal_message import SendSignalMessageTool, SCHEMA as SIGNAL_SCHEMA, validate_arguments as validate_signal_arguments


async def _not_set_up(*args, **kwargs):
    raise RuntimeError("Signal messaging is not wired up")


def create_tool_registry(
    *, get_connection, is_closing, shutdown, request_standby, write_debug, schedule,
    screen_looking=lambda: True, show_image=lambda image, caption, page_url: None,
    find_signal_contact=lambda name: (None, "Signal isn't set up."), confirm=_not_set_up, send_signal=_not_set_up,
    signal_unreachable=None,
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
    name = SetPersonNameTool(get_connection, is_closing, write_debug)
    registry.register(NAME_SCHEMA, validate_name_arguments, name.handle)
    recall_self = RecallSelfTool(get_connection, is_closing, write_debug)
    registry.register(SELF_SCHEMA, validate_self_arguments, recall_self.handle)
    random_number = RandomNumberTool(get_connection, is_closing, write_debug)
    registry.register(RANDOM_NUMBER_SCHEMA, validate_random_number_arguments, random_number.handle)
    screen = LookAtScreenTool(get_connection, is_closing, screen_looking, write_debug)
    registry.register(SCREEN_SCHEMA, validate_screen_arguments, screen.handle)
    web_image = ShowWebImageTool(get_connection, is_closing, show_image, write_debug)
    registry.register(WEB_IMAGE_SCHEMA, validate_web_image_arguments, web_image.handle)
    signal_message = SendSignalMessageTool(
        get_connection, is_closing, write_debug, find_signal_contact, confirm, send_signal, signal_unreachable,
    )
    registry.register(SIGNAL_SCHEMA, validate_signal_arguments, signal_message.handle)
    return registry