import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from os import getenv
import sys
from pathlib import Path
from time import monotonic
from uuid import UUID

import pyperclip
from textual.actions import SkipAction
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, HorizontalGroup
from textual.widgets import Collapsible, RichLog, Static, Button, TextArea
from textual.worker import WorkerError, get_current_worker

from dotenv import load_dotenv
from textual import events, work

from ui.activity import Activity
from ui.audio_status import AudioStatus
from ui.chat_widgets import SelectableRichLog, TitleBar
from ui.composer import ChatComposer
from ui.conversation import Conversation
from ui.dashboard import DashboardScreen
from ui.model_settings import ModelSettingsScreen, ServiceControls, SignalControls
from backup import BackupError, daily_backup_due, run_backup
from messaging.container import (
    clear_attachments, delete_attachments, signal_port, start_container, stop_container,
)
from messaging.images import read_images
from messaging.messages import (
    APPROVAL_SECONDS, SIGNAL_SORRY, approval_answer, approval_request_text, signal_content,
    signal_message_text,
)
from messaging.receiver import IncomingMessage, SignalReceiver
from messaging.setup import (
    FAILED as SIGNAL_FAILED, LINKED as SIGNAL_LINKED, NOT_LINKED as SIGNAL_NOT_LINKED, OFF as SIGNAL_OFF, SignalSetup,
)
from messaging.signal_cli import SignalClient, signal_failure
from storage.client_store import Client
from storage.signal_settings_store import SignalSettings, normalise_number
from services.catalogue import SERVICES
from services.confirmation import Confirmations
from services.connection import CONNECTED, ServiceManager
from ui.confirm_view import ConfirmScreen
from services.tools import PAGE_ARGUMENT, ResultCache, ServiceToolHandler, tool_name, tool_schema
from services.tools import validate_arguments as validate_service_arguments
from services.credentials import forget_token, get_token, save_token
from storage.keyboard_persistence import KeyboardPersistence
from storage.live_trace import LiveTrace
from subconscious.attention import AttentionManager
from backend_work import BackendWorkTracker
from subconscious.event_bus import EventBus, FindingEvent, ObservationEvent
from subconscious.identity.reflect_identity import run_identity_cycle
from subconscious.interests.evolve_interests import run_interest_cycle
from subconscious.interests.exploration_store import claim_mention_now, release_mention_now
from subconscious.interests.explore_interest import run_exploration_cycle
from inactivity import InactivityController, StandbyAlert, read_idle_timeout
from greeting import (
    choose_hook, finding_hook, greeting_brief, knows_them_well, last_thread_hook, memory_hook, signal_hook,
)
from live import CoraLive, typed_content
from backend_chat import SIGNAL_EXCLUDED, STANDBY_EXCLUDED, BackendChat, BackendRoute
from tools.tell_nick import CLIENT_ONLY
from session_config import (
    SIGNAL_CHAT_INSTRUCTIONS, client_chat_instructions, instruction_sections, stranger_reply,
)
from live_events import LiveEventHandler
from storage.conversation_persistence import ConversationPersistence
from ui.chat_submission import ChatSubmission
from tools import create_tool_registry
from tools.route import routed
from wake.controller import WakeController
from wake.listener import WakeListener


load_dotenv()

# Cheap when not due: the 24-hour gate is one database read. Also lets memories extracted
# at startup clear the store's 5-minute commit margin before the first check.
INTEREST_CHECK_SECONDS = 10 * 60
# Same cheap check for identity: the store's 24-hour gate decides when she really reflects.
IDENTITY_CHECK_SECONDS = 10 * 60
# The store's limits (6h gap, 3 a day) decide how often she really explores.
EXPLORE_CHECK_SECONDS = 10 * 60
# Explore only in standby or after this long without conversation.
EXPLORE_IDLE_SECONDS = 10 * 60
# Offer a finding only into a lull this long, well before the default 5-minute standby.
# The store's limits (4h gap, 2 a day, fresh findings) decide how often she really does.
FINDING_CHECK_SECONDS = 2 * 60
FINDING_IDLE_SECONDS = 90
# Cora's own standby request waits for her sign-off; give up rather than surprise the person later.
REQUESTED_STANDBY_TIMEOUT_SECONDS = 30
# A shorter standby continues the same conversation; a longer one ends it so memories form.
CONVERSATION_END_STANDBY_SECONDS = 3 * 60 * 60
# At launch, how long Live waits for services to connect so their tools are in the first session.
SERVICE_STARTUP_WAIT_SECONDS = 10
# The most things one client can pass on to the person in a day.
SIGNAL_RELAYS_PER_DAY = 5
# At launch, the greeting waits up to this long for Signal messages that waited while Cora was
# closed to be answered, counting them all in once nothing new has come for SIGNAL_QUIET_SECONDS.
SIGNAL_GREETING_WAIT_SECONDS = 20
SIGNAL_QUIET_SECONDS = 3


