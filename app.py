import asyncio
from os import getenv
import sys
from pathlib import Path
from time import monotonic
from uuid import UUID, uuid4

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
from ui.chat_widgets import SelectableRichLog
from ui.composer import ChatComposer
from ui.conversation import Conversation
from storage.keyboard_persistence import KeyboardPersistence
from storage.live_trace import LiveTrace
from attention import AttentionManager
from backend_work import BackendWorkTracker
from inactivity import InactivityController, StandbyAlert, read_idle_timeout
from live import CoraLive
from live_events import LiveEventHandler
from memory.background import run_background_memory_pass
from tools import create_tool_registry
from wake.controller import WakeController
from wake.listener import WakeListener


load_dotenv()


class CoraApp(App):

    live_trace = None
    CSS_PATH = "cora.tcss"
    BINDINGS = [
        ("ctrl+x", "disconnect_and_quit", "Quit"),
        ("ctrl+r", "reconnect", "Reconnect"),
        Binding("ctrl+t", "test_observation", "Test awareness", priority=True, show=False),
        Binding("ctrl+c", "copy_chat_selection", "Copy selected text", priority=True, show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="chat-panel"):
            with HorizontalGroup(id="window-controls"):
                yield Button("×", id="close-button")
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
        self.query_one("#chat-panel").border_title = "CORA"
        self.query_one("#chat-input", ChatComposer).focus()
        self.activity = Activity(
            self.query_one("#activity-panel", Collapsible),
            self.query_one("#activity-output", RichLog),
        )
        self.persistence = KeyboardPersistence()
        self.database_lock = asyncio.Lock()
        self.chat_workers = []
        self.chat_recording_closed = False
        self.conversation = Conversation(
            self.query_one("#chat-output", RichLog), self.set_timer, self.activity.write,
            record_message=self.record_chat_message,
        )
        self.pending_keyboard_message = None
        self.keyboard_worker = None
        self.startup_worker = None
        self.summary_worker = None
        self.closing = False
        self.reconnecting = False
        self.standby = False
        self.reconnect_worker = None
        self.startup_complete = False
        self.backend_work = BackendWorkTracker()
        self.last_live_activity = 0.0
        self.idle_standby_timer = None
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
        self.tools = create_tool_registry(
            get_connection=lambda: self.live.connection,
            is_closing=lambda: self.closing or self.reconnecting,
            shutdown=lambda: self.action_disconnect_and_quit(),
            write_debug=self.activity.write,
            schedule=lambda action: self.run_worker(action, group="tool-actions"),
        )
        self.live = CoraLive(tool_schemas=self.tools.schemas, on_trace=self.trace_live)
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
        )
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
        if self.inactivity.timeout_seconds > 0:
            self.idle_standby_timer = self.set_interval(1.0, self.check_inactivity)

    def record_idle_activity(self, *, user_activity: bool = False) -> None:
        self.inactivity.record_activity()
        if user_activity:
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
        self.request_live_transition(restart=True, from_wake=True)
        return self.reconnecting

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
        self.attention.stop()
        self.observation_timer.stop()
        self.cancel_standby_alert()
        self.wake.stop_timers()
        await self.stop_wake_listener()
        self.audio_status.stop()
        if self.idle_standby_timer is not None:
            self.idle_standby_timer.stop()

    def on_chat_composer_submitted(self, event: ChatComposer.Submitted) -> None:
        self.record_idle_activity(user_activity=True)
        message = event.text.strip()
        composer = event.composer

        if not message or composer.disabled or self.closing or self.reconnecting:
            return
        if self.standby:
            self.notify("Live is in standby. Click Standby or press Ctrl+R to resume; your draft is kept.", severity="warning")
            return

        composer.disabled = True
        self.conversation.flush_transcript()
        if self.pending_keyboard_message is None or self.pending_keyboard_message[0] != message:
            self.pending_keyboard_message = (message, uuid4())
        message_id = self.pending_keyboard_message[1]
        self.keyboard_worker = self.submit_keyboard_message(message, message_id, composer)

    @work(group="keyboard-submit")
    async def submit_keyboard_message(
        self, message: str, message_id: UUID, composer: ChatComposer
    ) -> None:
        try:
            if self.closing:
                return
            try:
                await self.call_database(self.persistence.save_message, message_id, message)
            except Exception as error:
                # SQL errors may include query parameters: never log private text/credentials.
                self.activity.write(
                    f"Database save failed ({type(error).__name__}). Draft kept; nothing sent.",
                    "Database save failed",
                )
                self.notify("Message not sent: database save failed. Draft kept.", severity="error")
                return
            self.activity.write(
                f"Keyboard message saved: conversation_id={self.persistence.conversation_id}, "
                f"message_id={message_id}",
                "Keyboard message saved",
            )
            if self.closing:
                # Saved text remains pending; shutdown must not start a new send.
                return
            try:
                self.backend_work.start_typed_request()
                self.trace_live("typed.submit", message_id=str(message_id))
                await self.live.send_text(message)
                self.trace_live("typed.sent", message_id=str(message_id))
            except Exception as error:
                self.backend_work.finish_typed_request()
                self.trace_live("typed.failed", message_id=str(message_id), error_type=type(error).__name__)
                # A failed send may have reached the backend partially; do not auto-retry it.
                self.pending_keyboard_message = None
                await self.record_keyboard_delivery(message_id, "failed")
                self.activity.write(f"Text send error: {error}", "Text send error")
                return

            self.pending_keyboard_message = None
            await self.record_keyboard_delivery(message_id, "sent")
            self.conversation.flush_transcript()
            self.conversation.write_log(f"You: {message}")
            self.conversation.write_log("")
            composer.clear()
        finally:
            self.record_idle_activity()
            composer.disabled = self.closing or self.reconnecting
            if not composer.disabled:
                composer.focus()

    async def record_keyboard_delivery(self, message_id: UUID, status: str) -> None:
        try:
            await self.call_database(self.persistence.record_delivery, message_id, status)
        except Exception as error:
            self.activity.write(
                f"Could not confirm database delivery status '{status}' for message_id={message_id} "
                f"({type(error).__name__}). Text was already saved.",
                "Delivery status error",
            )
            self.notify("Text saved, but database delivery status could not be confirmed.", severity="warning")

    async def call_database(self, operation, *args):
        # Serialise off-thread calls, including lazy engine creation and shutdown.
        async with self.database_lock:
            return await asyncio.to_thread(operation, *args)

    def record_chat_message(self, role: str, text: str, channel: str) -> None:
        if self.chat_recording_closed:
            return
        self.chat_workers = [worker for worker in self.chat_workers if not worker.is_finished]
        self.chat_workers.append(self.save_chat_message(uuid4(), role, text, channel))

    @work(group="chat-save")
    async def save_chat_message(self, message_id: UUID, role: str, text: str, channel: str) -> None:
        try:
            await self.call_database(self.persistence.save_chat_message, message_id, role, text, channel)
        except Exception as error:
            self.activity.write(
                f"Chat save failed ({type(error).__name__}); displayed text remains available.",
                "Chat save failed",
            )
            self.notify("Conversation text could not be saved to the database.", severity="warning")
        else:
            self.activity.write(f"Chat text saved: role={role}, channel={channel}", "Chat text saved")

    @work
    async def connect_to_cora(self) -> None:
        self.set_connection_state("connecting")

        try:
            if not self.startup_complete:
                self.startup_worker = self.check_interrupted_conversations()
                await self.startup_worker.wait()
                self.startup_complete = True
                self.summary_worker = self.summarize_previous_conversations()
            if self.closing:
                return
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
            "Simulated camera observation: nick just got a signal message. "
            "Their identity is unknown."
        )

    def queue_observation(self, observation: str) -> None:
        self.attention.submit(observation)

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

    def request_live_transition(self, restart: bool, *, from_wake: bool = False) -> None:
        if self.closing or self.reconnecting:
            return
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
            return
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
            for worker in self.chat_workers:
                await worker.wait()
            if self.closing:
                return
            self.live.conversation_history = self.conversation.get_reconnect_history()
            self.last_live_activity = 0.0
            if not restart:
                if self.live.connection is not None or not self.live.session_close_confirmed:
                    raise RuntimeError("Live close was not confirmed")
                self.standby = True
                self.handle_mic_status(False)
                self.set_connection_state("standby")
                self.activity.write("Live closed; local app and chat remain open", "Standby")
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

    @work(group="database-startup")
    async def check_interrupted_conversations(self) -> None:
        try:
            count = await self.call_database(self.persistence.mark_interrupted_conversations)
        except Exception as error:
            self.activity.write(
                f"Interrupted conversation check failed ({type(error).__name__})",
                "Database startup check failed",
            )
            self.notify("Could not tag unfinished conversations in the database.", severity="warning")
        else:
            if count:
                self.activity.write(
                    f"Tagged {count} unfinished conversation(s) as interrupted.",
                    "Unfinished conversations tagged",
                )

        if self.closing:
            return
        self.live.memory_context = ""
        try:
            context = await self.call_database(self.persistence.load_startup_memory_context)
        except Exception as error:
            self.activity.write(
                f"Startup memory load failed ({type(error).__name__}); connecting without memories.",
                "Memory context unavailable",
            )
            self.notify("Could not load saved memories; connecting without them.", severity="warning")
        else:
            if not self.closing:
                self.live.memory_context = context
                self.activity.write(
                    f"Startup memory context loaded ({len(context)} characters).", "Memory context loaded",
                )

    @work(thread=True, group="memory-summary", exit_on_error=False)
    def summarize_previous_conversations(self) -> None:
        worker = get_current_worker()
        run_background_memory_pass(
            self.persistence.conversation_id,
            should_stop=lambda: self.closing or worker.is_cancelled,
            write_activity=lambda message, summary: self.call_from_thread(
                self.activity.write, message, summary,
            ),
        )

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
        elif event.button.id == "chat-status":
            if self.standby:
                self.action_reconnect()
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
        self.attention.stop()
        self.observation_timer.stop()
        self.cancel_standby_alert()
        self.wake.response_deadline = None
        self.wake.stop_timers()
        await self.stop_wake_listener()
        self.audio_status.stop()
        if self.idle_standby_timer is not None:
            self.idle_standby_timer.stop()
        self.update_speaking_animation()
        self.query_one("#close-button", Button).disabled = True
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
                self.conversation.flush_transcript()
                self.chat_recording_closed = True
                for worker in self.chat_workers:
                    try:
                        await worker.wait()
                    except WorkerError:
                        self.activity.write("Chat save worker stopped", "Chat save worker stopped")
                try:
                    await self.call_database(self.persistence.finish_conversation)
                except Exception as error:
                    self.activity.write(
                        f"Conversation close failed ({type(error).__name__})",
                        "Conversation close failed",
                    )
                    self.notify("Conversation end could not be saved to the database.", severity="warning")
            finally:
                try:
                    await self.call_database(self.persistence.close)
                except Exception as error:
                    self.activity.write(f"Database cleanup failed ({type(error).__name__})", "Database cleanup failed")
                finally:
                    self.exit()

if __name__ == "__main__":
    app = CoraApp()
    app.live_trace = LiveTrace(Path(__file__).with_name("live-trace.log"))
    app.run()