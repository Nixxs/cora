"""Wire custom tools once for both local execution and API schemas."""

from .client_boundaries import (
    RAISE_CONCERN_SCHEMA, STOP_REPLYING_SCHEMA, RaiseConcernTool, StopReplyingTool, validate_concern_arguments,
    validate_stop_arguments,
)
from .check_email import CheckEmailTool, SCHEMA as CHECK_EMAIL_SCHEMA, validate_arguments as validate_check_email_arguments
from .change_calendar import (
    CANCEL_SCHEMA as CANCEL_EVENT_SCHEMA, CREATE_SCHEMA as CREATE_EVENT_SCHEMA, FORWARD_SCHEMA as FORWARD_EVENT_SCHEMA,
    UPDATE_SCHEMA as UPDATE_EVENT_SCHEMA, CancelEventTool, CreateEventTool, ForwardEventTool, UpdateEventTool,
    validate_cancel_arguments, validate_create_arguments, validate_forward_arguments, validate_update_arguments,
)
from .check_calendar import (
    CheckCalendarTool, SCHEMA as CHECK_CALENDAR_SCHEMA, validate_arguments as validate_check_calendar_arguments,
)
from .claude_code import ClaudeCodeTool, SCHEMA as CLAUDE_CODE_SCHEMA, validate_arguments as validate_claude_code_arguments
from .close_app import CloseAppTool, SCHEMA, validate_arguments
from .consult_rocky import (
    ConsultRockyTool, SCHEMA as CONSULT_ROCKY_SCHEMA, validate_arguments as validate_consult_rocky_arguments,
)
from .run_claude_code import (
    RunClaudeCodeTool, SCHEMA as RUN_CLAUDE_CODE_SCHEMA, validate_arguments as validate_run_claude_code_arguments,
)
from .close_task import CloseTaskTool, SCHEMA as CLOSE_TASK_SCHEMA, validate_arguments as validate_close_task_arguments
from .read_call import ReadCallTool, SCHEMA as READ_CALL_SCHEMA, validate_arguments as validate_read_call_arguments
from .enter_standby import StandbyTool, SCHEMA as STANDBY_SCHEMA, validate_arguments as validate_standby_arguments
from .look_at_screen import LookAtScreenTool, SCHEMA as SCREEN_SCHEMA, validate_arguments as validate_screen_arguments
from .look_again import KeptImages, LookAgainTool, SCHEMA as LOOK_AGAIN_SCHEMA, validate_arguments as validate_look_again_arguments
from .weigh_odds import SCHEMA as WEIGH_ODDS_SCHEMA, WeighOddsTool, validate_arguments as validate_weigh_odds_arguments
from .aircon import SCHEMA as AIRCON_SCHEMA, AirconTool, validate_arguments as validate_aircon_arguments
from .set_wow_chat_channel import (
    SCHEMA as WOW_CHANNEL_SCHEMA, SetWowChatChannelTool, validate_arguments as validate_wow_channel_arguments,
)
from .recall_interest import RecallInterestTool, SCHEMA as RECALL_SCHEMA, validate_arguments as validate_recall_arguments
from .recall_self import RecallSelfTool, SCHEMA as SELF_SCHEMA, validate_arguments as validate_self_arguments
from .recall_desire import RecallDesireTool, SCHEMA as DESIRE_SCHEMA, validate_arguments as validate_desire_arguments
from .feel import FeelTool, SCHEMA as FEEL_SCHEMA, validate_arguments as validate_feel_arguments
from .note_desire import NoteDesireTool, SCHEMA as NOTE_DESIRE_SCHEMA, validate_arguments as validate_note_desire_arguments
from .note_detail import NoteDetailTool, SCHEMA as NOTE_DETAIL_SCHEMA, validate_arguments as validate_note_detail_arguments
from .registry import ToolRegistry
from .search_memory import SearchMemoryTool, SCHEMA as SEARCH_SCHEMA, validate_arguments as validate_search_arguments
from .set_person_name import SetPersonNameTool, SCHEMA as NAME_SCHEMA, validate_arguments as validate_name_arguments
from .show_web_image import ShowWebImageTool, SCHEMA as WEB_IMAGE_SCHEMA, validate_arguments as validate_web_image_arguments
from .random_number import RandomNumberTool, SCHEMA as RANDOM_NUMBER_SCHEMA, validate_arguments as validate_random_number_arguments
from .send_signal_message import SendSignalMessageTool, SCHEMA as SIGNAL_SCHEMA, validate_arguments as validate_signal_arguments
from .message_person_on_signal import (
    MessagePersonOnSignalTool, SCHEMA as MESSAGE_PERSON_SCHEMA, validate_arguments as validate_message_person_arguments,
)
from .end_call import SCHEMA as END_CALL_SCHEMA, EndCallTool, validate_arguments as validate_end_call_arguments
from .press_keys import SCHEMA as PRESS_KEYS_SCHEMA, PressKeysTool, validate_arguments as validate_press_keys_arguments
from .call_someone import SCHEMA as CALL_SOMEONE_SCHEMA, CallSomeoneTool, validate_arguments as validate_call_arguments
from .send_email import SendEmailTool, SCHEMA as SEND_EMAIL_SCHEMA, validate_arguments as validate_send_email_arguments
from .pass_on import PassOnTool, SCHEMA as PASS_ON_SCHEMA, validate_arguments as validate_pass_on_arguments
from .place_wow_map_pins import (
    PlaceWowMapPinsTool, SCHEMA as WOW_PINS_SCHEMA, validate_arguments as validate_wow_pins_arguments,
)
from .wow_map_pins_command import (
    SCHEMA as WOW_COMMAND_SCHEMA, WowMapPinsCommandTool, validate_arguments as validate_wow_command_arguments,
)
from .show_map import ShowMapTool, SCHEMA as MAP_SCHEMA, validate_arguments as validate_map_arguments
from .where_is_person import WhereIsPersonTool, SCHEMA as WHERE_SCHEMA, validate_arguments as validate_where_arguments
from .request_sign_up import (
    RequestSignUpTool, SCHEMA as SIGN_UP_SCHEMA, validate_arguments as validate_sign_up_arguments,
)