class CoraApp(App):

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
            set_person_name=lambda name: setattr(self.live, "person_name", name),
            set_delegation_model=lambda model: setattr(self.live, "delegation_model", model),
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
        self.exploration_worker = None
        self.exploration_outcome = None
        self.closing = False
        self.reconnecting = False
        self.standby = False
        self.reconnect_worker = None
        self.startup_complete = False
        # The next Live session greets first: "launch" once per launch, "welcome_back" after a
        # Standby click. Typing or speaking first cancels it; Ctrl+R and wake never set one.
        self.pending_greeting = "launch"
        self.backend_work = BackendWorkTracker()
        self.last_live_activity = 0.0
        self.idle_standby_timer = None
        self.requested_standby_timer = None
        self.requested_standby_deadline = 0.0
        self.conversation_end_timer = None
        # Settings > General. Kept for this run only; not saved yet.
        self.screen_looking = True
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
        self.tools = create_tool_registry(
            get_connection=self.tool_connection,
            is_closing=lambda: self.closing or self.reconnecting,
            shutdown=lambda: self.action_disconnect_and_quit(),
            request_standby=self.request_standby,
            write_debug=self.activity.write,
            schedule=lambda action: self.run_worker(action, group="tool-actions"),
            screen_looking=lambda: self.screen_looking,
            show_image=self.conversation.show_image,
            find_signal_contact=self.find_signal_contact,
            # Looked up when called: the approval queue is set up after the tools.
            confirm=lambda *args, **options: self.confirmations.ask(*args, **options),
            send_signal=self.send_to_signal_contact,
            signal_unreachable=self.signal_contact_unreachable,
            relay_to_person=self.relay_to_person,
        )
        # Live never sees client-only tools such as tell_nick.
        self.live = CoraLive(tool_schemas=lambda: self.tools.schemas(excluding=CLIENT_ONLY), on_trace=self.trace_live)
        # Outside services over MCP. The tools switched on in Settings join Cora's at her next connect.
        self.service_tool_choices: dict[str, set[str]] = {}
        # Set when Cora's service tools changed since Live last connected.
        self.service_tools_changed = False
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
        # How many things each client passed on to the person today, by (client id, day).
        self.signal_relays: dict[tuple, int] = {}
        self.signal_listener: SignalReceiver | None = None
        self.signal_startup = None
        self.signal_last_message_at: float | None = None
        # Signal messages answered at launch, before the opening greeting, which follows on from them.
        self.signal_launch_exchanges: list[tuple[str, str]] = []
        # Service tools that ask for approval: the pop-up is on the desk, so Signal leaves them out.
        self.confirming_tools: frozenset[str] = frozenset()
        # Recent long replies from service tools, so Cora can read their later pages.
        self.service_results = ResultCache()
        # Tool calls that change things wait here for the person's answer.
        self.confirmations = Confirmations(
            show=self.show_confirmation, close=self.close_confirmation, write_activity=self.activity.write,
            remind=lambda request: self.tell_cora_approval_waiting(request.title, request.tool, reminder=True),
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
        )
        self.event_bus = EventBus(on_error=self.handle_event_bus_error)
        self.event_bus.subscribe(
            ObservationEvent, lambda event: self.attention.submit(event.content),
        )
        self.event_bus.subscribe(FindingEvent, lambda event: self.attention.submit_finding())
        if self.live_trace is not None:
            self.trace_live("app.started")
            if not self.live_trace.failed:
                self.activity.write(
                    f"Diagnostic trace: {self.live_trace.path.name} (IDs/state only; no chat text or audio).",
                    "Diagnostic trace enabled",
                )
        self.connection_worker = self.connect_to_cora()
        self.update_wake_control()
        self.observation_timer = self.set_interval(0.5, self.check_observations)
        self.interest_timer = self.set_interval(INTEREST_CHECK_SECONDS, self.check_interests)
        self.identity_timer = self.set_interval(IDENTITY_CHECK_SECONDS, self.check_identity)
        self.exploration_timer = self.set_interval(EXPLORE_CHECK_SECONDS, self.check_exploration)
        self.finding_timer = self.set_interval(FINDING_CHECK_SECONDS, self.check_findings)
        if self.inactivity.timeout_seconds > 0:
            self.idle_standby_timer = self.set_interval(1.0, self.check_inactivity)

    def record_idle_activity(self, *, user_activity: bool = False) -> None:
        self.inactivity.record_activity()
        if user_activity:
            self.pending_greeting = None
            self.wake.response_deadline = None
            self.cancel_standby_alert("User activity; staying active.")

    def update_wake_control(self) -> None:
        self.wake.update_control()

    def start_wake_listener(self) -> None:
        self.wake.start_listener()

    async def stop_wake_listener(self) -> None:
        await self.wake.stop_listener()

    def handle_wake_status(self, working: bool, message: str) -> None:
        self.wake.handle_status(working, message)

    def handle_wake_diagnostic(self, message: str) -> None:
        self.wake.handle_diagnostic(message)

    def handle_wake(self, listener: WakeListener) -> bool:
        return self.wake.handle_wake(listener)

    def request_wake_connection(self) -> bool:
        return self.request_live_transition(restart=True, from_wake=True)

    @work(group="wake-abort")
    async def abort_wake_connection(self) -> None:
        if not self.wake.reconnecting or self.live.ready or self.closing:
            return
        self.wake.reconnecting = False
        self.wake.failed = True
        self.wake.response_deadline = None
        if self.reconnect_worker is not None and not self.reconnect_worker.is_finished:
            self.reconnect_worker.cancel()
            try:
                await self.reconnect_worker.wait()
            except WorkerError:
                pass
        await self.stop_wake_listener()
        if self.connection_worker is not None:
            self.connection_worker.cancel()
            try:
                await self.connection_worker.wait()
            except WorkerError:
                pass
        if self.live.connection is None:
            self.standby = True
            self.set_connection_state("standby")
        self.activity.write("Wake connection failed/timed out; audio discarded. Resume manually or toggle Wake to retry.", "Wake cancelled")
        # No automatic retries after a connection/capture failure.

    def check_wake_timeout(self) -> None:
        self.wake.check_timeout()

    def on_key(self, event: events.Key) -> None:
        self.record_idle_activity(user_activity=True)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if isinstance(event.text_area, ChatComposer):
            self.record_idle_activity(user_activity=True)

    def on_text_area_selection_changed(self, event: TextArea.SelectionChanged) -> None:
        if isinstance(event.text_area, ChatComposer):
            self.record_idle_activity(user_activity=True)

    def check_inactivity(self) -> None:
        self.inactivity.check()

    @work(group="standby-alert")
    async def send_standby_alert(self, alert: StandbyAlert) -> None:
        try:
            await asyncio.wait_for(self.live.send_standby_alert(alert.event_id), timeout=5)
        except Exception as error:
            if self.inactivity.alert is alert:
                self.cancel_standby_alert(
                    f"Heads-up send failed ({type(error).__name__}); staying active.", cancel_send=False,
                )

    async def choose_launch_hook(self, last_talked_at, relationship=None):
        """Return (hook text, claimed finding to release if the greeting is not sent)."""
        options = []
        if last_talked_at:
            options = ["finding", "last_thread", *(["memory"] if knows_them_well(relationship) else [])]
        kind = choose_hook(options)
        if kind == "memory":
            plan = await self.conversation_persistence.load_recent_plan()
            if plan:
                return memory_hook(plan), None
            kind = "last_thread"  # nothing recent to ask about
        if kind == "finding":
            # Claiming counts as a mention, so the usual gap and daily limit apply.
            try:
                mention = await asyncio.to_thread(claim_mention_now)
            except Exception as error:
                self.activity.write(
                    f"Greeting finding check failed ({type(error).__name__})", "Greeting finding unavailable",
                )
                mention = None
            if mention is not None:
                return finding_hook(mention.interest_title, mention.finding), mention
            kind = "last_thread"  # nothing fresh to share
        if kind == "last_thread":
            summary = await self.conversation_persistence.load_last_summary()
            if summary:
                return last_thread_hook(summary), None
        return None, None

    def signal_catching_up(self) -> bool:
        """Whether Signal may still be delivering, or answering, messages that waited while Cora was closed."""
        if self.signal_startup is not None and not self.signal_startup.is_finished:
            return True  # its settings aren't even loaded yet
        if not self.signal_settings.enabled or self.signal.status.state in (SIGNAL_FAILED, SIGNAL_NOT_LINKED, SIGNAL_OFF):
            return False
        if self.signal.status.state != SIGNAL_LINKED:
            return True  # still starting or linking
        now = asyncio.get_running_loop().time()
        listener = self.signal_listener
        if listener is None or listener.connected_at is None or now - listener.connected_at < SIGNAL_QUIET_SECONDS:
            return True
        if self.signal_last_message_at is not None and now - self.signal_last_message_at < SIGNAL_QUIET_SECONDS:
            return True
        person = self.signal_settings.allowed_number
        answering = self.signal_answering.get(person)
        return bool(self.signal_waiting.get(person)) or (answering is not None and not answering.is_finished)

    async def wait_for_signal_catch_up(self) -> None:
        """At launch, let messages that waited on Signal be answered first, so the greeting can follow on."""
        deadline = asyncio.get_running_loop().time() + SIGNAL_GREETING_WAIT_SECONDS
        while self.signal_catching_up() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.2)

    async def release_greeting_finding(self, mention) -> None:
        if mention is None:
            return
        try:
            await asyncio.to_thread(release_mention_now, mention)
        except Exception as error:
            self.activity.write(
                f"Greeting finding release failed ({type(error).__name__})", "Greeting finding failed",
            )

    @work(group="greeting", exclusive=True)
    async def send_greeting(self, kind: str) -> None:
        mention = None
        if kind == "launch":
            last_talked_at = self.conversation_persistence.last_talked_at
            relationship = await self.conversation_persistence.load_relationship_values()
            await self.wait_for_signal_catch_up()
            if self.signal_launch_exchanges:
                # What they just said on Signal beats any other hook.
                hook = signal_hook(self.signal_launch_exchanges)
            else:
                hook, mention = await self.choose_launch_hook(last_talked_at, relationship)
            # The launch greeting stays pending while waiting, so typing or speaking still cancels it.
            if self.pending_greeting != "launch":
                self.activity.write("Opening greeting skipped: the person spoke first", "Greeting skipped")
                await self.release_greeting_finding(mention)
                return
            self.pending_greeting = None
            brief = greeting_brief(
                self.live.person_name, last_talked_at, datetime.now().astimezone(), hook, relationship,
            )
            send, sent = self.live.send_greeting(brief), "Opening greeting sent"
        else:
            send, sent = self.live.send_welcome_back(), "Welcome back sent"
        try:
            await asyncio.wait_for(send, timeout=5)
        except Exception as error:
            self.activity.write(f"Greeting send failed ({type(error).__name__})", "Greeting skipped")
            await self.release_greeting_finding(mention)
        else:
            self.activity.write(sent, "Greeting")

    def cancel_standby_alert(self, reason: str = "", *, cancel_send: bool = True) -> None:
        self.inactivity.cancel_alert(reason, cancel_send=cancel_send)

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
        self.confirmations.decline_all()
        self.stop_signal_receiver()
        self.services.stop_all()
        self.event_bus.close()
        self.attention.stop()
        self.observation_timer.stop()
        self.interest_timer.stop()
        self.identity_timer.stop()
        self.exploration_timer.stop()
        self.finding_timer.stop()
        self.cancel_standby_alert()
        self.wake.stop_timers()
        await self.stop_wake_listener()
        self.audio_status.stop()
        if self.idle_standby_timer is not None:
            self.idle_standby_timer.stop()
        self.stop_requested_standby()
        self.stop_conversation_end_timer()

    # Textual owns worker scheduling and cancellation; coordinators own the work.
    def on_chat_composer_submitted(self, event: ChatComposer.Submitted) -> None:
        self.submission.submitted(event)

    @work(group="keyboard-submit")
    async def submit_keyboard_message(
        self, message: str, message_id: UUID, composer: ChatComposer, image_png: bytes | None = None
    ) -> None:
        await self.submission.submit_keyboard_message(message, message_id, composer, image_png)

    def record_chat_message(self, role: str, text: str, channel: str) -> None:
        self.conversation_persistence.record_chat_message(role, text, channel)

    @work(group="chat-save")
    async def save_chat_message(self, message_id: UUID, role: str, text: str, channel: str) -> None:
        await self.conversation_persistence.save_chat_message(message_id, role, text, channel)

    @work
    async def connect_to_cora(self) -> None:
        self.set_connection_state("connecting")

        try:
            if not self.startup_complete:
                self.startup_worker = self.check_interrupted_conversations()
                await self.startup_worker.wait()
                self.startup_complete = True
                self.summary_worker = self.summarize_previous_conversations()
                await self.wait_for_services()
                # Signal starts on its own time; it never holds up Live.
                self.signal_startup = self.start_signal_if_enabled()
                self.daily_backup()
            else:
                # Shutdown waits on startup_worker, so reuse it for later context reloads.
                self.startup_worker = self.load_session_context()
                await self.startup_worker.wait()
            if self.closing:
                return
            # This connect sends Cora's tools as they are now.
            self.service_tools_changed = False
            await self.live.run(
                self.handle_live_event,
                self.handle_mic_status,
                self.handle_audio_status,
            )
        except Exception as error:
            self.set_connection_state("disconnected", "Connection failed. See Activity for details.")
            self.activity.write(f"Connection error: {error}", "Connection failed")
        else:
            self.set_connection_state("disconnected")
            self.activity.write("Connection closed", "Disconnected")
        finally:
            self.report_interrupted_work()
            self.conversation.flush_transcript()
            if self.wake.reconnecting:
                self.wake.reconnecting = False
                self.wake.failed = True
                if self.wake.connection_timer is not None:
                    self.wake.connection_timer.stop()
                await self.stop_wake_listener()
                self.activity.write("Wake connection ended; buffered audio discarded.", "Wake cancelled")

    def report_interrupted_work(self) -> None:
        self.cancel_standby_alert()
        if self.backend_work.pending:
            self.activity.write(
                "Live ended with unfinished backend work; it will not be replayed.", "Work interrupted",
            )
            self.notify("Live ended with unfinished work. Reconnect will not retry it.", severity="warning")
        self.backend_work.clear()

    def action_test_observation(self) -> None:
        self.queue_observation(
            "Simulated camera observation: the person just got a signal message. "
            "Their identity is unknown.", source="test",
        )

    def queue_observation(self, observation: str, *, source: str = "app") -> None:
        self.event_bus.publish(ObservationEvent(source=source, content=observation))

    def handle_event_bus_error(self, error: Exception) -> None:
        self.activity.write(
            f"Event subscriber failed ({type(error).__name__}); not retried.", "Event bus error",
        )

    def can_deliver_observation(self) -> bool:
        # The sender's own transition blocker must not block its readiness recheck.
        return (
            self.is_mounted and not (self.closing or self.reconnecting or self.standby)
            and self.live.ready
            and not any(name != "observation send" for name in self.live_transition_blockers())
        )

    def check_observations(self) -> None:
        self.attention.check()

    @work(group="observation-send")
    async def send_next_observation(self) -> None:
        await self.attention.deliver_next()

    def observation_delivery_finished(self) -> None:
        # Give Live a short chance to react; transcript/audio events extend this gap.
        self.last_live_activity = monotonic()
        self.record_idle_activity()

    def action_reconnect(self) -> None:
        self.record_idle_activity(user_activity=True)
        self.request_live_transition(restart=True)

    def action_standby(self) -> None:
        if self.live.ready and not self.standby:
            self.request_live_transition(restart=False)

    def request_standby(self) -> None:
        """Cora asked for standby: enter it once her sign-off and other work finish."""
        self.requested_standby_deadline = monotonic() + REQUESTED_STANDBY_TIMEOUT_SECONDS
        if self.requested_standby_timer is None:
            self.requested_standby_timer = self.set_interval(0.5, self.check_requested_standby)

    def stop_requested_standby(self) -> None:
        if self.requested_standby_timer is not None:
            self.requested_standby_timer.stop()
            self.requested_standby_timer = None

    def check_requested_standby(self) -> None:
        if self.closing or self.reconnecting or self.standby or not self.live.ready:
            self.stop_requested_standby()
        elif monotonic() >= self.requested_standby_deadline:
            self.stop_requested_standby()
            self.activity.write("Requested standby timed out; staying active.", "Standby cancelled")
        elif not self.live_transition_blockers():
            self.stop_requested_standby()
            self.action_standby()

    def live_transition_blockers(self) -> list[str]:
        """Share exactly the same safety checks for manual and automatic standby."""
        connecting = (
            self.connection_worker is not None and not self.connection_worker.is_finished
            and not self.live.ready
        )
        keyboard_busy = self.keyboard_worker is not None and not self.keyboard_worker.is_finished
        observation_busy = self.attention.busy
        tools_busy = any(
            worker.group == "tool-actions" and not worker.is_finished for worker in self.workers
        )
        speaker = self.live.speaker
        playback_busy = speaker is not None and speaker.is_playback_busy()
        return [name for name, busy in (
            ("connection setup", connecting),
            ("typed submission", keyboard_busy),
            ("observation send", observation_busy),
            ("tool execution", tools_busy),
            ("backend response", bool(self.backend_work.pending)),
            ("typed acknowledgement", bool(self.live.pending_text_requests)),
            ("audio playback", playback_busy),
            ("recent speech", self.live.ready and monotonic() - self.last_live_activity < 2.0),
            ("standby heads-up", self.inactivity.alert is not None),
        ) if busy]

    def request_live_transition(self, restart: bool, *, from_wake: bool = False) -> bool:
        if self.closing or self.reconnecting:
            return False
        blockers = self.live_transition_blockers()
        self.live.trace(
            "transition.request", restart=restart, blockers=blockers,
            playback_busy="audio playback" in blockers,
        )
        if blockers:
            action = "reconnect" if restart else "enter standby"
            reason = ", ".join(blockers)
            self.activity.write(f"Cannot {action}: {reason}.", "Live busy")
            self.notify(
                f"Wait for speech and pending work to finish, then {action}. Waiting for: {reason}.",
                severity="warning",
            )
            return False
        self.reconnecting = True
        self.wake.reconnecting = from_wake
        if not from_wake:
            self.wake.failed = False
            self.wake.response_deadline = None
        self.query_one("#chat-input", ChatComposer).disabled = True
        # Privacy mute remains available while opening speech is being buffered.
        self.query_one("#speaker-status", Button).disabled = True
        self.set_connection_state("connecting" if restart and self.standby else "disconnecting")
        self.reconnect_worker = self.reconnect_live(restart=restart)
        # The worker may finish before this returns, so report the request, not self.reconnecting.
        return True

    @work(group="live-reconnect")
    async def reconnect_live(self, restart: bool = True) -> None:
        try:
            if not self.wake.reconnecting:
                await self.stop_wake_listener()
            self.set_connection_state("connecting" if restart and self.standby else "disconnecting")
            await self.live.stop_microphone()
            # Closing/cleanup must finish before another reader can own the transport.
            await self.live.close()
            if self.connection_worker is not None:
                await self.connection_worker.wait()
            if self.closing:
                return
            self.conversation.flush_transcript()
            await self.conversation_persistence.wait_for_saves()
            if self.closing:
                return
            self.live.conversation_history = self.conversation.get_reconnect_history()
            self.last_live_activity = 0.0
            self.stop_conversation_end_timer()
            if not restart:
                if self.live.connection is not None or not self.live.session_close_confirmed:
                    raise RuntimeError("Live close was not confirmed")
                self.standby = True
                self.handle_mic_status(False)
                self.set_connection_state("standby")
                self.activity.write("Live closed; local app and chat remain open", "Standby")
                self.conversation_end_timer = self.set_timer(
                    CONVERSATION_END_STANDBY_SECONDS, self.end_standby_conversation,
                )
                return
            self.standby = False
            self.activity.write("Restarting Live with recent chat context", "Reconnecting")
            self.connection_worker = self.connect_to_cora()
        except Exception as error:
            if self.wake.reconnecting:
                self.wake.reconnecting = False
                self.wake.failed = True
                await self.stop_wake_listener()
            summary = "Reconnect failed" if restart else "Standby failed"
            self.activity.write(f"{summary} ({type(error).__name__})", summary)
            if self.live.connection is None:
                self.standby = False
                self.set_connection_state("disconnected", f"{summary}. See Activity for details.")
            self.notify(f"{summary}. Check Activity; no requests were replayed.", severity="error")
        finally:
            self.reconnecting = False
            if self.standby and not self.closing:
                self.set_connection_state("standby")
                self.start_wake_listener()
            self.query_one("#chat-input", ChatComposer).disabled = self.closing
            self.query_one("#mic-status", Button).disabled = self.closing
            self.query_one("#speaker-status", Button).disabled = self.closing
            if not self.closing:
                self.query_one("#chat-input", ChatComposer).focus()

    def stop_conversation_end_timer(self) -> None:
        if self.conversation_end_timer is not None:
            self.conversation_end_timer.stop()
            self.conversation_end_timer = None

    @work(group="conversation-end")
    async def end_standby_conversation(self) -> None:
        """A long standby ends the conversation, so its memories and summary form now."""
        self.conversation_end_timer = None
        if not self.standby or self.closing or self.reconnecting:
            return
        if await self.conversation_persistence.end_conversation() and not self.closing:
            self.process_finished_conversations()

    @work(group="database-startup")
    async def check_interrupted_conversations(self) -> None:
        await self.conversation_persistence.check_interrupted_conversations()

    @work(group="database-startup")
    async def load_session_context(self) -> None:
        await self.conversation_persistence.load_session_context()

    @work(group="model-settings", exclusive=True)
    async def open_model_settings(self) -> None:
        try:
            models = await self.conversation_persistence.call_database(self.persistence.load_models)
            enabled = await self.conversation_persistence.call_database(self.persistence.load_enabled_services)
            self.service_tool_choices = await self.conversation_persistence.call_database(
                self.persistence.load_service_tools,
            )
            self.signal_settings = await self.conversation_persistence.call_database(
                self.persistence.load_signal_settings,
            )
            self.signal_clients = {
                client.number: client
                for client in await self.conversation_persistence.call_database(self.persistence.load_clients)
            }
            has_token ={service_id for service_id in SERVICES if await asyncio.to_thread(get_token, service_id)}
        except Exception as error:
            self.activity.write(f"Model settings load failed ({type(error).__name__}).", "Model settings error")
            self.notify("Could not load model settings. Check Activity.", severity="error")
            return
        if not self.closing:
            self.push_screen(ModelSettingsScreen(
                models, self.save_model_setting, self.screen_looking, self.set_screen_looking,
                services=ServiceControls(
                    status=self.services.status, set_enabled=self.set_service_enabled,
                    save_token=self.save_service_token, forget_token=self.forget_service_token,
                    set_tool_enabled=self.set_service_tool, enabled=set(enabled), has_token=has_token,
                    tools_on={service_id: set(tools) for service_id, tools in self.service_tool_choices.items()},
                ),
                signal=SignalControls(
                    status=lambda: self.signal.status, set_enabled=self.set_signal_enabled,
                    save_number=self.save_signal_number, link=self.link_signal,
                    clients=lambda: sorted(self.signal_clients.values(), key=lambda client: client.name.lower()),
                    add_client=self.add_signal_client, remove_client=self.remove_signal_client,
                    enabled=self.signal_settings.enabled, person_number=self.signal_settings.allowed_number,
                ),
            ), callback=self.settings_closed)

    @work(thread=True, group="backup", exit_on_error=False)
    def daily_backup(self) -> None:
        """At launch, back the database up if the newest backup is a day old (see backup.py)."""
        outcome = self.back_up_if_due()
        if outcome is not None:
            self.call_from_thread(self.activity.write, *outcome)

    def back_up_if_due(self) -> tuple[str, str] | None:
        """Back up the database if due; returns what to say in Activity, or None if nothing was due."""
        try:
            if not daily_backup_due(datetime.now()):
                return None
            made, copied = run_backup(include_signal=False)
        except BackupError as error:
            # Docker's own reason, which never contains her data.
            return f"Daily backup failed: {error}", "Backup failed"
        except Exception as error:
            return f"Daily backup failed ({type(error).__name__}).", "Backup failed"
        where = f"; copied to {copied}" if copied else ""
        return f"Backup saved: {made[0].name}{where}", "Backup saved"

    @work(group="signal")
    async def start_signal_if_enabled(self) -> None:
        try:
            self.signal_settings = await self.conversation_persistence.call_database(
                self.persistence.load_signal_settings,
            )
        except Exception as error:
            self.activity.write(f"Signal settings load failed ({type(error).__name__}).", "Signal error")
            return
        if self.signal_settings.enabled and not self.closing:
            await self.load_signal_clients()
            await self.signal.start(self.signal_settings.account)
            await self.listen_on_signal()

    async def load_signal_clients(self) -> None:
        try:
            clients = await self.conversation_persistence.call_database(self.persistence.load_clients)
        except Exception as error:
            self.activity.write(f"Signal people load failed ({type(error).__name__}).", "Signal error")
            return
        self.signal_clients = {client.number: client for client in clients}

    @work(group="signal-save")
    async def add_signal_client(self, name: str, number: str) -> None:
        try:
            number = normalise_number(number)
            if number == self.signal_settings.allowed_number:
                raise ValueError("That's your own number: you're the person she works with, not a client")
            if number == self.signal.status.account or number == self.signal_settings.account:
                raise ValueError("That's Cora's own number")
            client = await self.conversation_persistence.call_database(self.persistence.add_client, name, number)
        except ValueError as error:
            self.notify(str(error), severity="warning")
            return
        except Exception as error:
            self.activity.write(f"Adding a Signal person failed ({type(error).__name__}).", "Signal error")
            self.notify("Could not add them. Check Activity.", severity="error")
            return
        self.signal_clients[client.number] = client
        # Someone added after getting the stranger reply is answered from now on.
        self.signal_introduced.discard(client.number)
        self.activity.write(f"{client.name} added: Cora may talk with them on Signal.", "Signal person added")
        # Added either way (they may join later), but say now if messages can't reach them.
        if self.signal.status.state == SIGNAL_LINKED and (problem := await self.signal_contact_unreachable(client)):
            self.notify(f"{client.name}'s number isn't on Signal, or is hidden. Check the number.", severity="warning")
            self.activity.write(problem, "Signal number not found")
        await self.learn_signal_ids()

    @work(group="signal-save")
    async def remove_signal_client(self, number: str) -> None:
        client = self.signal_clients.get(number)
        if client is None:
            return
        try:
            await self.conversation_persistence.call_database(self.persistence.remove_client, client.id)
        except Exception as error:
            self.activity.write(f"Removing a Signal person failed ({type(error).__name__}).", "Signal error")
            self.notify("Could not remove them. Check Activity.", severity="error")
            return
        self.signal_clients.pop(number, None)
        self.activity.write(f"{client.name} removed; their past chats are kept.", "Signal person removed")

    @work(group="signal")
    async def set_signal_enabled(self, enabled: bool) -> None:
        try:
            await self.conversation_persistence.call_database(self.persistence.save_signal_enabled, enabled)
        except Exception as error:
            self.activity.write(f"Signal setting save failed ({type(error).__name__}).", "Signal error")
            self.notify("Could not save the Signal setting. Check Activity.", severity="error")
            return
        self.signal_settings = replace(self.signal_settings, enabled=enabled)
        if enabled:
            await self.signal.start(self.signal_settings.account)
        else:
            await self.signal.stop()
            self.activity.write("Signal is off; Cora doesn't use it.", "Signal off")
        await self.listen_on_signal()

    @work(group="signal-save")
    async def save_signal_number(self, number: str) -> None:
        try:
            await self.conversation_persistence.call_database(self.persistence.save_signal_allowed_number, number)
        except ValueError as error:
            self.notify(str(error), severity="warning")
            return
        except Exception as error:
            self.activity.write(f"Your Signal number save failed ({type(error).__name__}).", "Signal error")
            self.notify("Could not save your number. Check Activity.", severity="error")
            return
        if number != self.signal_settings.allowed_number:
            # A new number may be a new account: its ID is learned again.
            self.signal_settings = replace(self.signal_settings, allowed_number=number, allowed_signal_id=None)
        self.activity.write("Your number saved: Cora answers you as herself on Signal.", "Signal number saved")
        await self.learn_signal_ids()

    async def save_signal_account(self, account: str | None) -> None:
        await self.conversation_persistence.call_database(self.persistence.save_signal_account, account)
        self.signal_settings = replace(self.signal_settings, account=account)

    @work(group="signal-link")
    async def link_signal(self) -> None:
        if not self.signal_settings.allowed_number:
            self.notify("Save your own number first.", severity="warning")
            return
        await self.signal.link(self.signal_settings.allowed_number)
        await self.listen_on_signal()

    def send_without_live(self, message: str, image_seen: str | None = None) -> None:
        """In Standby, answer a typed message in writing without waking Live."""
        # Taken now, before the message itself joins the chat, so it isn't sent twice.
        history = self.conversation.get_reconnect_history()
        self.keep_conversation_open()
        self.answer_without_live(typed_content(message, image_seen), history)

    def keep_conversation_open(self) -> None:
        """Talking in writing keeps the conversation going: in Standby it ends only after a long quiet."""
        if self.conversation_end_timer is not None:
            self.stop_conversation_end_timer()
            self.conversation_end_timer = self.set_timer(
                CONVERSATION_END_STANDBY_SECONDS, self.end_standby_conversation,
            )

    async def reply_in_writing(
        self, content: list[dict], history: list[dict], route: BackendRoute, extra_instructions: str = "",
    ) -> str:
        """One turn of a chat without Live, as the same Cora: raises if it fails."""
        # Reloaded each time, as at a Live connect, so she is the same Cora with what she
        # knows now: name, memories, interests, identity, relationship and chosen model.
        await self.conversation_persistence.load_session_context()
        sections = instruction_sections(
            self.live.memory_context, interest_context=self.live.interest_context,
            person_name=self.live.person_name, identity_context=self.live.identity_context,
            self_image_context=self.live.self_image_context,
            relationship_context=self.live.relationship_context,
        )
        return await self.backend_chat.reply(
            content=content, history=history, sections=sections, model=self.live.delegation_model,
            route=route, extra_instructions=extra_instructions,
        )

    @work(group="text-chat")
    async def answer_without_live(self, content: list[dict], history: list[dict]) -> None:
        self.activity.write("Answering in writing without waking Live.", "Writing a reply")
        try:
            text = await self.reply_in_writing(content, history, BackendRoute(excluded=STANDBY_EXCLUDED))
        except Exception as error:
            # The type only: a message could echo the conversation.
            self.activity.write(f"Written reply failed ({type(error).__name__}).", "Written reply failed")
            self.notify("Cora couldn't reply in writing. Check Activity.", severity="error")
            return
        if self.closing:
            return
        if text:
            self.conversation.show_written_reply(text)
            self.activity.write(f"Written reply shown: characters={len(text)}", "Written reply")
        else:
            self.activity.write("The written reply was empty.", "Written reply empty")

    async def listen_on_signal(self) -> None:
        """Learn known people's Signal IDs first: messages that waited arrive the moment listening starts."""
        await self.learn_signal_ids()
        if self.signal.status.state == SIGNAL_LINKED and (self.signal_receiver is None or self.signal_receiver.is_finished):
            # Nothing is being answered yet, so any attachment still stored is left over: none are kept.
            try:
                await clear_attachments()
            except Exception as error:
                self.activity.write(f"Clearing stored Signal attachments failed ({type(error).__name__}).", "Signal error")
        self.sync_signal_receiver()

    def sync_signal_receiver(self) -> None:
        """Listen while Cora's Signal account is linked, and not otherwise."""
        linked = self.signal.status.state == SIGNAL_LINKED and not self.closing
        running = self.signal_receiver is not None and not self.signal_receiver.is_finished
        account = self.signal.status.account
        if running and (not linked or self.signal_listening_as != account):
            self.signal_receiver.cancel()
            self.signal_receiver = None
            running = False
        if linked and not running:
            self.signal_listening_as = account
            receiver = SignalReceiver(self.signal.client, account, self.on_signal_message, self.activity.write)
            self.signal_listener = receiver
            # The method, not a coroutine: nothing is left un-awaited if it's cancelled before it starts.
            self.signal_receiver = self.run_worker(receiver.run, group="signal-receive", exit_on_error=False)
            self.activity.write("Listening for your Signal messages to Cora.", "Signal listening")

    def stop_signal_receiver(self) -> None:
        if self.signal_receiver is not None:
            self.signal_receiver.cancel()
            self.signal_receiver = None

    def on_signal_message(self, message: IncomingMessage) -> None:
        self.signal_last_message_at = asyncio.get_running_loop().time()
        # A first message may carry only their Signal ID until the request is accepted; known people's IDs are looked up.
        sender = message.sender or self.signal_ids.get(message.sender_id or "", "")
        known = sender and (sender == self.signal_settings.allowed_number or sender in self.signal_clients)
        if not known:
            # A stranger's attachments are never opened, and not kept either.
            self.discard_signal_attachments([message])
            self.answer_stranger(sender or message.sender_id)
            return
        if not message.sender:
            # Someone she knows wrote first: accept their request so her replies show normally.
            message = replace(message, sender=sender)
            self.accept_signal_request(sender)
        # She's waiting for the person's answer to a request: only their own next message answers it.
        pending = self.signal_approvals.get(sender)
        if pending is not None and not pending.done():
            answer = approval_answer(message.text)
            pending.set_result(answer is True)
            if answer is not None:
                self.discard_signal_attachments([message])
                self.conversation.show_signal_message(signal_message_text(message))
                return
            # Not a clear yes or no: the request is declined, and this is answered as a message.
        elif message.sender_id and message.sender_id not in self.signal_ids:
            # Number and ID together: saved, for when they hide their number later.
            self.run_worker(self.remember_signal_id(sender, message.sender_id), group="signal-ids")
        # One queue per sender: each is answered in order, and different people at the same time.
        self.signal_waiting.setdefault(sender, []).append(message)
        worker = self.signal_answering.get(sender)
        if worker is None or worker.is_finished:
            self.signal_answering[sender] = self.run_worker(
                self.answer_signal(sender), group="signal-chat", exit_on_error=False,
            )

    async def answer_signal(self, sender: str) -> None:
        """Answer one sender's messages in order; ones that arrive meanwhile are answered together next."""
        while self.signal_waiting.get(sender) and not self.closing:
            batch = self.signal_waiting.pop(sender)
            account = self.signal.status.account
            if not account:
                self.discard_signal_attachments(batch)
                return
            client = self.signal_clients.get(sender)
            if sender == self.signal_settings.allowed_number:
                batch = await self.read_signal_images(account, self.signal_settings.person_recipient, batch)
                await self.answer_primary_on_signal(account, self.signal_settings.person_recipient, batch)
            elif client is not None:
                batch = await self.read_signal_images(account, client.recipient, batch)
                await self.answer_client_on_signal(account, client, batch)
            else:
                self.discard_signal_attachments(batch)

    async def read_signal_images(self, account: str, recipient: str, batch: list[IncomingMessage]) -> list[IncomingMessage]:
        """Images from people she knows are read like pasted ones; then every attachment is deleted from signal-cli."""
        if not any(message.attachments for message in batch):
            return batch
        try:
            await self.signal.client.send_typing(account, recipient)
        except Exception:
            pass  # only a courtesy, while the images are read
        read = []
        try:
            for message in batch:
                seen, failed = await read_images(message, lambda attachment_id: self.signal.client.attachment(account, attachment_id))
                if seen:
                    self.activity.write(f"Read {len(seen)} Signal image(s) for Cora.", "Image read")
                if failed:
                    # Not why: errors may echo the request.
                    self.activity.write(f"{failed} Signal image(s) couldn't be read; noted as not opened.", "Image look failed")
                read.append(replace(message, images_seen=seen))
        finally:
            await self.delete_signal_attachments(batch)
        return read

    def discard_signal_attachments(self, messages: list[IncomingMessage]) -> None:
        if any(message.attachments for message in messages):
            self.run_worker(self.delete_signal_attachments(messages), group="signal-attachments", exit_on_error=False)

    async def delete_signal_attachments(self, messages: list[IncomingMessage]) -> None:
        ids = [attachment.id for message in messages for attachment in message.attachments]
        if not ids:
            return
        try:
            await delete_attachments(ids)
        except Exception as error:
            # Cleared at the next launch instead.
            self.activity.write(f"Deleting Signal attachments failed ({type(error).__name__}).", "Signal error")

    async def send_signal_reply(self, account: str, recipient: str, write) -> str | None:
        """Show typing, write a reply and send it; on failure send a short apology and return None."""
        try:
            await self.signal.client.send_typing(account, recipient)
        except Exception:
            pass  # only a courtesy
        try:
            text = await write()
            if not text:
                raise ValueError("empty reply")
            await self.signal.client.send(account, recipient, text)
            return text
        except Exception as error:
            self.activity.write(f"Signal reply failed ({signal_failure(error)}).", "Signal reply failed")
            try:
                await self.signal.client.send(account, recipient, SIGNAL_SORRY)
            except Exception:
                pass
            return None

    async def answer_primary_on_signal(self, account: str, person: str, batch: list[IncomingMessage]) -> None:
        """The person's own messages: the same conversation as the desktop, shown in its chat."""
        # Taken before these messages join the chat, so they aren't sent twice.
        history = self.conversation.get_reconnect_history()
        for message in batch:
            self.conversation.show_signal_message(signal_message_text(message))
        self.keep_conversation_open()
        self.activity.write(f"Answering {len(batch)} Signal message(s) from you.", "Signal reply")
        # Tools that change things are asked about in this chat, answered only by the person's reply.
        route = BackendRoute(
            excluded=SIGNAL_EXCLUDED,
            ask_approval=self.signal_approval_asker(account, person, self.signal_settings.allowed_number),
            approval_seconds=APPROVAL_SECONDS, tool_seconds=APPROVAL_SECONDS + 60,
        )
        text = await self.send_signal_reply(account, person, lambda: self.reply_in_writing(
            signal_content(batch, datetime.now(timezone.utc)), history, route, SIGNAL_CHAT_INSTRUCTIONS,
        ))
        if text is None or self.closing:
            return
        self.conversation.show_written_reply(text, label="Cora · Signal", channel="signal")
        self.activity.write(f"Signal reply sent: characters={len(text)}", "Signal reply sent")
        said = "\n".join(signal_message_text(message) for message in batch)
        if self.pending_greeting == "launch":
            # Answered before she says hello, so the hello can follow on from it.
            self.signal_launch_exchanges.append((said, text))
        try:
            await self.live.note_signal_exchange(said, text)
        except Exception as error:
            self.activity.write(f"Signal note to voice Cora failed ({type(error).__name__}).", "Signal note failed")

    def signal_approval_asker(self, account: str, recipient: str, number: str):
        """Ask the person in their Signal chat; the answer is their own next message (see on_signal_message)."""
        async def ask(request) -> bool:
            future = asyncio.get_running_loop().create_future()
            self.signal_approvals[number] = future
            try:
                text = approval_request_text(request)
                await self.signal.client.send(account, recipient, text)
                self.conversation.show_written_reply(text, label="Cora · Signal", channel="signal")
                return await future
            finally:
                if self.signal_approvals.get(number) is future:
                    del self.signal_approvals[number]
        return ask

    async def answer_client_on_signal(self, account: str, client: Client, batch: list[IncomingMessage]) -> None:
        """Someone the person added: their own conversation, kept out of the person's chat."""
        try:
            conversation_id = await self.conversation_persistence.call_database(
                self.persistence.client_conversation, client.id,
            )
            history = await self.conversation_persistence.call_database(
                self.persistence.load_client_history, conversation_id,
            )
            for message in batch:
                await self.conversation_persistence.call_database(
                    self.persistence.save_client_message, conversation_id, "user", signal_message_text(message),
                )
        except Exception as error:
            self.activity.write(f"Saving {client.name}'s Signal message failed ({type(error).__name__}).",
                                "Signal reply failed")
            return
        self.activity.write(f"Answering {client.name} on Signal.", "Signal client")
        text = await self.send_signal_reply(
            account, client.recipient, lambda: self.reply_to_client(client, batch, history),
        )
        if text is None or self.closing:
            return
        try:
            await self.conversation_persistence.call_database(
                self.persistence.save_client_message, conversation_id, "assistant", text,
            )
        except Exception as error:
            self.activity.write(f"Saving the reply to {client.name} failed ({type(error).__name__}).", "Signal error")
        self.activity.write(f"Answered {client.name} on Signal.", "Signal client")

    async def reply_to_client(self, client: Client, batch: list[IncomingMessage], history: list[dict]) -> str:
        """Professional mode: her own identity and interests and what she remembers of them, none of the person's."""
        await self.conversation_persistence.load_session_context()
        try:
            # What she remembers from their earlier chats: theirs only, never the person's.
            remembered = await self.conversation_persistence.call_database(
                self.persistence.load_client_memory_context, client.id, client.name,
            )
        except Exception as error:
            self.activity.write(f"{client.name}'s memories didn't load ({type(error).__name__}).", "Signal client")
            remembered = ""
        sections = instruction_sections(
            remembered, interest_context=self.live.interest_context, identity_context=self.live.identity_context,
            self_image_context=self.live.self_image_context,
        )
        return await self.backend_chat.reply(
            content=signal_content(batch, datetime.now(timezone.utc)), history=history, sections=sections,
            model=self.live.delegation_model,
            # Web search, memory search limited to what they may know, and passing things on to the
            # person; no services, nothing at the desk.
            route=BackendRoute(
                excluded=frozenset(self.tools.names()) - {"search_memory", *CLIENT_ONLY},
                memory_scope=(client.id, client.name),
            ),
            extra_instructions=client_chat_instructions(client.name, self.live.person_name),
        )

    def find_signal_contact(self, name: str) -> tuple[Client | None, str | None]:
        """Someone the person added, by name (or first name if only one matches), for Cora to message."""
        if self.signal.status.state != SIGNAL_LINKED:
            return None, "Signal isn't set up and linked, so nothing can be sent."
        contacts = list(self.signal_clients.values())
        if not contacts:
            return None, "Nobody has been added for you to message yet (Settings > Services > Signal)."
        wanted = " ".join(name.split()).casefold()
        matches = [contact for contact in contacts if contact.name.casefold() == wanted] or [
            contact for contact in contacts if contact.name.casefold().split()[0] == wanted.split()[0]
        ]
        if len(matches) == 1:
            return matches[0], None
        names = ", ".join(sorted(contact.name for contact in contacts))
        if matches:
            return None, f"More than one contact matches; ask which one: {names}."
        return None, f"There's no contact by that name. You can message: {names}."

    async def signal_contact_unreachable(self, contact: Client) -> str | None:
        """Why Signal can't deliver to them, or None. A failed check doesn't block: the send will say."""
        if contact.signal_id:
            return None  # known by ID, which works even if they hide their number
        try:
            if await self.signal.client.is_registered(self.signal.status.account, contact.number):
                return None
        except Exception:
            return None
        return (
            f"{contact.name}'s number isn't on Signal, or they've set Signal to hide their number, so "
            "nothing can be sent. Ask the person to check the number in Settings, or ask "
            f"{contact.name} to allow being found by number."
        )

    async def relay_to_person(self, client_id, client_name: str, text: str) -> str | None:
        """From a client's chat to the person, marked as theirs; None once sent, else why not."""
        account, person = self.signal.status.account, self.signal_settings.person_recipient
        if not account or not person:
            return "Signal isn't set up to reach them right now."
        today = datetime.now().date()
        key = (client_id, today)
        if self.signal_relays.get(key, 0) >= SIGNAL_RELAYS_PER_DAY:
            return f"You've passed on as much as you can for {client_name} today; it wasn't sent."
        message = f"From {client_name}, via Cora:\n{text}"
        await self.signal.client.send(account, person, message)
        self.signal_relays[key] = self.signal_relays.get(key, 0) + 1
        # In the person's own chat and conversation, so voice Cora knows too.
        self.conversation.show_written_reply(message, label="Cora · Signal", channel="signal")
        self.activity.write(f"Passed something on from {client_name} to you on Signal.", "Signal relay")
        try:
            await self.live.note_relay(client_name, text)
        except Exception as error:
            self.activity.write(f"Relay note to voice Cora failed ({type(error).__name__}).", "Signal note failed")
        return None

    async def send_to_signal_contact(self, contact: Client, text: str) -> None:
        """Send from Cora's number and save it in that person's conversation, so she sees it when they reply."""
        account = self.signal.status.account
        if not account:
            raise RuntimeError("Signal is not linked")
        await self.signal.client.send(account, contact.recipient, text)
        try:
            conversation_id = await self.conversation_persistence.call_database(
                self.persistence.client_conversation, contact.id,
            )
            await self.conversation_persistence.call_database(
                self.persistence.save_client_message, conversation_id, "assistant", text,
            )
        except Exception as error:
            # Sent all the same; only her record of it is missing.
            self.activity.write(f"Saving the message to {contact.name} failed ({type(error).__name__}).", "Signal error")

    @work(group="signal-accept")
    async def accept_signal_request(self, number: str) -> None:
        if number in self.signal_accepted or not self.signal.status.account:
            return
        self.signal_accepted.add(number)
        try:
            await self.signal.client.accept_request(self.signal.status.account, number)
            self.activity.write("Accepted a Signal message request from someone you added.", "Signal request accepted")
        except Exception as error:
            self.signal_accepted.discard(number)
            self.activity.write(f"Accepting a Signal request failed ({signal_failure(error)}).", "Signal error")

    async def learn_signal_ids(self) -> None:
        """Know the Signal IDs of the person and everyone they added, so their messages are recognised.

        Saved IDs come first: they keep working when someone hides their number. Anyone without one
        is looked up by number, and what's found is saved.
        """
        person = self.signal_settings
        if person.allowed_number and person.allowed_signal_id:
            self.signal_ids[person.allowed_signal_id] = person.allowed_number
        for client in self.signal_clients.values():
            if client.signal_id:
                self.signal_ids[client.signal_id] = client.number
        account = self.signal.status.account
        if self.signal.status.state != SIGNAL_LINKED or not account:
            return
        for number in filter(None, [person.allowed_number, *self.signal_clients]):
            if number in self.signal_ids.values():
                continue
            try:
                signal_id = await self.signal.client.signal_id(account, number)
            except Exception:
                continue  # looked up again next time
            if signal_id:
                await self.remember_signal_id(number, signal_id)

    async def remember_signal_id(self, number: str, signal_id: str) -> None:
        """Save someone's Signal ID, so they're still recognised if they later hide their number."""
        self.signal_ids[signal_id] = number
        try:
            if number == self.signal_settings.allowed_number:
                await self.conversation_persistence.call_database(self.persistence.save_signal_person_id, signal_id)
                self.signal_settings = replace(self.signal_settings, allowed_signal_id=signal_id)
            elif (client := self.signal_clients.get(number)) is not None:
                await self.conversation_persistence.call_database(
                    self.persistence.save_client_signal_id, client.id, signal_id,
                )
                self.signal_clients[number] = replace(client, signal_id=signal_id)
        except Exception as error:
            self.activity.write(f"Saving a Signal ID failed ({type(error).__name__}).", "Signal error")

    @work(group="signal-stranger")
    async def answer_stranger(self, recipient: str) -> None:
        """Someone nobody added gets one polite reply per run, and never a conversation.

        `recipient` is their number, or their Signal ID when Signal hasn't shared the number. Their
        message request is left unaccepted.
        """
        if not recipient or recipient in self.signal_introduced or not self.signal.status.account:
            return
        self.signal_introduced.add(recipient)
        self.activity.write("A Signal message from someone not added got the polite reply.", "Signal stranger")
        try:
            await self.signal.client.send(self.signal.status.account, recipient, stranger_reply(self.live.person_name))
        except Exception as error:
            self.activity.write(f"Signal reply failed ({signal_failure(error)}).", "Signal reply failed")

    def show_confirmation(self, request) -> None:
        self.push_screen(ConfirmScreen(request, self.confirmations.answer))
        self.tell_cora_approval_waiting(request.title, request.tool)

    @work(group="approval-notes")
    async def tell_cora_approval_waiting(self, service: str, tool: str, reminder: bool = False) -> None:
        """Let Cora know her request waits on the pop-up, so she can ask the person to click it."""
        try:
            await self.live.send_approval_waiting(service, tool, reminder=reminder)
        except Exception as error:
            # The pop-up still works without her mentioning it.
            self.activity.write(f"Approval note to Cora failed ({type(error).__name__}).", "Approval note failed")

    def close_confirmation(self, request) -> None:
        """A request ended without the person answering (timed out or closing): take its pop-up away."""
        for screen in self.screen_stack:
            if isinstance(screen, ConfirmScreen) and screen.request is request:
                screen.dismiss()

    def settings_closed(self, _result=None) -> None:
        # Tool changes reach Cora at a connect, so reconnect once now rather than after every switch.
        if self.service_tools_changed and self.live.ready and not self.standby and not self.closing:
            self.activity.write("Reconnecting so Cora has the service tools you changed.", "Service tools changed")
            self.action_reconnect()

    @work(group="services-save")
    async def set_service_tool(self, service_id: str, tool: str, enabled: bool) -> None:
        title = SERVICES[service_id].title
        try:
            await self.conversation_persistence.call_database(
                self.persistence.save_service_tool, service_id, tool, enabled,
            )
        except Exception as error:
            self.activity.write(f"{title} tool setting save failed ({type(error).__name__}).", "Services error")
            self.notify(f"Could not save the {title} tool setting. Check Activity.", severity="error")
            return
        chosen = self.service_tool_choices.setdefault(service_id, set())
        if enabled:
            chosen.add(tool)
        else:
            chosen.discard(tool)
        self.activity.write(
            f"{title} tool {tool} {'on' if enabled else 'off'}; Cora gets the change at her next connect.",
            f"{title} · {tool} {'on' if enabled else 'off'}",
        )
        self.sync_service_tools()

    async def wait_for_services(self) -> None:
        """At launch, give services a short while to connect so their tools are in the first session.

        A slow service joins at the next connect; this never stops Cora connecting.
        """
        try:
            await self.start_enabled_services().wait()
            await self.services.wait_settled(SERVICE_STARTUP_WAIT_SECONDS)
        except Exception as error:
            self.activity.write(f"Services startup failed ({type(error).__name__}).", "Services error")

    def sync_service_tools(self) -> None:
        """Register each connected service's switched-on tools as Cora's; the next connect sends them."""
        wanted = {}
        for service_id in SERVICES:
            status = self.services.status(service_id)
            if status.state != CONNECTED:
                continue
            chosen = self.service_tool_choices.get(service_id, set())
            for tool in status.tools:
                schema = tool_schema(service_id, tool) if tool.name in chosen else None
                if schema is not None:
                    wanted[schema["name"]] = (service_id, tool, schema)
        current = {
            name for name in self.tools.names()
            if any(name.startswith(tool_name(service_id, "")) for service_id in SERVICES)
        }
        for name in current - set(wanted):
            self.tools.unregister(name)
        for name in set(wanted) - current:
            service_id, tool, schema = wanted[name]
            handler = ServiceToolHandler(
                service_id, tool.name, self.services.call_tool, self.tool_connection,
                lambda: self.closing or self.reconnecting, self.activity.write,
                cache=self.service_results,
                paged=PAGE_ARGUMENT not in tool.input_schema.get("properties", {}),
                # Anything the server doesn't mark read-only asks the person first.
                confirm=None if tool.read_only else self.confirmations.ask,
            )
            self.tools.register(schema, validate_service_arguments, handler.handle)
        self.confirming_tools = frozenset(name for name, (_, tool, _) in wanted.items() if not tool.read_only)
        if current != set(wanted):
            self.service_tools_changed = True

    @work(group="services-startup")
    async def start_enabled_services(self) -> None:
        """At launch, connect each service left switched on."""
        try:
            enabled = await self.conversation_persistence.call_database(self.persistence.load_enabled_services)
            self.service_tool_choices = await self.conversation_persistence.call_database(
                self.persistence.load_service_tools,
            )
            tokens = {service_id: await asyncio.to_thread(get_token, service_id) for service_id in enabled}
        except Exception as error:
            self.activity.write(f"Services load failed ({type(error).__name__}).", "Services error")
            return
        for service_id in sorted(enabled):
            if self.closing:
                return
            if tokens[service_id]:
                self.services.start(service_id, tokens[service_id])
            else:
                title = SERVICES[service_id].title
                self.activity.write(f"{title} is switched on but has no saved token.", "Services error")

    @work(group="services-save")
    async def set_service_enabled(self, service_id: str, enabled: bool) -> None:
        title = SERVICES[service_id].title
        try:
            await self.conversation_persistence.call_database(
                self.persistence.save_service_enabled, service_id, enabled,
            )
        except Exception as error:
            self.activity.write(f"{title} setting save failed ({type(error).__name__}).", "Services error")
            self.notify(f"Could not save the {title} setting. Check Activity.", severity="error")
            return
        if not enabled:
            self.services.stop(service_id)
            return
        try:
            token = await asyncio.to_thread(get_token, service_id)
        except Exception as error:
            self.activity.write(f"{title} token load failed ({type(error).__name__}).", "Services error")
            return
        if token and not self.closing:
            self.services.start(service_id, token)

    @work(group="services-save")
    async def save_service_token(self, service_id: str, token: str) -> None:
        title = SERVICES[service_id].title
        try:
            await asyncio.to_thread(save_token, service_id, token)
        except Exception as error:
            self.activity.write(f"{title} token save failed ({type(error).__name__}).", "Services error")
            self.notify(f"Could not save the {title} token. Check Activity.", severity="error")
            return
        self.activity.write(f"{title} token saved to the system credential store.", f"{title} token saved")
        # A connection already running, or failed with the old token, retries with the new one.
        if service_id in self.services.connections and not self.closing:
            self.services.start(service_id, token)

    @work(group="services-save")
    async def forget_service_token(self, service_id: str) -> None:
        title = SERVICES[service_id].title
        try:
            await asyncio.to_thread(forget_token, service_id)
        except Exception as error:
            self.activity.write(f"{title} token removal failed ({type(error).__name__}).", "Services error")
            return
        self.activity.write(f"{title} token removed from the system credential store.", f"{title} token removed")

    def set_screen_looking(self, enabled: bool) -> None:
        self.screen_looking = enabled
        self.activity.write(
            "Cora can look at the screen." if enabled else "Cora cannot look at the screen.",
            "Screen looking on" if enabled else "Screen looking off",
        )

    @work(group="model-settings-save")
    async def save_model_setting(self, area: str, model: str) -> None:
        try:
            await self.conversation_persistence.call_database(self.persistence.save_model, area, model)
        except Exception as error:
            self.activity.write(f"Model setting save failed ({type(error).__name__}).", "Model settings error")
            self.notify("Could not save the model setting. Check Activity.", severity="error")
            return
        self.activity.write(f"The {area} model is now {model}.", "Model settings saved")
        # Live's session config holds the delegation model; restart Live so it applies now.
        # In standby or while disconnected, the next connect loads it anyway.
        if area == "delegation" and self.live.ready and not self.standby:
            self.action_reconnect()

    def process_finished_conversations(self) -> None:
        # Single flight: a pass already running leaves this one for the next standby or launch.
        if self.summary_worker is None or self.summary_worker.is_finished:
            self.summary_worker = self.summarize_previous_conversations()

    @work(thread=True, group="memory-summary", exit_on_error=False)
    def summarize_previous_conversations(self) -> None:
        worker = get_current_worker()
        self.conversation_persistence.summarize_previous_conversations(
            should_stop=lambda: self.closing or worker.is_cancelled,
            dispatch_activity=lambda message, summary: self.call_from_thread(
                self.activity.write, message, summary,
            ),
        )

    def check_interests(self) -> None:
        # Single flight: a slow model call simply delays the next check.
        if self.closing or (self.interest_worker is not None and not self.interest_worker.is_finished):
            return
        self.interest_worker = self.evolve_interests()

    @work(thread=True, group="interests", exit_on_error=False)
    def evolve_interests(self) -> None:
        worker = get_current_worker()
        outcome = run_interest_cycle(should_stop=lambda: self.closing or worker.is_cancelled)
        # Repeated outcomes (e.g. the same failure every check) are written once.
        if outcome is not None and outcome != self.interest_outcome:
            self.interest_outcome = outcome
            self.call_from_thread(self.activity.write, outcome, "Interests")

    def check_identity(self) -> None:
        if self.closing or (self.identity_worker is not None and not self.identity_worker.is_finished):
            return
        self.identity_worker = self.reflect_identity()

    @work(thread=True, group="identity", exit_on_error=False)
    def reflect_identity(self) -> None:
        worker = get_current_worker()
        outcome = run_identity_cycle(should_stop=lambda: self.closing or worker.is_cancelled)
        if outcome is not None and outcome != self.identity_outcome:
            self.identity_outcome = outcome
            self.call_from_thread(self.activity.write, outcome, "Identity")

    def can_explore(self) -> bool:
        if self.closing or self.reconnecting:
            return False
        if self.standby:
            return True
        return (
            self.live.ready and not self.live_transition_blockers()
            and monotonic() - self.inactivity.last_activity >= EXPLORE_IDLE_SECONDS
        )

    def check_exploration(self) -> None:
        if not self.can_explore() or (
            self.exploration_worker is not None and not self.exploration_worker.is_finished
        ):
            return
        self.exploration_worker = self.explore_interest()

    @work(thread=True, group="exploration", exit_on_error=False)
    def explore_interest(self) -> None:
        worker = get_current_worker()
        outcome = run_exploration_cycle(should_stop=lambda: self.closing or worker.is_cancelled)
        if outcome is not None and outcome != self.exploration_outcome:
            self.exploration_outcome = outcome
            self.call_from_thread(self.activity.write, outcome, "Exploration")

    def can_offer_finding(self) -> bool:
        # Only into a quiet, connected conversation: never wakes Live from standby.
        return (
            self.can_deliver_observation() and not self.live_transition_blockers()
            and not self.attention.pending
            and monotonic() - self.inactivity.last_activity >= FINDING_IDLE_SECONDS
        )

    def check_findings(self) -> None:
        if self.can_offer_finding():
            self.event_bus.publish(FindingEvent(source="interests"))

    def handle_live_event(self, event) -> None:
        if event.type in (
            "session.input_transcript.delta", "session.output_transcript.delta",
        ) and event.delta:
            self.last_live_activity = monotonic()
            user_activity = event.type == "session.input_transcript.delta"
            if user_activity:
                user_activity = self.wake.has_request(event.delta)
            self.record_idle_activity(user_activity=user_activity)
            self.inactivity.record_output()
        elif (
            event.type == "session.output_audio.delta" and self.live.speaker is not None
            and not self.live.speaker_muted and self.live.speaker.is_playback_busy()
        ):
            # Output audio is a continuous stream: silent chunks are not speech.
            self.last_live_activity = monotonic()
            self.record_idle_activity()
            self.inactivity.record_output()

        self.live_events.handle(event)

    def handle_live_session_event(self, event) -> None:
        if event.type == "session.started":
            self.wake.session_started()
            self.record_idle_activity()
            self.inactivity.was_busy = False
            self.set_connection_state("active")
            self.activity.write("Live session started", "Connected")
            kind = self.pending_greeting
            # An unknown last talk must not become a stranger introduction.
            if kind == "launch" and self.conversation_persistence.last_talked_loaded:
                self.send_greeting(kind)  # clears pending_greeting once it is sent
            else:
                self.pending_greeting = None
                if kind == "welcome_back":
                    self.send_greeting(kind)

        elif event.type == "session.commentary.appended":
            self.inactivity.acknowledge(event.client_event_id)

        elif event.type == "error":
            # A rejected command does not necessarily end the Live session.
            self.activity.write(f"API error: {event.error.message}", "API error")
            command_id = getattr(event.error, "client_event_id", None)
            if self.inactivity.alert is not None and command_id == self.inactivity.alert.event_id:
                self.cancel_standby_alert("Heads-up rejected; staying active.")
            if command_id:
                if command_id.startswith("typed_response_") and not self.live.pending_text_requests:
                    self.backend_work.finish_typed_request()
                    self.record_idle_activity()
                self.activity.write(f"Rejected command: id={command_id}", "API error")

        elif event.type == "session.closed":
            self.report_interrupted_work()
            self.set_connection_state("disconnected")
            self.conversation.flush_transcript()
            self.activity.write(f"Session ended: {event.reason}", "Disconnected")

    def on_collapsible_expanded(self, event: Collapsible.Expanded) -> None:
        if event.collapsible.id == "activity-panel":
            # Wait for layout to restore the hidden log's usable width.
            self.call_after_refresh(self.activity.redraw)

    def set_connection_state(self, state: str, detail: str = "") -> None:
        labels = {
            "disconnected": "○ Disconnected",
            "standby": "○ Standby",
            "connecting": "◌ Connecting…",
            "active": "● Active",
            "disconnecting": "◌ Disconnecting…",
        }
        button = self.query_one("#chat-status", Button)
        button.label = labels[state]
        for name in labels:
            button.set_class(name == state, f"state-{name}")
        button.tooltip = detail or {
            "active": "Click to close Live and enter standby.",
            "standby": "Live is closed. Click to resume, or press Ctrl+R.",
            "disconnected": "Live is disconnected. Ctrl+R to reconnect.",
        }.get(state, f"Live is {state}.")
        button.disabled = state not in ("active", "standby") or self.closing or self.reconnecting

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
                    self.conversation_persistence.load_dashboard_snapshot, self.activity,
                    self.live.context,
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

    def update_audio_controls(self) -> None:
        self.audio_status.update_controls()

    def voice_bands(self) -> tuple[tuple[float, ...], tuple[float, ...]]:
        """Cora's playback and the person's microphone loudness bands, for the dashboard."""
        speaker = self.live.speaker
        playing = speaker.get_playback_bands() if speaker is not None and not self.live.speaker_muted else ()
        return playing, self.live.get_microphone_bands()

    def update_speaking_animation(self) -> None:
        self.audio_status.update_speaking_animation()

    def reset_mic_indicator(self) -> None:
        self.audio_status.reset_mic_indicator()

    def action_copy_chat_selection(self) -> None:
        text = self.screen.get_selected_text()
        if not text:
            # Let the focused input handle its own normal copy shortcut.
            raise SkipAction()
        try:
            if sys.platform == "win32":
                # VS Code's terminal may not support Textual's OSC 52 clipboard.
                pyperclip.copy(text)
            else:
                self.copy_to_clipboard(text)
        except pyperclip.PyperclipException as error:
            self.activity.write(f"Clipboard error: {error}", "Clipboard error")
        else:
            self.notify("Selected text copied", timeout=2)

    def handle_audio_status(self, working: bool, message: str) -> None:
        if not working:
            self.cancel_standby_alert("Audio unavailable; staying active.")
        self.activity.write(
            message,
            "Audio playback ready" if working else "Audio playback error",
        )

    def handle_mic_status(self, working: bool, message: str = "") -> None:
        self.audio_status.handle_mic_status(working)

        if message:
            self.activity.write(
                f"[mic] {message}",
                "Microphone active" if working else "Microphone stopped",
            )

    async def action_disconnect_and_quit(self) -> None:
        if self.closing:
            return
        self.closing = True
        # Nothing still waiting for approval may run while shutting down.
        self.confirmations.decline_all()
        self.stop_signal_receiver()
        self.event_bus.close()
        self.attention.stop()
        self.observation_timer.stop()
        self.interest_timer.stop()
        self.identity_timer.stop()
        self.exploration_timer.stop()
        self.finding_timer.stop()
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