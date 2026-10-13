import asyncio
import sys
from collections import deque
from uuid import UUID, uuid4

import pyperclip
from textual.actions import SkipAction
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, HorizontalGroup
from textual.widgets import Collapsible, RichLog, Static, Button
from textual.worker import WorkerError, get_current_worker

from dotenv import load_dotenv
from textual import work

from ui.activity import Activity
from ui.chat_widgets import SelectableRichLog
from ui.composer import ChatComposer
from ui.conversation import Conversation
from storage.conversation_store import ConversationStore
from storage.database import build_engine
from storage.keyboard_persistence import KeyboardPersistence
from memory.store import MemoryStore
from live import CoraLive
from memory.extract_memories import extract_memories
from memory.embed_memory import embed_memory
from memory.deduplicate_memory import deduplicate_memory
from memory.summarize import summarize_conversation
from tools import create_tool_registry


load_dotenv()

class CoraApp(App):

    CSS_PATH = "cora.tcss"
    BINDINGS = [
        ("ctrl+x", "disconnect_and_quit", "Quit"),
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
                    tooltip="Live is disconnected. Standby controls are not available yet.",
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
        self.tools = create_tool_registry(
            get_connection=lambda: self.live.connection,
            is_closing=lambda: self.closing,
            shutdown=lambda: self.action_disconnect_and_quit(),
            write_debug=self.activity.write,
            schedule=lambda action: self.run_worker(action, group="tool-actions"),
        )
        self.live = CoraLive(tool_schemas=self.tools.schemas)
        self.speaker_level_indicator = self.query_one("#speaker-level", Static)
        self.speaker_levels = deque("────────", maxlen=8)
        self.speaker_level_timer = self.set_interval(0.05, self.update_speaking_animation)
        self.connection_worker = self.connect_to_cora()

    def on_unmount(self) -> None:
        self.speaker_level_timer.stop()

    def on_chat_composer_submitted(self, event: ChatComposer.Submitted) -> None:
        message = event.text.strip()
        composer = event.composer

        if not message or composer.disabled or self.closing:
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
                await self.live.send_text(message)
            except Exception as error:
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
            composer.disabled = self.closing
            if not self.closing:
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
            self.startup_worker = self.check_interrupted_conversations()
            await self.startup_worker.wait()
            if self.closing:
                return
            self.summary_worker = self.summarize_previous_conversations()
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
            self.conversation.flush_transcript()

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

        if event.type == "session.started":
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

        elif event.type == "session.delegation.created":
            delegation = event.delegation
            self.activity.write(
                f"Delegation requested: "
                f"target={delegation.target}, id={delegation.id}",
                "Delegation requested",
            )

        elif event.type == "response.event":
            # Live wraps backend events in a dictionary, not an event object.
            backend_event = event.event
            event_type = backend_event.get("type")
            if event_type == "response.output_item.done":
                # Live lifecycle snapshots omit output; completed items carry it.
                item = backend_event.get("item") or {}
                if item.get("type") == "function_call":
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
                    f"response_id={response.get('id')}",
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
            if command_id:
                self.activity.write(f"Rejected command: id={command_id}", "API error")

        elif event.type == "session.closed":
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
            "connecting": "◌ Connecting…",
            "active": "● Active",
            "disconnecting": "◌ Disconnecting…",
        }
        button = self.query_one("#chat-status", Button)
        button.label = labels[state]
        for name in labels:
            button.set_class(name == state, f"state-{name}")
        button.tooltip = detail or (
            f"Live is {state}. Standby controls are not available yet."
        )
        # The eventual standby/resume control needs safe session reconnection first.
        button.disabled = True

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close-button":
            event.button.disabled = True
            await self.action_disconnect_and_quit()
        elif event.button.id in ("mic-status", "speaker-status") and not self.closing:
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
        self.speaker_level_timer.stop()
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
    CoraApp().run()