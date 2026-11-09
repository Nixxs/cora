import asyncio
from os import getenv
from pathlib import Path
from time import monotonic

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, HorizontalGroup
from textual.widgets import Collapsible, RichLog, Static, Button
from textual.worker import WorkerError

from dotenv import load_dotenv

from openai_key import apply_api_key, has_key

from ui.activity import Activity
from ui.audio_status import AudioStatus
from ui.chat_widgets import SelectableRichLog, TitleBar
from ui.composer import ChatComposer
from ui.conversation import Conversation
from ui.dashboard import DashboardScreen
from ui.api_key_view import ApiKeyScreen
from ui.model_settings import EmailControls
from messaging.container import signal_port, start_container, stop_container
from messaging.receiver import IncomingMessage, SignalReceiver
from messaging.setup import SignalSetup
from messaging.signal_cli import SignalClient
from storage.client_store import Client
from storage.signal_settings_store import SignalSettings
from mail.account import EmailAccount, EmailStatus, Mailbox, CALENDAR_SCOPES
from mail.calendar_events import Calendar
from server.listener import Listener
from tools.look_again import KeptImages
from web.location import Whereabouts
from web.feed import MessageFeed
from web.voice import VoiceRelay
from web.pairing import Pairing
from storage.webhook_settings_store import WebhookSettings
from services.confirmation import Confirmations
from services.connection import ServiceManager
from services.tools import ResultCache
from storage.keyboard_persistence import KeyboardPersistence
from storage.live_trace import LiveTrace
from subconscious.attention import AttentionManager
from backend_work import BackendWorkTracker
from subconscious.event_bus import CalendarChangedEvent, EventBus, FindingEvent, MailArrivedEvent, ObservationEvent
from subconscious.interests.exploration_store import claim_mention_now, release_mention_now
from subconscious.interests.relevance import claim_relevant_now
from subconscious.desires.opportunities import release_opportunity_now
from inactivity import InactivityController, read_idle_timeout
from live import CoraLive
from backend_chat import BackendChat
from tools.end_call import CALL_ONLY
from tools.pass_on import CLIENT_ONLY
from live_events import LiveEventHandler
from storage.conversation_persistence import ConversationPersistence
from ui.chat_submission import ChatSubmission
from tools import create_tool_registry
from tools.route import routed
from tools.look_at_screen import set_image_model
from tools.use_browser import BrowserHelper, UseBrowserTool
from wake.controller import WakeController
from app_areas.voice import VoiceMixin
from app_areas.live_session import LiveSessionMixin
from app_areas.attention import AttentionMixin
from app_areas.chat import ChatMixin
from app_areas.settings import SettingsMixin
from app_areas.services import ServicesMixin
from app_areas.signal_setup import SignalSetupMixin
from app_areas.signal_chats import SignalChatsMixin
from app_areas.reaching_you import ReachingYouMixin
from app_areas.tasks import TasksMixin
from app_areas.email_account import EmailAccountMixin
from app_areas.mail import MailMixin
from app_areas.webhooks import WebhooksMixin
from app_areas.web_interface import WebInterfaceMixin
from app_areas.phone_calls import PhoneCallsMixin
from app_areas.contact_calls import ContactCallsMixin
from app_areas.task_calls import TaskCallsMixin
from app_areas.background import BackgroundMixin
from app_areas.sign_ups import SignUpsMixin
from app_areas.feelings import FEELING_CHECK_SECONDS, FeelingsMixin
from app_areas.game import GameMixin
from app_areas.shared import WEBHOOK_STARTUP_SECONDS


load_dotenv()
# A key saved in the app wins over .env's; without either, Cora asks for one at start.
apply_api_key()

# Cheap when not due: the 24-hour gate is one database read. Also lets memories extracted
# at startup clear the store's 5-minute commit margin before the first check.
INTEREST_CHECK_SECONDS = 10 * 60
# Same cheap check for identity: the store's 24-hour gate decides when she really reflects.
IDENTITY_CHECK_SECONDS = 10 * 60
# And for desires: the store's 24-hour gate decides when she really reflects on what she wants.
DESIRE_CHECK_SECONDS = 10 * 60
# The store's limits (6h gap, 3 a day) decide how often she really explores.
EXPLORE_CHECK_SECONDS = 10 * 60
# Offer a finding only into a lull this long, well before the default 5-minute standby.
# The store's limits (4h gap, 2 a day, fresh findings) decide how often she really does.
FINDING_CHECK_SECONDS = 2 * 60
RELEVANCE_CHECK_SECONDS = 5
# Background housekeeping: closes clients' quiet chats so the memory pass picks them up.
BACKGROUND_ROUND_SECONDS = 60 * 60
# Her task email threads are checked this often for replies (and once her email is ready). She
# answers at most EMAIL_REPLIES_PER_DAY times a day in one thread, so two mailboxes answering
# each other automatically soon stop.
TASK_MAIL_CHECK_SECONDS = 10 * 60