async def _not_set_up(*args, **kwargs):
    raise RuntimeError("Signal messaging is not wired up")


async def _nowhere(refresh):
    return {"status": "error", "message": "Checking where the person is isn't available here."}


async def _no_claude_code(project, recent_exchanges):
    return {"status": "error", "message": "Claude Code's sessions aren't available here."}


async def _no_claude_code_runs(action, project, create, location, brief):
    return {"status": "error", "message": "Running Claude Code isn't available here."}


async def _no_rocky(question, new_topic):
    return {"status": "error", "message": "Rocky isn't available here."}


async def _no_game_pins(route, lines):
    return {"status": "error", "message": "Placing map pins in the game isn't available here."}


async def _no_game_commands(line):
    return {"status": "error", "message": "Running map pin commands in the game isn't available here."}


async def _no_game_channel(channel):
    return {"status": "error", "message": "Changing the game's chat channel isn't available here."}


async def _no_aircon(room):
    return {"status": "error", "message": "The air conditioners aren't available here."}


async def _no_sign_ups(url):
    return None, "Signing up isn't available here."


def create_tool_registry(
    *, get_connection, is_closing, shutdown, request_standby, write_debug, schedule,
    screen_looking=lambda: True, show_image=lambda image, caption, page_url: None,
    find_signal_contact=lambda name: (None, "Signal isn't set up."), confirm=_not_set_up, send_signal=_not_set_up,
    signal_unreachable=None, relay_to_person=_not_set_up, raise_concern=_not_set_up, stop_replying=_not_set_up,
    task_goal=None, record_task=None, close_task=_not_set_up, read_call=_not_set_up, get_mailbox=lambda: None, record_email_task=None,
    relay_from_email=None, find_email_thread=None, email_signature=lambda: "", get_calendar=lambda: None,
    calendar_changed=lambda: None, check_sign_up=_no_sign_ups, begin_sign_up=_not_set_up,
    signal_reaches_person=lambda: False, message_person=_not_set_up, note_feeling=_not_set_up,
    kept_images: KeptImages | None = None, locate_person=_nowhere, show_map=lambda view: False,
    can_call=lambda number: "Phone calls aren't set up.", place_call=_not_set_up,
    look_at_claude_code=_no_claude_code, run_claude_code=_no_claude_code_runs,
    place_map_pins=_no_game_pins, run_map_pins_command=_no_game_commands, consult_rocky=_no_rocky,
    set_wow_chat_channel=_no_game_channel, read_aircon=_no_aircon,
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
    recall_desire = RecallDesireTool(get_connection, is_closing, write_debug)
    registry.register(DESIRE_SCHEMA, validate_desire_arguments, recall_desire.handle)
    note_desire = NoteDesireTool(get_connection, is_closing, write_debug)
    registry.register(NOTE_DESIRE_SCHEMA, validate_note_desire_arguments, note_desire.handle)
    feel = FeelTool(get_connection, is_closing, write_debug, note_feeling)
    registry.register(FEEL_SCHEMA, validate_feel_arguments, feel.handle)
    random_number = RandomNumberTool(get_connection, is_closing, write_debug)
    registry.register(RANDOM_NUMBER_SCHEMA, validate_random_number_arguments, random_number.handle)
    screen = LookAtScreenTool(get_connection, is_closing, screen_looking, write_debug)
    registry.register(SCREEN_SCHEMA, validate_screen_arguments, screen.handle)
    # The person's photos from this conversation (web page, desk paste, their own Signal).
    if kept_images is None:
        kept_images = KeptImages()
    look_again = LookAgainTool(get_connection, is_closing, kept_images, write_debug)
    registry.register(LOOK_AGAIN_SCHEMA, validate_look_again_arguments, look_again.handle)
    # Real probabilities for a judgement call (the Decisions API), with those photos if they help.
    weigh_odds = WeighOddsTool(get_connection, is_closing, kept_images, write_debug)
    registry.register(WEIGH_ODDS_SCHEMA, validate_weigh_odds_arguments, weigh_odds.handle)
    # Where the person is, from their open web page, when it matters to what they asked.
    where = WhereIsPersonTool(get_connection, is_closing, locate_person, write_debug)
    registry.register(WHERE_SCHEMA, validate_where_arguments, where.handle)
    # A map on the web page (only offered with a Google Maps key).
    map_tool = ShowMapTool(get_connection, is_closing, show_map, write_debug)
    registry.register(MAP_SCHEMA, validate_map_arguments, map_tool.handle)
    web_image = ShowWebImageTool(get_connection, is_closing, show_image, write_debug)
    registry.register(WEB_IMAGE_SCHEMA, validate_web_image_arguments, web_image.handle)
    signal_message = SendSignalMessageTool(
        get_connection, is_closing, write_debug, find_signal_contact, confirm, send_signal, signal_unreachable,
        task_goal, record_task,
    )
    registry.register(SIGNAL_SCHEMA, validate_signal_arguments, signal_message.handle)
    message_person = MessagePersonOnSignalTool(get_connection, is_closing, write_debug, signal_reaches_person,
                                               message_person)
    registry.register(MESSAGE_PERSON_SCHEMA, validate_message_person_arguments, message_person.handle)
    pass_on = PassOnTool(get_connection, is_closing, write_debug, relay_to_person, relay_from_email)
    registry.register(PASS_ON_SCHEMA, validate_pass_on_arguments, pass_on.handle)
    concern = RaiseConcernTool(get_connection, is_closing, write_debug, raise_concern)
    registry.register(RAISE_CONCERN_SCHEMA, validate_concern_arguments, concern.handle)
    stop = StopReplyingTool(get_connection, is_closing, write_debug, stop_replying)
    registry.register(STOP_REPLYING_SCHEMA, validate_stop_arguments, stop.handle)
    close_task_tool = CloseTaskTool(get_connection, is_closing, write_debug, close_task)
    registry.register(CLOSE_TASK_SCHEMA, validate_close_task_arguments, close_task_tool.handle)
    read_call_tool = ReadCallTool(get_connection, is_closing, write_debug, read_call)
    registry.register(READ_CALL_SCHEMA, validate_read_call_arguments, read_call_tool.handle)
    check_email = CheckEmailTool(get_connection, is_closing, write_debug, get_mailbox)
    registry.register(CHECK_EMAIL_SCHEMA, validate_check_email_arguments, check_email.handle)
    send_email = SendEmailTool(
        get_connection, is_closing, write_debug, get_mailbox, confirm, task_goal, record_email_task, find_email_thread,
        email_signature,
    )
    registry.register(SEND_EMAIL_SCHEMA, validate_send_email_arguments, send_email.handle)
    call_someone = CallSomeoneTool(get_connection, is_closing, write_debug, confirm,
                                   task_goal or _not_set_up, can_call, place_call)
    registry.register(CALL_SOMEONE_SCHEMA, validate_call_arguments, call_someone.handle)
    end_call = EndCallTool(get_connection, is_closing, write_debug)
    registry.register(END_CALL_SCHEMA, validate_end_call_arguments, end_call.handle)
    press_keys = PressKeysTool(get_connection, is_closing, write_debug)
    registry.register(PRESS_KEYS_SCHEMA, validate_press_keys_arguments, press_keys.handle)
    check_calendar = CheckCalendarTool(get_connection, is_closing, write_debug, get_calendar)
    registry.register(CHECK_CALENDAR_SCHEMA, validate_check_calendar_arguments, check_calendar.handle)
    for schema, validate, tool_type in (
        (CREATE_EVENT_SCHEMA, validate_create_arguments, CreateEventTool),
        (UPDATE_EVENT_SCHEMA, validate_update_arguments, UpdateEventTool),
        (CANCEL_EVENT_SCHEMA, validate_cancel_arguments, CancelEventTool),
        (FORWARD_EVENT_SCHEMA, validate_forward_arguments, ForwardEventTool),
    ):
        tool = tool_type(get_connection, is_closing, write_debug, get_calendar, confirm, calendar_changed)
        registry.register(schema, validate, tool.handle)
    sign_up = RequestSignUpTool(get_connection, is_closing, write_debug, check_sign_up, confirm, begin_sign_up)
    registry.register(SIGN_UP_SCHEMA, validate_sign_up_arguments, sign_up.handle)
    # The person's Claude Code sessions (offered while Cora listens for them); their own chats only.
    claude_code = ClaudeCodeTool(get_connection, is_closing, look_at_claude_code, write_debug)
    registry.register(CLAUDE_CODE_SCHEMA, validate_claude_code_arguments, claude_code.handle)
    # Starting, continuing or stopping one (offered while Settings lets her run them); approved each time.
    run_tool = RunClaudeCodeTool(get_connection, is_closing, run_claude_code, write_debug)
    registry.register(RUN_CLAUDE_CODE_SCHEMA, validate_run_claude_code_arguments, run_tool.handle)
    # Rocky, her consultant (offered while Settings > Local model lets her consult him).
    rocky = ConsultRockyTool(get_connection, is_closing, consult_rocky, write_debug)
    registry.register(CONSULT_ROCKY_SCHEMA, validate_consult_rocky_arguments, rocky.handle)
    # The person's own chats only: clients' chats and task work offer allowlists without it.
    note_detail = NoteDetailTool(get_connection, is_closing, write_debug)
    registry.register(NOTE_DETAIL_SCHEMA, validate_note_detail_arguments, note_detail.handle)
    # Pins on the person's WoW map, typed into the game through the push-to-talk keyboard bridge.
    wow_pins = PlaceWowMapPinsTool(get_connection, is_closing, place_map_pins, write_debug)
    registry.register(WOW_PINS_SCHEMA, validate_wow_pins_arguments, wow_pins.handle)
    # The addon's other /cmp commands (clear, done, tracker...), one checked line, the same way.
    wow_command = WowMapPinsCommandTool(get_connection, is_closing, run_map_pins_command, write_debug)
    registry.register(WOW_COMMAND_SCHEMA, validate_wow_command_arguments, wow_command.handle)
    # The house's air conditioners (Settings > Services > Sensibo; offered once set up).
    aircon = AirconTool(get_connection, is_closing, read_aircon, write_debug)
    registry.register(AIRCON_SCHEMA, validate_aircon_arguments, aircon.handle)
    # Which WoW chat channel push-to-talk speaks into (Settings > Game), when the person asks.
    wow_channel = SetWowChatChannelTool(get_connection, is_closing, set_wow_chat_channel, write_debug)
    registry.register(WOW_CHANNEL_SCHEMA, validate_wow_channel_arguments, wow_channel.handle)
    return registry