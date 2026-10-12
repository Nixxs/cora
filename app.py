from collections import deque
from datetime import datetime
import json
import sys

import pyperclip
from textual.actions import SkipAction
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, HorizontalGroup
from textual.widgets import Collapsible, RichLog, Static, Button

from dotenv import load_dotenv
from textual import work

from chat_widgets import ChatMarkdown, SelectableRichLog
from composer import ChatComposer
from live import CoraLive


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
        self.transcript_speaker = None
        self.transcript_text = ""
        self.transcript_idle_timer = None
        self.active_transcript_index = None
        self.log_entries = []
        self.written_item_ids = set()
        self.activity_entries = deque(maxlen=500)
        self.closing = False
        self.close_tool_pending = False
        self.handled_tool_call_ids = set()
        # Each tool has its own argument validator and async handler.
        self.tool_handlers = {
            "close_app": (self.validate_close_arguments, self.close_from_tool),
        }

        self.live = CoraLive()
        self.connection_worker = self.connect_to_cora()

    async def on_chat_composer_submitted(self, event: ChatComposer.Submitted) -> None:
        message = event.text.strip()
        composer = event.composer

        if not message or composer.disabled:
            return

        composer.disabled = True
        try:
            await self.live.send_text(message)
        except Exception as error:
            self.write_debug(f"Text send error: {error}", "Text send error")
            return
        finally:
            composer.disabled = False
            composer.focus()

        self.flush_transcript()
        self.write_log(f"You: {message}")
        self.write_log("")

        composer.clear()

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
            self.write_debug(f"Connection error: {error}", "Connection failed")
        else:
            status.update("○ Disconnected")
            self.write_debug("Connection closed", "Disconnected")
        finally:
            self.flush_transcript()

    def handle_live_event(self, event) -> None:
        status = self.query_one("#chat-status", Static)
        mic_status = self.query_one("#mic-status", Static)

        if event.type == "session.started":
            status.update("● Connected")
            self.write_debug("Live session started", "Connected")

        elif event.type == "session.input_transcript.delta":
            mic_status.update("🎙 Speech received")
            if self.mic_idle_timer is not None:
                self.mic_idle_timer.stop()
            # Transcript silence is a UI hint, not a confirmed end of speech.
            self.mic_idle_timer = self.set_timer(1.5, self.reset_mic_indicator)
            self.append_transcript("You", event.delta)

        elif event.type == "session.output_transcript.delta":
            self.append_transcript("Cora", event.delta)

        elif event.type == "session.thinking.appended":
            command_id = event.client_event_id
            if command_id and command_id.startswith("typed_context_"):
                self.write_debug(
                    f"Live accepted typed context: id={command_id}",
                    "Typed context accepted",
                )

        elif event.type == "session.delegation.created":
            delegation = event.delegation
            self.write_debug(
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
                    self.handle_function_call(item)
                else:
                    self.display_written_item(item)
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
                self.write_debug(
                    f"Backend {event_type}: "
                    f"delegation_id={event.delegation_id}, "
                    f"response_id={response.get('id')}",
                    summary,
                )
                error = response.get("error") or backend_event.get("error")
                if error:
                    self.write_debug(f"Backend error: {error}", "Backend error")
                if event_type == "error" and not error:
                    self.write_debug(
                        f"Backend error: {backend_event.get('message')}",
                        "Backend error",
                    )
                details = response.get("incomplete_details")
                if details:
                    self.write_debug(f"Backend incomplete: {details}", "Backend incomplete")
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
                self.write_debug(
                    f"Search {event_type}: "
                    f"delegation_id={event.delegation_id}, "
                    f"item_id={backend_event.get('item_id')}",
                    summary,
                )

        elif event.type == "error":
            status.update("○ API error")
            self.write_debug(f"API error: {event.error.message}", "API error")
            command_id = getattr(event.error, "client_event_id", None)
            if command_id:
                self.write_debug(f"Rejected command: id={command_id}", "API error")

        elif event.type == "session.closed":
            self.flush_transcript()
            self.write_debug(f"Session ended: {event.reason}", "Disconnected")

    def display_written_item(self, item: dict) -> None:
        item_id = item.get("id")
        if not item_id or item_id in self.written_item_ids:
            return

        # Read the backend's original answer, not the spoken transcript.
        # Ignore search/tool output, reasoning, and intermediate commentary.
        if item.get("type") != "message" or item.get("role") != "assistant":
            return
        if item.get("status") != "completed" or item.get("phase") == "commentary":
            return
        parts = []
        for content in item.get("content") or []:
            if content.get("type") == "output_text":
                parts.append(content.get("text") or "")
            elif content.get("type") == "refusal":
                parts.append(content.get("refusal") or "")

        text = "\n\n".join(parts)
        if not text.strip():
            self.write_debug(
                f"Completed backend message has no written text: item_id={item_id}",
                "No screen content",
            )
            return

        # Plain conversational answers belong in Live's spoken transcript.
        # Other written deliverables opt in with a leading backend marker.
        screen_marker = "<!-- cora:screen -->"
        screen_requested = text.lstrip().startswith(screen_marker)
        if screen_requested:
            text = text.lstrip()[len(screen_marker):].lstrip("\r\n")
        written = ChatMarkdown(text, code_theme="monokai")
        has_visual_content = any(
            token.type in ("fence", "code_block", "table_open")
            for token in written.parsed
        )
        if not text.strip() or not (screen_requested or has_visual_content):
            return

        self.written_item_ids.add(item_id)
        # Keep renderables in history so transcript redraws preserve formatting.
        # Do not end an active spoken/user transcript when a result arrives.
        self.log_entries.extend([
            "",
            "Cora · on screen:",
            written,
            "",
        ])
        self.redraw_log()
        self.write_debug(
            f"Written answer displayed: item_id={item_id}, characters={len(text)}",
            "Written answer displayed",
        )

    def write_debug(self, message: str, summary: str) -> None:
        # Activity must not flush or alter the active conversation entry.
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.activity_entries.append(f"{timestamp}  {message}")
        panel = self.query_one("#activity-panel", Collapsible)
        panel.title = f"Activity · {summary}"
        panel.set_class(
            any(word in summary.lower() for word in ("error", "failed", "incomplete")),
            "activity-error",
        )
        if not panel.collapsed:
            self.redraw_activity_log()

    def on_collapsible_expanded(self, event: Collapsible.Expanded) -> None:
        if event.collapsible.id == "activity-panel":
            # Wait for layout to restore the hidden log's usable width.
            self.call_after_refresh(self.redraw_activity_log)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close-button":
            event.button.disabled = True
            await self.action_disconnect_and_quit()

    def redraw_activity_log(self) -> None:
        panel = self.query_one("#activity-panel", Collapsible)
        output = self.query_one("#activity-output", RichLog)
        width = output.scrollable_content_region.width
        if panel.collapsed or width <= 0:
            return
        output.clear()
        for entry in self.activity_entries:
            output.write(entry, width=width, scroll_end=False)
        output.scroll_end(animate=False)

    def write_log(self, message: str) -> None:
        self.flush_transcript()
        self.log_entries.append(message)
        self.query_one("#chat-output", RichLog).write(message)

    def append_transcript(self, speaker: str, delta: str) -> None:
        if not delta:
            return
        if speaker != self.transcript_speaker:
            self.flush_transcript()

        if self.active_transcript_index is None:
            self.transcript_speaker = speaker
            self.log_entries.append("")
            self.active_transcript_index = len(self.log_entries) - 1

        # Keep the spacing supplied by Live; fragments can split inside words.
        self.transcript_text += delta
        self.log_entries[self.active_transcript_index] = (
            f"{speaker}: {self.transcript_text.strip()}"
        )
        self.redraw_log()

        if self.transcript_idle_timer is not None:
            self.transcript_idle_timer.stop()
        # A pause groups text for display; it is not an API turn boundary.
        self.transcript_idle_timer = self.set_timer(2.0, self.flush_transcript)

    def redraw_log(self) -> None:
        # RichLog.write adds lines, so redraw to update the active message.
        output = self.query_one("#chat-output", RichLog)
        output.clear()
        for entry in self.log_entries:
            output.write(entry, scroll_end=False)
        output.scroll_end(animate=False)

    def flush_transcript(self) -> None:
        if self.transcript_idle_timer is not None:
            self.transcript_idle_timer.stop()
            self.transcript_idle_timer = None

        had_active_transcript = self.active_transcript_index is not None
        if had_active_transcript and self.log_entries[-1] != "":
            self.log_entries.append("")

        self.transcript_text = ""
        self.transcript_speaker = None
        self.active_transcript_index = None

        if had_active_transcript:
            self.redraw_log()

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
            self.write_debug(f"Clipboard error: {error}", "Clipboard error")
        else:
            self.notify("Selected text copied", timeout=2)

    def handle_audio_status(self, working: bool, message: str) -> None:
        self.write_debug(
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
            self.write_debug(
                f"[mic] {message}",
                "Microphone active" if working else "Microphone stopped",
            )

    def handle_function_call(self, item: dict) -> None:
        if self.closing:
            return
        name = item.get("name")
        if not isinstance(name, str) or name not in self.tool_handlers:
            self.write_debug(f"Unknown tool: {name}", "Tool error")
            return
        call_id = item.get("call_id")
        if not isinstance(call_id, str) or not call_id.strip() or item.get("status") not in (None, "completed"):
            self.write_debug(f"Incomplete {name} call rejected", "Tool error")
            return
        if call_id in self.handled_tool_call_ids:
            return
        validate, _ = self.tool_handlers[name]
        try:
            arguments = json.loads(item.get("arguments", ""))
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be a JSON object")
            validate(arguments)
        except (TypeError, ValueError):
            self.write_debug(f"Invalid arguments for {name}", "Tool error")
            return

        # Deduplicate all tools, not just close_app, before scheduling.
        self.handled_tool_call_ids.add(call_id)
        self.run_worker(self.run_tool(name, call_id, arguments), group="tool-actions")

    async def run_tool(self, name: str, call_id: str, arguments: dict) -> None:
        _, handler = self.tool_handlers[name]
        try:
            # Handlers send their own result/continuation; closing is terminal.
            await handler(call_id, arguments)
        except Exception as error:
            self.write_debug(f"{name} failed: {error}", "Tool error")

    @staticmethod
    def validate_close_arguments(arguments: dict) -> None:
        if arguments:
            raise ValueError("close_app requires an empty argument object")

    async def close_from_tool(self, call_id: str, arguments: dict) -> None:
        # Only this tool reserves shutdown; other tools stay independent.
        if self.closing or self.close_tool_pending:
            return
        self.close_tool_pending = True
        self.write_debug(f"close_app requested: call_id={call_id}", "Closing Cora")
        try:
            connection = self.live.connection
            if connection is not None and not self.closing:
                await connection.response.item.create(
                    item={
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps({"status": "shutting_down"}),
                    }
                )
            # No response.create: this terminal action ends the session rather
            # than requesting another backend answer or a spoken goodbye.
        except Exception as error:
            self.write_debug(f"Close tool result error: {error}", "Tool error")
        finally:
            # Separate worker lets the Live reader process session.closed.
            await self.action_disconnect_and_quit()

    async def action_disconnect_and_quit(self) -> None:
        if self.closing:
            return
        self.closing = True
        self.query_one("#close-button", Button).disabled = True
        self.query_one("#chat-status", Static).update("○ Disconnecting...")

        try:
            await self.live.close()
        finally:
            if self.connection_worker is not None:
                self.connection_worker.cancel()
            self.exit()

if __name__ == "__main__":
    CoraApp().run()