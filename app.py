import asyncio
import sys
from uuid import UUID, uuid4

import pyperclip
from textual.actions import SkipAction
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, HorizontalGroup
from textual.widgets import Collapsible, RichLog, Static, Button
from textual.worker import WorkerError

from dotenv import load_dotenv
from textual import work

from activity import Activity
from chat_widgets import SelectableRichLog
from composer import ChatComposer
from conversation import Conversation
from keyboard_persistence import KeyboardPersistence
from live import CoraLive
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
                yield Static("○ Not connected", id="chat-status")
                yield Static("🎙× Mic", id="mic-status")
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
        self.activity = Activity(
            self.query_one("#activity-panel", Collapsible),
            self.query_one("#activity-output", RichLog),
        )
        self.conversation = Conversation(
            self.query_one("#chat-output", RichLog), self.set_timer, self.activity.write
        )
        self.persistence = KeyboardPersistence()
        self.pending_keyboard_message = None
        self.keyboard_worker = None
        self.closing = False
        self.tools = create_tool_registry(
            get_connection=lambda: self.live.connection,
            is_closing=lambda: self.closing,
            shutdown=lambda: self.action_disconnect_and_quit(),
            write_debug=self.activity.write,
            schedule=lambda action: self.run_worker(action, group="tool-actions"),
        )
        self.live = CoraLive(tool_schemas=self.tools.schemas)
        self.connection_worker = self.connect_to_cora()

    def on_chat_composer_submitted(self, event: ChatComposer.Submitted) -> None:
        message = event.text.strip()
        composer = event.composer

        if not message or composer.disabled or self.closing:
            return

        composer.disabled = True
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
                await asyncio.to_thread(self.persistence.save_message, message_id, message)
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
            await asyncio.to_thread(self.persistence.record_delivery, message_id, status)
        except Exception as error:
            self.activity.write(
                f"Could not confirm database delivery status '{status}' for message_id={message_id} "
                f"({type(error).__name__}). Text was already saved.",
                "Delivery status error",
            )
            self.notify("Text saved, but database delivery status could not be confirmed.", severity="warning")

    @work
    async def connect_to_cora(self) -> None:
        status = self.query_one("#chat-status", Static)

        status.update("○ Connecting...")

        try:
            await self.live.run(
                self.handle_live_event,
                self.handle_mic_status,
                self.handle_audio_status,
            )
        except Exception as error:
            status.update("○ Connection failed")
            self.activity.write(f"Connection error: {error}", "Connection failed")
        else:
            status.update("○ Disconnected")
            self.activity.write("Connection closed", "Disconnected")
        finally:
            self.conversation.flush_transcript()

    def handle_live_event(self, event) -> None:
        status = self.query_one("#chat-status", Static)
        mic_status = self.query_one("#mic-status", Static)

        if event.type == "session.started":
            status.update("● Connected")
            self.activity.write("Live session started", "Connected")

        elif event.type == "session.input_transcript.delta":
            mic_status.update("🎙 Speech received")
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
            status.update("○ API error")
            self.activity.write(f"API error: {event.error.message}", "API error")
            command_id = getattr(event.error, "client_event_id", None)
            if command_id:
                self.activity.write(f"Rejected command: id={command_id}", "API error")

        elif event.type == "session.closed":
            self.conversation.flush_transcript()
            self.activity.write(f"Session ended: {event.reason}", "Disconnected")

    def on_collapsible_expanded(self, event: Collapsible.Expanded) -> None:
        if event.collapsible.id == "activity-panel":
            # Wait for layout to restore the hidden log's usable width.
            self.call_after_refresh(self.activity.redraw)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close-button":
            event.button.disabled = True
            await self.action_disconnect_and_quit()

    def reset_mic_indicator(self) -> None:
        self.mic_idle_timer = None
        self.query_one("#mic-status", Static).update("🎙 Listening")

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
        mic_status = self.query_one("#mic-status", Static)

        if self.mic_idle_timer is not None:
            self.mic_idle_timer.stop()
            self.mic_idle_timer = None

        if working:
            mic_status.update("🎙 Listening")
        else:
            mic_status.update("🎙× Mic")

        if message:
            self.activity.write(
                f"[mic] {message}",
                "Microphone active" if working else "Microphone stopped",
            )

    async def action_disconnect_and_quit(self) -> None:
        if self.closing:
            return
        self.closing = True
        self.query_one("#close-button", Button).disabled = True
        self.query_one("#chat-input", ChatComposer).disabled = True
        self.query_one("#chat-status", Static).update("○ Disconnecting...")

        try:
            if self.keyboard_worker is not None:
                try:
                    await self.keyboard_worker.wait()
                except WorkerError:
                    self.activity.write("Keyboard submission worker stopped", "Keyboard worker stopped")
            await self.live.close()
        finally:
            try:
                await asyncio.to_thread(self.persistence.close)
            except Exception as error:
                self.activity.write(f"Database cleanup failed ({type(error).__name__})", "Database cleanup failed")
            finally:
                if self.connection_worker is not None:
                    self.connection_worker.cancel()
                self.exit()

if __name__ == "__main__":
    CoraApp().run()