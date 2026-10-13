import asyncio
from math import isfinite
from os import getenv
import sys
from collections import deque
from dataclasses import dataclass
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
from ui.chat_widgets import SelectableRichLog
from ui.composer import ChatComposer
from ui.conversation import Conversation
from storage.conversation_store import ConversationStore
from storage.database import build_engine
from storage.keyboard_persistence import KeyboardPersistence
from storage.live_trace import LiveTrace
from memory.store import MemoryStore
from live import CoraLive
from memory.extract_memories import extract_memories
from memory.embed_memory import embed_memory
from memory.deduplicate_memory import deduplicate_memory
from memory.summarize import summarize_conversation
from tools import create_tool_registry


load_dotenv()


def read_idle_timeout() -> float:
    """Seconds of inactivity before standby; zero disables the timer."""
    value = float(getenv("CORA_IDLE_TIMEOUT_SECONDS", "300"))
    if not isfinite(value) or value < 0:
        raise ValueError("Idle timeout must be finite and nonnegative")
    return value


@dataclass
class StandbyAlert:
    event_id: str
    started_at: float
    accepted: bool = False
    last_output_at: float | None = None
    quiet_since: float | None = None


class CoraApp(App):

    live_trace = None
    CSS_PATH = "cora.tcss"
    BINDINGS = [
        ("ctrl+x", "disconnect_and_quit", "Quit"),
        ("ctrl+r", "reconnect", "Reconnect"),
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
        self.mic_idle_timer = None
        self.mic_working = False
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
        self.pending_backend_work = set()
        self.tool_response_ids = set()
        self.backend_response_ids = {}
        self.last_live_activity = 0.0
        self.last_idle_activity = monotonic()
        self.idle_was_busy = False
        self.idle_standby_timer = None
        self.standby_farewell_enabled = getenv("CORA_STANDBY_FAREWELL", "0").strip() == "1"
        self.standby_alert = None
        self.standby_alert_worker = None
        try:
            self.idle_timeout_seconds = read_idle_timeout()
        except ValueError:
            self.idle_timeout_seconds = 0.0
            self.activity.write(
                "Invalid CORA_IDLE_TIMEOUT_SECONDS; automatic standby disabled. Use finite seconds >= 0.",
                "Idle timeout configuration error",
            )
        self.tools = create_tool_registry(
            get_connection=lambda: self.live.connection,
            is_closing=lambda: self.closing or self.reconnecting,
            shutdown=lambda: self.action_disconnect_and_quit(),
            write_debug=self.activity.write,
            schedule=lambda action: self.run_worker(action, group="tool-actions"),
        )
        self.live = CoraLive(tool_schemas=self.tools.schemas, on_trace=self.trace_live)
        self.speaker_level_indicator = self.query_one("#speaker-level", Static)
        self.speaker_levels = deque("────────", maxlen=8)
        self.speaker_level_timer = self.set_interval(0.05, self.update_speaking_animation)
        if self.live_trace is not None:
            self.trace_live("app.started")
            if not self.live_trace.failed:
                self.activity.write(
                    f"Diagnostic trace: {self.live_trace.path.name} (IDs/state only; no chat text or audio).",
                    "Diagnostic trace enabled",
                )
        self.connection_worker = self.connect_to_cora()
        if self.idle_timeout_seconds > 0:
            self.idle_standby_timer = self.set_interval(1.0, self.check_inactivity)

    def record_idle_activity(self, *, user_activity: bool = False) -> None:
        self.last_idle_activity = monotonic()
        if user_activity:
            self.cancel_standby_alert("User activity; staying active.")

    def on_key(self, event: events.Key) -> None:
        self.record_idle_activity(user_activity=True)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if isinstance(event.text_area, ChatComposer):
            self.record_idle_activity(user_activity=True)

    def on_text_area_selection_changed(self, event: TextArea.SelectionChanged) -> None:
        if isinstance(event.text_area, ChatComposer):
            self.record_idle_activity(user_activity=True)

    def check_inactivity(self) -> None:
        if self.standby_alert is not None:
            self.check_standby_alert()
            return
        if self.idle_timeout_seconds <= 0 or self.closing or self.reconnecting or self.standby or not self.live.ready:
            return
        if self.live_transition_blockers():
            # No time spent doing work or playing speech counts toward idleness.
            self.record_idle_activity()
            self.idle_was_busy = True
            return
        if self.idle_was_busy:
            self.record_idle_activity()
            self.idle_was_busy = False
            return
        if monotonic() - self.last_idle_activity >= self.idle_timeout_seconds:
            if self.standby_farewell_enabled and not self.live.speaker_muted:
                alert = StandbyAlert(f"standby_alert_{uuid4().hex}", monotonic())
                self.standby_alert = alert
                self.activity.write("Standby heads-up; interact to stay active.", "Standby heads-up")
                self.standby_alert_worker = self.send_standby_alert(alert)
                return
            self.activity.write("Idle timeout reached; entering standby.", "Automatic standby")
            self.action_standby()

    @work(group="standby-alert")
    async def send_standby_alert(self, alert: StandbyAlert) -> None:
        try:
            await asyncio.wait_for(self.live.send_standby_alert(alert.event_id), timeout=5)
        except Exception as error:
            if self.standby_alert is alert:
                self.cancel_standby_alert(
                    f"Heads-up send failed ({type(error).__name__}); staying active.", cancel_send=False,
                )

    def cancel_standby_alert(self, reason: str = "", *, cancel_send: bool = True) -> None:
        if self.standby_alert is None:
            return
        self.standby_alert = None
        if cancel_send and self.standby_alert_worker is not None and not self.standby_alert_worker.is_finished:
            self.standby_alert_worker.cancel()
        self.record_idle_activity()
        self.idle_was_busy = False
        if reason:
            self.activity.write(reason, "Standby cancelled")

    def check_standby_alert(self) -> None:
        alert = self.standby_alert
        if alert is None:
            return
        if self.closing or self.reconnecting or self.standby or not self.live.ready or self.idle_timeout_seconds <= 0:
            self.cancel_standby_alert()
            return
        blockers = self.live_transition_blockers()
        if any(name not in ("standby heads-up", "audio playback", "recent speech") for name in blockers):
            self.cancel_standby_alert("New work; staying active.")
            return
        now = monotonic()
        if now - alert.started_at >= 30:
            # No authoritative speech-done event exists. Never close on timeout.
            self.cancel_standby_alert("Heads-up wait timed out; staying active.")
            return
        if not alert.accepted or alert.last_output_at is None:
            return
        if "audio playback" in blockers or now - alert.last_output_at < 2:
            alert.quiet_since = None
            return
        if alert.quiet_since is None:
            alert.quiet_since = now
            return
        if now - alert.quiet_since >= 3:
            self.standby_alert = None
            self.activity.write("Heads-up finished; entering standby.", "Automatic standby")
            self.action_standby()

    def trace_live(self, stage: str, **metadata) -> None:
        trace = self.live_trace
        if trace is None or trace.failed:
            return
        trace.write(
            stage, pending_backend_work=sorted(self.pending_backend_work, key=str),
            tool_response_ids=sorted(self.tool_response_ids, key=str), **metadata,
        )
        if trace.failed:
            self.activity.write("Could not write diagnostic trace; Live continues unchanged.", "Diagnostic trace unavailable")

    def on_unmount(self) -> None:
        self.cancel_standby_alert()
        self.speaker_level_timer.stop()
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
                self.pending_backend_work.add("typed-request")
                self.trace_live("typed.submit", message_id=str(message_id))
                await self.live.send_text(message)
                self.trace_live("typed.sent", message_id=str(message_id))
            except Exception as error:
                self.pending_backend_work.discard("typed-request")
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

    def report_interrupted_work(self) -> None:
        self.cancel_standby_alert()
        if self.pending_backend_work:
            self.activity.write(
                "Live ended with unfinished backend work; it will not be replayed.", "Work interrupted",
            )
            self.notify("Live ended with unfinished work. Reconnect will not retry it.", severity="warning")
        self.pending_backend_work.clear()
        self.tool_response_ids.clear()
        self.backend_response_ids.clear()

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
        tools_busy = any(
            worker.group == "tool-actions" and not worker.is_finished for worker in self.workers
        )
        speaker = self.live.speaker
        playback_busy = speaker is not None and speaker.is_playback_busy()
        return [name for name, busy in (
            ("connection setup", connecting),
            ("typed submission", keyboard_busy),
            ("tool execution", tools_busy),
            ("backend response", bool(self.pending_backend_work)),
            ("typed acknowledgement", bool(self.live.pending_text_requests)),
            ("audio playback", playback_busy),
            ("recent speech", self.live.ready and monotonic() - self.last_live_activity < 2.0),
            ("standby heads-up", self.standby_alert is not None),
        ) if busy]

    def request_live_transition(self, restart: bool) -> None:
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
        self.query_one("#chat-input", ChatComposer).disabled = True
        self.query_one("#mic-status", Button).disabled = True
        self.query_one("#speaker-status", Button).disabled = True
        self.set_connection_state("connecting" if restart and self.standby else "disconnecting")
        self.reconnect_worker = self.reconnect_live(restart=restart)

    @work(group="live-reconnect")
    async def reconnect_live(self, restart: bool = True) -> None:
        try:
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
        # Own a separate engine so an in-flight request can finish during shutdown.
        engine = None
        worker = get_current_worker()
        try:
            if self.closing or worker.is_cancelled:
                return
            engine = build_engine()
            store = ConversationStore(engine)
            for conversation_id in store.get_unsummarized_conversation_ids(self.persistence.conversation_id):
                if self.closing or worker.is_cancelled:
                    break
                memories = extract_memories(store, conversation_id)
                if self.closing or worker.is_cancelled:
                    break
                self.call_from_thread(
                    self.activity.write,
                    f"Memories extracted: conversation_id={conversation_id}, count={len(memories)}",
                    "Memory extraction complete",
                )
                # Save the summary last: even zero-memory chats then leave this queue.
                summarize_conversation(store, conversation_id)
                if not self.closing and not worker.is_cancelled:
                    self.call_from_thread(
                        self.activity.write,
                        f"Summary saved: conversation_id={conversation_id}", "Memory summary saved",
                    )
            if self.closing or worker.is_cancelled:
                return
            memory_store = MemoryStore(engine)
            for memory_id in memory_store.get_pending_embedding_ids():
                if self.closing or worker.is_cancelled:
                    return
                embed_memory(memory_store, memory_id)
                if not self.closing and not worker.is_cancelled:
                    self.call_from_thread(
                        self.activity.write, f"Memory embedded: memory_id={memory_id}", "Memory embedding saved",
                    )
            for memory_id in memory_store.get_pending_deduplication_ids():
                if self.closing or worker.is_cancelled:
                    return
                duplicate = deduplicate_memory(memory_store, memory_id)
                if not self.closing and not worker.is_cancelled:
                    self.call_from_thread(
                        self.activity.write,
                        f"Memory checked: memory_id={memory_id}, duplicate={duplicate}", "Memory deduplication checked",
                    )
        except Exception as error:
            if not self.closing and not worker.is_cancelled:
                self.call_from_thread(
                    self.activity.write,
                    f"Background memory pass stopped ({type(error).__name__}).", "Memory processing failed",
                )
        finally:
            if engine is not None:
                engine.dispose()

    def handle_live_event(self, event) -> None:
        mic_status = self.query_one("#mic-status", Button)
        if event.type in (
            "session.input_transcript.delta", "session.output_transcript.delta",
        ) and event.delta:
            self.last_live_activity = monotonic()
            self.record_idle_activity(user_activity=event.type == "session.input_transcript.delta")
            if self.standby_alert is not None and event.delta:
                self.standby_alert.last_output_at = monotonic()
                self.standby_alert.quiet_since = None
        elif (
            event.type == "session.output_audio.delta" and self.live.speaker is not None
            and not self.live.speaker_muted and self.live.speaker.is_playback_busy()
        ):
            # Output audio is a continuous stream: silent chunks are not speech.
            self.last_live_activity = monotonic()
            self.record_idle_activity()
            if self.standby_alert is not None:
                self.standby_alert.last_output_at = monotonic()
                self.standby_alert.quiet_since = None

        if event.type == "session.started":
            self.record_idle_activity()
            self.idle_was_busy = False
            self.set_connection_state("active")
            self.activity.write("Live session started", "Connected")

        elif event.type == "session.input_transcript.delta":
            if not self.live.microphone_muted:
                mic_status.label = "🎙 Speech received"
                mic_status.refresh(layout=True)
                if self.mic_idle_timer is not None:
                    self.mic_idle_timer.stop()
                # Transcript silence is a UI hint, not a confirmed end of speech.
                self.mic_idle_timer = self.set_timer(1.5, self.reset_mic_indicator)
            self.conversation.append_transcript("You", event.delta)

        elif event.type == "session.output_transcript.delta":
            self.conversation.append_transcript("Cora", event.delta)

        elif event.type == "session.thinking.appended":
            command_id = event.client_event_id
            if command_id and command_id.startswith("typed_context_"):
                self.activity.write(
                    f"Live accepted typed context: id={command_id}",
                    "Typed context accepted",
                )

        elif event.type == "session.commentary.appended":
            if self.standby_alert is not None and event.client_event_id == self.standby_alert.event_id:
                self.standby_alert.accepted = True

        elif event.type == "session.delegation.created":
            self.cancel_standby_alert("New backend work; staying active.")
            delegation = event.delegation
            self.pending_backend_work.add(delegation.id)
            response_id = getattr(delegation, "response_id", None)
            if response_id:
                self.backend_response_ids[delegation.id] = response_id
            self.activity.write(
                f"Delegation requested: "
                f"target={delegation.target}, id={delegation.id}",
                "Delegation requested",
            )

        elif event.type == "response.event":
            # Live wraps backend events in a dictionary, not an event object.
            backend_event = event.event
            event_type = backend_event.get("type")
            response_id = (backend_event.get("response") or {}).get("id") or backend_event.get("response_id")
            # Forwarded output items can omit response_id; their delegation
            # identifies the response announced by metadata/lifecycle events.
            if response_id and event.delegation_id is not None:
                self.backend_response_ids[event.delegation_id] = response_id
            elif not response_id:
                response_id = self.backend_response_ids.get(event.delegation_id)
            if event_type in (
                "response.created", "response.in_progress", "response.output_item.done",
                "response.completed", "response.failed", "response.incomplete",
            ):
                if not self.live.pending_text_requests:
                    self.pending_backend_work.discard("typed-request")
            if event_type in ("response.created", "response.in_progress", "response.output_item.done"):
                self.cancel_standby_alert("New backend work; staying active.")
                self.pending_backend_work.add(event.delegation_id)
                self.pending_backend_work.add(response_id or event.delegation_id)
            elif event_type in ("response.completed", "response.failed", "response.incomplete"):
                self.record_idle_activity()
                self.pending_backend_work.discard(response_id or event.delegation_id)
                # A completed response with function calls still needs a tool continuation.
                if event_type != "response.completed" or response_id not in self.tool_response_ids:
                    self.pending_backend_work.discard(event.delegation_id)
                    self.backend_response_ids.pop(event.delegation_id, None)
                self.tool_response_ids.discard(response_id)
            if event_type == "response.output_item.done":
                # Live lifecycle snapshots omit output; completed items carry it.
                item = backend_event.get("item") or {}
                if item.get("type") == "function_call":
                    self.pending_backend_work.add(event.delegation_id)
                    self.tool_response_ids.add(response_id)
                    self.tools.handle_function_call(item)
                else:
                    self.conversation.display_written_item(item)
            elif event_type in (
                "response.created",
                "response.completed",
                "response.failed",
                "response.incomplete",
                "error",
            ):
                response = backend_event.get("response") or {}
                summary = {
                    "response.created": "Luna started",
                    "response.completed": "Luna response complete",
                    "response.failed": "Backend failed",
                    "response.incomplete": "Backend incomplete",
                    "error": "Backend error",
                }[event_type]
                self.activity.write(
                    f"Backend {event_type}: "
                    f"delegation_id={event.delegation_id}, "
                    f"response_id={response_id}, "
                    f"client_event_id={getattr(event, 'client_event_id', None)}, "
                    f"typed_pending={len(self.live.pending_text_requests)}, "
                    f"backend_pending={len(self.pending_backend_work)}",
                    summary,
                )
                error = response.get("error") or backend_event.get("error")
                if error:
                    self.activity.write(f"Backend error: {error}", "Backend error")
                if event_type == "error" and not error:
                    self.activity.write(
                        f"Backend error: {backend_event.get('message')}",
                        "Backend error",
                    )
                details = response.get("incomplete_details")
                if details:
                    self.activity.write(f"Backend incomplete: {details}", "Backend incomplete")
            elif event_type in (
                "response.web_search_call.in_progress",
                "response.web_search_call.searching",
                "response.web_search_call.completed",
            ):
                summary = {
                    "response.web_search_call.in_progress": "Search started",
                    "response.web_search_call.searching": "Searching the web",
                    "response.web_search_call.completed": "Search complete",
                }[event_type]
                self.activity.write(
                    f"Search {event_type}: "
                    f"delegation_id={event.delegation_id}, "
                    f"item_id={backend_event.get('item_id')}",
                    summary,
                )

        elif event.type == "error":
            # A rejected command does not necessarily end the Live session.
            self.activity.write(f"API error: {event.error.message}", "API error")
            command_id = getattr(event.error, "client_event_id", None)
            if self.standby_alert is not None and command_id == self.standby_alert.event_id:
                self.cancel_standby_alert("Heads-up rejected; staying active.")
            if command_id:
                if command_id.startswith("typed_response_") and not self.live.pending_text_requests:
                    self.pending_backend_work.discard("typed-request")
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
        elif event.button.id in ("mic-status", "speaker-status") and not self.closing and not self.reconnecting:
            button = event.button
            button.disabled = True
            try:
                if button.id == "mic-status":
                    await self.live.set_microphone_muted(not self.live.microphone_muted)
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
        mic = self.query_one("#mic-status", Button)
        mic.label = (
            "🎙 Muted" if self.live.microphone_muted
            else "🎙 Listening" if self.mic_working else "🎙× Mic"
        )
        mic.tooltip = "Unmute microphone" if self.live.microphone_muted else "Mute microphone"
        mic.set_class(self.live.microphone_muted, "audio-muted")
        # Button.label repaints but does not invalidate its cached auto width.
        mic.refresh(layout=True)
        speaker = self.query_one("#speaker-status", Button)
        speaker.label = "🔇 Muted" if self.live.speaker_muted else "🔊 Speaker"
        speaker.tooltip = "Unmute speaker" if self.live.speaker_muted else "Mute speaker"
        speaker.set_class(self.live.speaker_muted, "audio-muted")
        speaker.refresh(layout=True)
        self.update_speaking_animation()

    def update_speaking_animation(self) -> None:
        indicator = self.speaker_level_indicator
        if not indicator.is_mounted:
            return
        muted = self.live.speaker_muted
        level = (
            self.live.speaker.get_playback_level()
            if not self.closing and not muted and self.live.speaker is not None else 0.0
        )
        if level > 0:
            # A gentle gain makes ordinary speech visible without changing playback.
            self.speaker_levels.append("▁▂▃▄▅▆▇█"[min(7, int(level ** 0.5 * 8))])
        else:
            self.speaker_levels.clear()
            self.speaker_levels.extend("────────")
        text = "Muted" if muted else "".join(self.speaker_levels)
        if indicator.content != text:
            # This fixed-width display never needs to relayout the conversation.
            indicator.update(text, layout=False)
        indicator.set_class(muted, "audio-muted")

    def reset_mic_indicator(self) -> None:
        self.mic_idle_timer = None
        self.update_audio_controls()

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
        self.mic_working = working

        if self.mic_idle_timer is not None:
            self.mic_idle_timer.stop()
            self.mic_idle_timer = None

        self.update_audio_controls()

        if message:
            self.activity.write(
                f"[mic] {message}",
                "Microphone active" if working else "Microphone stopped",
            )

    async def action_disconnect_and_quit(self) -> None:
        if self.closing:
            return
        self.closing = True
        self.cancel_standby_alert()
        self.speaker_level_timer.stop()
        if self.idle_standby_timer is not None:
            self.idle_standby_timer.stop()
        self.update_speaking_animation()
        self.query_one("#close-button", Button).disabled = True
        self.query_one("#mic-status", Button).disabled = True
        self.query_one("#speaker-status", Button).disabled = True
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