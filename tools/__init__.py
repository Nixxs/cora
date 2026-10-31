"""Wire custom tools once for both local execution and API schemas."""

from .client_boundaries import (
    RAISE_CONCERN_SCHEMA, STOP_REPLYING_SCHEMA, RaiseConcernTool, StopReplyingTool, validate_concern_arguments,
    validate_stop_arguments,
)
from .check_email import CheckEmailTool, SCHEMA as CHECK_EMAIL_SCHEMA, validate_arguments as validate_check_email_arguments
from .change_calendar import (
    CANCEL_SCHEMA as CANCEL_EVENT_SCHEMA, CREATE_SCHEMA as CREATE_EVENT_SCHEMA, UPDATE_SCHEMA as UPDATE_EVENT_SCHEMA,
    CancelEventTool, CreateEventTool, UpdateEventTool, validate_cancel_arguments, validate_create_arguments,
    validate_update_arguments,
)
from .check_calendar import (
    CheckCalendarTool, SCHEMA as CHECK_CALENDAR_SCHEMA, validate_arguments as validate_check_calendar_arguments,
)
from .close_app import CloseAppTool, SCHEMA, validate_arguments
from .close_task import CloseTaskTool, SCHEMA as CLOSE_TASK_SCHEMA, validate_arguments as validate_close_task_arguments
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
from .send_email import SendEmailTool, SCHEMA as SEND_EMAIL_SCHEMA, validate_arguments as validate_send_email_arguments
from .pass_on import PassOnTool, SCHEMA as PASS_ON_SCHEMA, validate_arguments as validate_pass_on_arguments


async def _not_set_up(*args, **kwargs):
    raise RuntimeError("Signal messaging is not wired up")


def create_tool_registry(
    *, get_connection, is_closing, shutdown, request_standby, write_debug, schedule,
    screen_looking=lambda: True, show_image=lambda image, caption, page_url: None,
    find_signal_contact=lambda name: (None, "Signal isn't set up."), confirm=_not_set_up, send_signal=_not_set_up,
    signal_unreachable=None, relay_to_person=_not_set_up, raise_concern=_not_set_up, stop_replying=_not_set_up,
    task_goal=None, record_task=None, close_task=_not_set_up, get_mailbox=lambda: None, record_email_task=None,
    relay_from_email=None, find_email_thread=None, email_signature=lambda: "", get_calendar=lambda: None,
    calendar_changed=lambda: None,
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
        task_goal, record_task,
    )
    registry.register(SIGNAL_SCHEMA, validate_signal_arguments, signal_message.handle)
    pass_on = PassOnTool(get_connection, is_closing, write_debug, relay_to_person, relay_from_email)
    registry.register(PASS_ON_SCHEMA, validate_pass_on_arguments, pass_on.handle)
    concern = RaiseConcernTool(get_connection, is_closing, write_debug, raise_concern)
    registry.register(RAISE_CONCERN_SCHEMA, validate_concern_arguments, concern.handle)
    stop = StopReplyingTool(get_connection, is_closing, write_debug, stop_replying)
    registry.register(STOP_REPLYING_SCHEMA, validate_stop_arguments, stop.handle)
    close_task_tool = CloseTaskTool(get_connection, is_closing, write_debug, close_task)
    registry.register(CLOSE_TASK_SCHEMA, validate_close_task_arguments, close_task_tool.handle)
    check_email = CheckEmailTool(get_connection, is_closing, write_debug, get_mailbox)
    registry.register(CHECK_EMAIL_SCHEMA, validate_check_email_arguments, check_email.handle)
    send_email = SendEmailTool(
        get_connection, is_closing, write_debug, get_mailbox, confirm, task_goal, record_email_task, find_email_thread,
        email_signature,
    )
    registry.register(SEND_EMAIL_SCHEMA, validate_send_email_arguments, send_email.handle)
    check_calendar = CheckCalendarTool(get_connection, is_closing, write_debug, get_calendar)
    registry.register(CHECK_CALENDAR_SCHEMA, validate_check_calendar_arguments, check_calendar.handle)
    for schema, validate, tool_type in (
        (CREATE_EVENT_SCHEMA, validate_create_arguments, CreateEventTool),
        (UPDATE_EVENT_SCHEMA, validate_update_arguments, UpdateEventTool),
        (CANCEL_EVENT_SCHEMA, validate_cancel_arguments, CancelEventTool),
    ):
        tool = tool_type(get_connection, is_closing, write_debug, get_calendar, confirm, calendar_changed)
        registry.register(schema, validate, tool.handle)
    return registry