# Each area's methods live in app_areas/; no two define the same name, so their order doesn't matter.
class CoraApp(
    LiveSessionMixin, VoiceMixin, AttentionMixin, ChatMixin, SettingsMixin, ServicesMixin,
    SignalSetupMixin, SignalChatsMixin, ReachingYouMixin, TasksMixin, EmailAccountMixin, MailMixin,
    WebhooksMixin, WebInterfaceMixin, PhoneCallsMixin, ContactCallsMixin, TaskCallsMixin, BackgroundMixin, SignUpsMixin, FeelingsMixin, GameMixin, App,
):

    live_trace = None
    CSS_PATH = "cora.tcss"
    BINDINGS = [
        ("ctrl+x", "disconnect_and_quit", "Quit"),
        ("ctrl+r", "reconnect", "Reconnect"),
        Binding("ctrl+t", "test_observation", "Test awareness", priority=True, show=False),
        Binding("ctrl+c", "copy_chat_selection", "Copy selected text", priority=True, show=False),
    ]

    # Compatibility accessors preserve dependency injection without duplicate state.
    @property
    def persistence(self):
        return self.conversation_persistence.storage

    @persistence.setter
    def persistence(self, value):
        self.conversation_persistence.storage = value

    @property
    def pending_keyboard_message(self):
        return self.submission.pending_message

    @pending_keyboard_message.setter
    def pending_keyboard_message(self, value):
        self.submission.pending_message = value

    @property
    def keyboard_worker(self):
        return self.submission.worker

    @keyboard_worker.setter
    def keyboard_worker(self, value):
        self.submission.worker = value

    def compose(self) -> ComposeResult:
        yield TitleBar("dashboard")
        with Vertical(id="chat-panel"):
            yield SelectableRichLog(id="chat-output", wrap=True, min_width=1)
            with Collapsible(title="Activity · Ready", collapsed=True, id="activity-panel"):
                yield RichLog(id="activity-output", wrap=True, min_width=1, max_lines=500)
            with HorizontalGroup(id="status-bar"):
                yield Button(
                    "○ Disconnected", id="chat-status", disabled=True,
                    classes="state-disconnected",
                    tooltip="Live is disconnected. Ctrl+R to reconnect.",
                )
                yield Button("🎙× Mic", id="mic-status", tooltip="Mute microphone")
                yield Button("🔊 Speaker", id="speaker-status", tooltip="Mute speaker")
                yield Static("────────", id="speaker-level")
                yield Button("Wake off", id="wake-status", tooltip="Enable local Hey Cora listening in standby")
            with Vertical(id="input-frame"):
                yield ChatComposer(
                    placeholder="Type a message to Cora...",
                    id="chat-input",
                    soft_wrap=True,
                    show_line_numbers=False,
                    highlight_cursor_line=False,
                )

    def on_mount(self) -> None:
        self.query_one("#chat-input", ChatComposer).focus()
        self.activity = Activity(
            self.query_one("#activity-panel", Collapsible),
            self.query_one("#activity-output", RichLog),
        )
        self.conversation_persistence = ConversationPersistence(
            KeyboardPersistence(), schedule_save=self.save_chat_message,
            is_closing=lambda: self.closing,
            set_memory_context=lambda context: setattr(self.live, "memory_context", context),
            set_interest_context=lambda context: setattr(self.live, "interest_context", context),
            set_identity_context=lambda context: setattr(self.live, "identity_context", context),
            set_self_image_context=lambda context: setattr(self.live, "self_image_context", context),
            set_relationship_context=lambda context: setattr(self.live, "relationship_context", context),
            set_people_context=lambda context: setattr(self.live, "people_context", context),
            set_tasks_context=lambda context: setattr(self.live, "tasks_context", context),
            set_sign_ups_context=lambda context: setattr(self.live, "sign_ups_context", context),
            set_key_details_context=lambda context: setattr(self.live, "key_details_context", context),
            set_desire_context=lambda context: setattr(self.live, "desire_context", context),
            set_feelings=lambda feelings: setattr(self.live, "feelings", feelings),
            set_person_name=lambda name: setattr(self.live, "person_name", name),
            set_own_name=lambda name: setattr(self.live, "own_name", name),
            set_delegation_model=lambda model: setattr(self.live, "delegation_model", model),
            set_voice=lambda voice: setattr(self.live, "voice", voice),
            set_image_model=set_image_model,
            write_activity=self.activity.write,
            notify=lambda message, **options: self.notify(message, **options),
        )
        self.conversation = Conversation(
            self.query_one("#chat-output", RichLog), self.set_timer, self.activity.write,
            record_message=self.record_chat_message,
        )
        self.startup_worker = None
        self.summary_worker = None
        self.interest_worker = None
        self.interest_outcome = None
        self.identity_worker = None
        self.identity_outcome = None
        self.desire_worker = None
        self.desire_outcome = None
        self.exploration_worker = None
        # Signing up to a site (one at a time); explorations leave the browser to it.
        self.sign_up_worker = None
        # Taking a sign-up she'd like, from her exploring, to the person.
        self.sign_up_wish_worker = None
        self.exploration_outcome = None
        self.reach_out_worker = None
        self.closing = False
        self.reconnecting = False
        self.standby = False
        self.reconnect_worker = None
        self.startup_complete = False
        # The next Live session greets first: "launch" once per launch, "welcome_back" after a
        # Standby click. Typing or speaking first cancels it; Ctrl+R and wake never set one.
        self.pending_greeting = "launch"
        # The person's latest message the relevance check has looked at: (their message count, text).
        self.relevance_seen = None
        # Whether a chance for one of her desires may be waiting to offer (one saved before launch may be).
        self.opportunity_waiting = True
        # What the last opportunity check looked at, and when (monotonic), so unchanged inputs aren't re-sent.
        self.opportunity_inputs = None
        self.opportunity_checked_at = None
        self.opportunity_worker = None
        self.backend_work = BackendWorkTracker()
        self.last_live_activity = 0.0
        self.idle_standby_timer = None
        self.requested_standby_timer = None
        self.requested_standby_deadline = 0.0
        self.conversation_end_timer = None
        # Settings > General. Kept for this run only; not saved yet.
        self.screen_looking = True
        # Settings > Game: push-to-talk into the game's chat.
        self.init_game()
        try:
            idle_timeout_seconds = read_idle_timeout()
        except ValueError:
            idle_timeout_seconds = 0.0
            self.activity.write(
                "Invalid CORA_IDLE_TIMEOUT_SECONDS; automatic standby disabled. Use finite seconds >= 0.",
                "Idle timeout configuration error",
            )
        self.inactivity = InactivityController(
            timeout_seconds=idle_timeout_seconds,
            farewell_enabled=getenv("CORA_STANDBY_FAREWELL", "0").strip() == "1",
            clock=lambda: monotonic(),
            can_standby=lambda: not (self.closing or self.reconnecting or self.standby) and self.live.ready,
            speaker_muted=lambda: self.live.speaker_muted,
            transition_blockers=self.live_transition_blockers,
            send_alert=self.send_standby_alert,
            enter_standby=lambda: self.action_standby(),
            write_activity=self.activity.write,
        )
        # Tools reply to Live, or to a chat without Live when the call came from one.
        self.tool_connection = routed(lambda: self.live.connection)
        # Her own mailbox, set up in Settings > Services > Email; optional.
        self.email_account = EmailAccount()
        self.mailbox = Mailbox(self.email_account.token)
        # Her calendar comes with her email sign-in.
        self.calendar = Calendar(lambda: self.email_account.token(CALENDAR_SCOPES))
        # Why her calendar couldn't last be read, so it's said once, not hourly.
        self.calendar_problem = ""
        self.email_status = EmailStatus()
        self.email_controls: EmailControls | None = None
        # The person's photos from this conversation, in memory only, for look_again.
        self.kept_images = KeptImages()
        self.tools = create_tool_registry(
            get_connection=self.tool_connection,
            is_closing=lambda: self.closing or self.reconnecting,
            shutdown=lambda: self.action_disconnect_and_quit(),
            request_standby=self.request_standby,
            write_debug=self.activity.write,
            schedule=lambda action: self.run_worker(action, group="tool-actions"),
            screen_looking=lambda: self.screen_looking,
            kept_images=self.kept_images,
            locate_person=lambda refresh: self.locate_person(refresh),
            show_map=lambda view: self.show_map(view),
            can_call=lambda number: self.can_call_out(number),
            place_call=lambda *args, **options: self.place_task_call(*args, **options),
            show_image=self.conversation.show_image,
            find_signal_contact=self.find_signal_contact,
            # Looked up when called: the approval queue is set up after the tools.
            confirm=lambda *args, **options: self.confirmations.ask(*args, **options),
            send_signal=self.send_to_signal_contact,
            signal_unreachable=self.signal_contact_unreachable,
            task_goal=self.task_goal, record_task=self.record_task, close_task=self.close_task,
            get_mailbox=lambda: self.mailbox, record_email_task=self.record_email_task,
            relay_to_person=self.relay_to_person, relay_from_email=self.relay_from_email,
            find_email_thread=self.find_email_thread, email_signature=lambda: self.email_signature,
            get_calendar=lambda: self.calendar, calendar_changed=lambda: self.refresh_calendar(),
            raise_concern=self.raise_client_concern, stop_replying=self.stop_replying_to_client,
            check_sign_up=self.check_sign_up, begin_sign_up=self.begin_sign_up,
            signal_reaches_person=self.signal_reaches_person, message_person=self.tell_person_on_signal,
            note_feeling=self.note_feeling,
        )
        # Live never sees client-only tools such as pass_on.
        self.live = CoraLive(tool_schemas=lambda: self.tools.schemas(excluding=CLIENT_ONLY | CALL_ONLY | self.unavailable_tools()),
                             on_trace=self.trace_live)
        # Outside services over MCP. The tools switched on in Settings join Cora's at her next connect.
        self.service_tool_choices: dict[str, set[str]] = {}
        # Set when Cora's service tools changed since Live last connected.
        self.service_tools_changed = False
        # The browser's own tools, {name: (schema, run)}, for her browser helper while the browser is
        # connected; she has use_browser instead of them (see sync_service_tools).
        self.browser_tools: dict = {}
        self.browser_helper = BrowserHelper(
            lambda: self.browser_tools, self.browser_model, self.details_for_forms, self.activity.write,
        )
        self.use_browser = UseBrowserTool(
            self.tool_connection, lambda: self.closing or self.reconnecting, self.activity.write, self.browser_helper,
        )
        # Set when her voice changed in Settings; closing Settings reconnects with it.
        self.voice_changed = False
        # Live's connect; None until the first, which waits for an OpenAI key if there isn't one.
        self.connection_worker = None
        # Cora's own Signal account: off and unused unless set up in Settings > Services.
        self.signal_settings = SignalSettings()
        self.signal = SignalSetup(
            SignalClient(f"http://127.0.0.1:{signal_port()}"), start_container, stop_container,
            save_account=self.save_signal_account, write_activity=self.activity.write,
        )
        # Listening for, and answering, the person's Signal messages.
        self.signal_receiver = None
        self.signal_listening_as: str | None = None
        # Waiting messages and the worker answering them, per sender's number.
        self.signal_waiting: dict[str, list[IncomingMessage]] = {}
        self.signal_answering: dict = {}
        # People added in Settings, by number, and numbers that already got the stranger reply.
        self.signal_clients: dict[str, Client] = {}
        self.signal_introduced: set[str] = set()
        # Signal IDs of the person and everyone added, by ID, and numbers whose requests she accepted this run.
        self.signal_ids: dict[str, str] = {}
        self.signal_accepted: set[str] = set()
        # The person's answer to a request asked in their Signal chat, by their number.
        self.signal_approvals: dict[str, asyncio.Future] = {}
        self.signal_approval_lock = asyncio.Lock()
        # How many things each client passed on to the person today, by (client id, day).
        self.signal_relays: dict[tuple, int] = {}
        self.signal_concerns: dict[tuple, int] = {}
        # Client conversations she ended; their messages are saved but not answered. A restart
        # starts new conversations, so this needn't outlive the app.
        self.signal_stopped: set = set()
        # Heads-ups on their way to the person, so a second round doesn't send them twice.
        self.heads_ups_pending: set = set()
        # Tasks with a coordinator turn under way, so the hourly round doesn't start a second one.
        self.tasks_coordinating: set = set()
        self.task_coordinator_lock = asyncio.Lock()
        # The mail check under way, and her replies in each task's email thread today, by (party id, day).
        self.task_mail_worker = None
        self.task_mail_again = False
        self.email_replies: dict[tuple, int] = {}
        # The person's own address (Settings > Services > Email), and their emails answered one at a time.
        self.email_person = ""
        # Her signature for new emails (Settings > Services > Email).
        self.email_signature = ""
        self.person_email_lock = asyncio.Lock()
        # Her other mail already sorted, and what's waiting for the digest, by message id.
        self.mail_sorted: set[str] = set()
        self.mail_digest: dict = {}
        self.mail_digest_sending = False
        # Webhooks: her local listener, its settings and secrets, Graph's subscription to new mail,
        # and what Settings shows of them. The lock keeps starting and stopping in order.
        self.webhook_settings = WebhookSettings()
        self.webhook_server: Listener | None = None
        self.webhook_worker = None
        self.webhook_secrets: tuple[str, str] | None = None
        self.webhook_lock = asyncio.Lock()
        self.webhook_status = ""
        self.mail_subscription = None
        self.mail_subscription_worker = None
        self.mail_subscription_status = ""
        # Graph's subscription to changes in her calendar, kept like the mail one.
        self.calendar_subscription = None
        self.calendar_subscription_worker = None
        self.calendar_subscription_status = ""
        self.calendar_notify_timer = None
        self.mail_notify_timer = None
        self.webhook_controls = None
        # The web interface, on the same listener: the open pairing code (if any), the paired
        # devices as Settings last loaded them, and what Settings shows.
        self.web_pairing = Pairing()
        # One web message answered at a time (see answer_on_web).
        self.web_reply_lock = asyncio.Lock()
        # The conversation as the web page sees it: every message said, and earlier ones loaded
        # from the database the first time a page connects.
        self.web_feed = MessageFeed()
        # The desk's Live state so far (set_connection_state keeps it current from here).
        status = self.query_one("#chat-status", Button)
        self.web_feed.set_status(next(
            (state for state in ("active", "standby", "connecting", "disconnecting")
             if status.has_class(f"state-{state}")), "disconnected",
        ))
        self.web_feed_lock = asyncio.Lock()
        # Requests for approval waiting on the page, by id (see ask_on_web).
        self.web_approvals: dict[str, asyncio.Future] = {}
        # Her voice for devices listening on the page; Live plays to it instead of the desk while any do.
        self.web_voice = VoiceRelay()
        self.live.voice_relay = self.web_voice
        # Whether a page's Talk woke her from standby, so hanging up puts her back.
        self.web_talk_woke = False
        # Where the person is talking from, still to be told to her (she was waking), or None.
        self.web_talk_where: str | None = None
        self.conversation.listeners.append(self.publish_to_web)
        # The person's photos and the pictures she shows reach the page too, from memory.
        self.web_pictures = KeptImages(prefix="pic")
        self.conversation.photo_listeners.append(self.publish_web_photo)
        self.conversation.picture_listeners.append(self.publish_web_picture)
        # Where the person is, asked of the open pages on demand (where_is_person), in memory only.
        self.whereabouts = Whereabouts(self.web_feed.ask_location)
        # The Google Maps key for maps on the page (Settings › Services › Web interface), or "".
        self.maps_key = ""
        # Phone calls to her number: Twilio's auth token (checks each call came from Twilio), or "".
        self.twilio_token = ""
        self.phone_controls = None
        # One-time tokens given to calls Twilio is connecting, until their stream opens (see phone_calls).
        self.phone_tokens: dict[str, float] = {}
        # The phone call's place in the voice relay while a call is connected to her.
        self.phone_listener = None
        # A contact's call while it's on (one at a time), and how many each has made today.
        self.contact_call = None
        self.contact_calls_today: dict = {}
        # A call she placed for a task, from placing it until it's over (one at a time).
        self.task_call = None
        self.web_devices = []
        self.web_controls = None
        self.signal_listener: SignalReceiver | None = None
        self.signal_startup = None
        self.signal_last_message_at: float | None = None
        # Signal messages answered at launch, before the opening greeting, which follows on from them.
        self.signal_launch_exchanges: list[tuple[str, str]] = []
        # Recent long replies from service tools, so Cora can read their later pages.
        self.service_results = ResultCache()
        # Tool calls that change things wait here for the person's answer.
        self.confirmations = Confirmations(
            show=self.show_confirmation, close=self.close_confirmation, write_activity=self.activity.write,
            remind=lambda request: self.tell_cora_approval_waiting(request.title, request.tool, reminder=True),
            # How each ended goes in the chat, and into the history Cora is given later.
            on_outcome=lambda request, outcome: self.conversation.show_approval_outcome(
                f"{request.title} · {request.tool}", outcome,
            ),
            # While the person is on a phone call: in their Signal chat, never by voice (see phone_calls).
            elsewhere=lambda: self.phone_approval_route(),
        )
        self.services = ServiceManager(
            schedule=lambda run: self.run_worker(run, group="services", exit_on_error=False),
            write_activity=self.activity.write,
            on_change=self.sync_service_tools,
        )
        self.submission = ChatSubmission(
            persistence=self.conversation_persistence, conversation=self.conversation,
            live=self.live, backend_work=self.backend_work,
            is_closing=lambda: self.closing, is_reconnecting=lambda: self.reconnecting,
            is_standby=lambda: self.standby, schedule=self.submit_keyboard_message,
            record_activity=self.record_idle_activity, write_activity=self.activity.write,
            notify=lambda message, **options: self.notify(message, **options),
            trace=self.trace_live, send_without_live=self.send_without_live,
            keep_image=self.kept_images.keep,
        )
        self.backend_chat = BackendChat(self.tools, self.activity.write)
        self.wake = WakeController(
            live=self.live, control=self.query_one("#wake-status", Button),
            enabled=getenv("CORA_WAKE_ENABLED", "0").strip() == "1",
            debug=getenv("CORA_WAKE_DEBUG", "1").strip() == "1",
            clock=lambda: monotonic(),
            can_listen=lambda: self.standby and not (self.closing or self.reconnecting),
            is_closing=lambda: self.closing, is_reconnecting=lambda: self.reconnecting,
            request_connection=self.request_wake_connection,
            abort_connection=lambda: self.abort_wake_connection(),
            record_idle_activity=self.record_idle_activity,
            transition_blockers=self.live_transition_blockers,
            enter_standby=lambda: self.action_standby(), write_activity=self.activity.write,
            set_timer=self.set_timer, set_interval=self.set_interval,
        )
        self.audio_status = AudioStatus(
            self.query_one("#mic-status", Button),
            self.query_one("#speaker-status", Button),
            self.query_one("#speaker-level", Static),
            self.live, self.set_timer, self.set_interval,
            lambda: self.closing, self.update_wake_control,
        )
        self.live_events = LiveEventHandler(
            live=self.live, backend_work=self.backend_work, conversation=self.conversation,
            audio_status=self.audio_status, tools=self.tools, write_activity=self.activity.write,
            cancel_standby_alert=self.cancel_standby_alert,
            record_idle_activity=self.record_idle_activity,
            handle_session_event=self.handle_live_session_event,
        )
        self.attention = AttentionManager(
            can_deliver=self.can_deliver_observation,
            is_closing=lambda: self.closing,
            schedule_delivery=self.send_next_observation,
            send_observation=lambda observation: self.live.send_observation(observation),
            record_activity=self.record_idle_activity,
            delivery_finished=self.observation_delivery_finished,
            write_activity=self.activity.write,
            claim_finding=lambda: asyncio.to_thread(claim_mention_now),
            send_finding=lambda mention: self.live.send_finding(
                mention.interest_title, mention.finding, mention.found_at,
            ),
            release_finding=lambda mention: asyncio.to_thread(release_mention_now, mention),
            send_message=lambda text: self.live.send_message_for_person(text),
            claim_relevant=lambda conversation: asyncio.to_thread(claim_relevant_now, conversation),
            claim_opportunity=self.claim_opportunity,
            send_opportunity=lambda chance: self.live.send_opportunity(chance.wish, chance.opportunity),
            release_opportunity=lambda chance: asyncio.to_thread(release_opportunity_now, chance),
            send_relevant=lambda mention: self.live.send_relevant_finding(
                mention.interest_title, mention.finding, mention.found_at,
            ),
        )
        self.event_bus = EventBus(on_error=self.handle_event_bus_error)
        self.event_bus.subscribe(
            ObservationEvent, lambda event: self.attention.submit(event.content),
        )
        self.event_bus.subscribe(FindingEvent, lambda event: self.attention.submit_finding())
        self.event_bus.subscribe(MailArrivedEvent, lambda event: self.on_mail_arrived())
        self.event_bus.subscribe(CalendarChangedEvent, lambda event: self.on_calendar_changed())
        if self.live_trace is not None:
            self.trace_live("app.started")
            if not self.live_trace.failed:
                self.activity.write(
                    f"Diagnostic trace: {self.live_trace.path.name} (IDs/state only; no chat text or audio).",
                    "Diagnostic trace enabled",
                )
        if has_key():
            self.connection_worker = self.connect_to_cora()
        else:
            # Without a key nothing can reach OpenAI: ask before connecting.
            self.activity.write("No OpenAI API key yet; asking for one before connecting.", "OpenAI key needed")
            self.push_screen(ApiKeyScreen(self.check_and_save_openai_key), callback=self.api_key_given)
        self.update_wake_control()
        self.observation_timer = self.set_interval(0.5, self.check_observations)
        self.interest_timer = self.set_interval(INTEREST_CHECK_SECONDS, self.check_interests)
        self.identity_timer = self.set_interval(IDENTITY_CHECK_SECONDS, self.check_identity)
        self.desire_timer = self.set_interval(DESIRE_CHECK_SECONDS, self.check_desires)
        self.exploration_timer = self.set_interval(EXPLORE_CHECK_SECONDS, self.check_exploration)
        self.finding_timer = self.set_interval(FINDING_CHECK_SECONDS, self.check_findings)
        self.relevance_timer = self.set_interval(RELEVANCE_CHECK_SECONDS, self.check_relevance)
        self.feeling_timer = self.set_interval(FEELING_CHECK_SECONDS, self.check_feelings)
        self.background_round_timer = self.set_interval(BACKGROUND_ROUND_SECONDS, self.background_round)
        self.task_mail_timer = self.set_interval(TASK_MAIL_CHECK_SECONDS, self.mail_round)
        # Always running: Settings can turn automatic standby on at any time (the check skips while it's off).
        self.idle_standby_timer = self.set_interval(1.0, self.check_inactivity)

    def record_idle_activity(self, *, user_activity: bool = False) -> None:
        self.inactivity.record_activity()
        if user_activity:
            self.pending_greeting = None
            self.wake.response_deadline = None
            self.cancel_standby_alert("User activity; staying active.")

    def trace_live(self, stage: str, **metadata) -> None:
        trace = self.live_trace
        if trace is None or trace.failed:
            return
        trace.write(
            stage, pending_backend_work=sorted(self.backend_work.pending, key=str),
            tool_response_ids=sorted(self.backend_work.tool_response_ids, key=str), **metadata,
        )
        if trace.failed:
            self.activity.write("Could not write diagnostic trace; Live continues unchanged.", "Diagnostic trace unavailable")

    async def on_unmount(self) -> None:
        if self.webhook_server is not None:
            self.webhook_server.stop()
        self.confirmations.decline_all()
        self.email_account.cancel_sign_in()
        self.stop_signal_receiver()
        self.services.stop_all()
        self.stop_push_to_talk()
        self.event_bus.close()
        self.attention.stop()
        self.observation_timer.stop()
        self.interest_timer.stop()
        self.identity_timer.stop()
        self.desire_timer.stop()
        self.exploration_timer.stop()
        self.finding_timer.stop()
        self.background_round_timer.stop()
        self.cancel_standby_alert()
        self.wake.stop_timers()
        await self.stop_wake_listener()
        self.audio_status.stop()
        if self.idle_standby_timer is not None:
            self.idle_standby_timer.stop()
        self.stop_requested_standby()
        self.stop_conversation_end_timer()

    def report_interrupted_work(self) -> None:
        self.cancel_standby_alert()
        if self.backend_work.pending:
            self.activity.write(
                "Live ended with unfinished backend work; it will not be replayed.", "Work interrupted",
            )
            self.notify("Live ended with unfinished work. Reconnect will not retry it.", severity="warning")
        self.backend_work.clear()

    def on_collapsible_expanded(self, event: Collapsible.Expanded) -> None:
        if event.collapsible.id == "activity-panel":
            # Wait for layout to restore the hidden log's usable width.
            self.call_after_refresh(self.activity.redraw)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        self.record_idle_activity(user_activity=True)
        if event.button.id == "close-button":
            event.button.disabled = True
            await self.action_disconnect_and_quit()
        elif event.button.id == "settings-button" and not self.closing:
            self.open_model_settings()
        elif event.button.id == "view-button" and not self.closing:
            if isinstance(self.screen, DashboardScreen):
                self.pop_screen()
            else:
                self.push_screen(DashboardScreen(
                    self.voice_bands, self.conversation, self.audio_status,
                    self.query_one("#chat-status", Button), self.conversation_persistence.load_dashboard_snapshot, self.activity,
                    self.live.context, load_spend=self.load_spend,
                ))
        elif event.button.id == "chat-status":
            if self.standby:
                self.action_reconnect()
                if self.reconnecting:
                    self.pending_greeting = "welcome_back"
            else:
                self.action_standby()
        elif event.button.id == "wake-status" and not self.closing:
            self.wake.enabled = not self.wake.enabled
            self.wake.failed = False
            if not self.wake.enabled:
                self.wake.reconnecting = False
                using_wake = self.live.opening_microphone is not None
                await self.stop_wake_listener()
                if using_wake:
                    await self.live.stop_microphone()
                    if self.live.ready:
                        self.live.microphone_task = asyncio.create_task(
                            self.live.send_microphone(self.live.connection, self.handle_mic_status),
                        )
            else:
                self.start_wake_listener()
            self.update_wake_control()
            # Remembered for the next launch.
            self.save_wake_word(self.wake.enabled)
            self.query_one("#chat-input", ChatComposer).focus()
        elif (event.button.id in ("mic-status", "speaker-status") and not self.closing
              and (not self.reconnecting or event.button.id == "mic-status")):
            button = event.button
            button.disabled = True
            try:
                if button.id == "mic-status":
                    await self.live.set_microphone_muted(not self.live.microphone_muted)
                    if self.live.microphone_muted:
                        self.wake.reconnecting = False
                        await self.stop_wake_listener()
                    else:
                        self.start_wake_listener()
                    self.handle_mic_status(False)
                    summary = "Microphone muted" if self.live.microphone_muted else "Microphone unmuted"
                else:
                    self.live.set_speaker_muted(not self.live.speaker_muted)
                    summary = "Speaker muted" if self.live.speaker_muted else "Speaker unmuted"
                self.activity.write(summary, summary)
            except Exception as error:
                self.activity.write(f"Audio control failed ({type(error).__name__})", "Audio control error")
                self.notify("Could not change audio playback or capture. Check Activity.", severity="error")
            finally:
                self.record_idle_activity()
                self.update_audio_controls()
                button.disabled = self.closing
                if not self.closing:
                    self.query_one("#chat-input", ChatComposer).focus()

    async def action_disconnect_and_quit(self) -> None:
        if self.closing:
            return
        self.closing = True
        # Microsoft stops calling before the listener goes; never long enough to hold up closing.
        try:
            await asyncio.wait_for(self.stop_webhooks(), WEBHOOK_STARTUP_SECONDS)
        except Exception:
            if self.webhook_server is not None:
                self.webhook_server.stop()
        # Nothing still waiting for approval may run while shutting down.
        self.confirmations.decline_all()
        self.email_account.cancel_sign_in()
        self.stop_signal_receiver()
        self.stop_push_to_talk()
        self.event_bus.close()
        self.attention.stop()
        self.observation_timer.stop()
        self.interest_timer.stop()
        self.identity_timer.stop()
        self.desire_timer.stop()
        self.exploration_timer.stop()
        self.finding_timer.stop()
        self.background_round_timer.stop()
        self.cancel_standby_alert()
        self.wake.response_deadline = None
        self.wake.stop_timers()
        await self.stop_wake_listener()
        self.audio_status.stop()
        if self.idle_standby_timer is not None:
            self.idle_standby_timer.stop()
        self.stop_requested_standby()
        self.update_speaking_animation()
        self.query_one("#close-button", Button).disabled = True
        self.query_one("#settings-button", Button).disabled = True
        self.query_one("#view-button", Button).disabled = True
        self.query_one("#mic-status", Button).disabled = True
        self.query_one("#speaker-status", Button).disabled = True
        self.query_one("#wake-status", Button).disabled = True
        self.query_one("#chat-input", ChatComposer).disabled = True
        self.set_connection_state("disconnecting")

        try:
            if self.startup_worker is not None:
                try:
                    await self.startup_worker.wait()
                except WorkerError:
                    self.activity.write("Database startup worker stopped", "Database startup worker stopped")
            if self.summary_worker is not None and not self.summary_worker.is_finished:
                self.summary_worker.cancel()
            if self.interest_worker is not None and not self.interest_worker.is_finished:
                self.interest_worker.cancel()
            if self.identity_worker is not None and not self.identity_worker.is_finished:
                self.identity_worker.cancel()
            if self.desire_worker is not None and not self.desire_worker.is_finished:
                self.desire_worker.cancel()
            if self.exploration_worker is not None and not self.exploration_worker.is_finished:
                self.exploration_worker.cancel()
            if self.attention.worker is not None and not self.attention.worker.is_finished:
                try:
                    await self.attention.worker.wait()
                except WorkerError:
                    self.activity.write("Observation send worker stopped", "Awareness stopped")
            if self.keyboard_worker is not None:
                try:
                    await self.keyboard_worker.wait()
                except WorkerError:
                    self.activity.write("Keyboard submission worker stopped", "Keyboard worker stopped")
            if self.reconnect_worker is not None and not self.reconnect_worker.is_finished:
                try:
                    await self.reconnect_worker.wait()
                except WorkerError:
                    self.activity.write("Reconnect worker stopped", "Reconnect worker stopped")
            await self.live.close()
        finally:
            try:
                if self.connection_worker is not None:
                    self.connection_worker.cancel()
                await self.conversation_persistence.finish(self.conversation.flush_transcript)
            finally:
                self.exit()

if __name__ == "__main__":
    app = CoraApp()
    app.live_trace = LiveTrace(Path(__file__).with_name("live-trace.log"))
    app.run()