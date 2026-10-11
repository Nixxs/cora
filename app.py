from collections import deque
from datetime import datetime

from textual.app import App, ComposeResult
from textual.containers import Vertical, HorizontalGroup
from textual.widgets import Collapsible, Input, RichLog, Static

from dotenv import load_dotenv
from textual import work

from live import CoraLive


load_dotenv()

class CoraApp(App):

    CSS_PATH = "cora.tcss"
    BINDINGS = [
        ("ctrl+x", "disconnect_and_quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="chat-panel"):
            yield RichLog(id="chat-output", wrap=True, min_width=1)
            with Collapsible(title="Activity · Ready", collapsed=True, id="activity-panel"):
                yield RichLog(id="activity-output", wrap=True, min_width=1, max_lines=500)
            with HorizontalGroup(id="status-bar"):
                yield Static("○ Not connected", id="chat-status")
                yield Static("🎙× Mic", id="mic-status")
            with Vertical(id="input-frame"):
                yield Input(
                    placeholder="Type a message to Cora...",
                    id="chat-input",
                )

    def on_mount(self) -> None:
        self.query_one("#chat-panel").border_title = "CORA"
        self.query_one("#chat-input", Input).focus()
        self.mic_idle_timer = None
        self.transcript_speaker = None
        self.transcript_text = ""
        self.transcript_idle_timer = None
        self.active_transcript_index = None
        self.log_entries = []
        self.activity_entries = deque(maxlen=500)

        self.live = CoraLive()
        self.connection_worker = self.connect_to_cora()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        message = event.value.strip()

        if not message:
            return

        event.input.disabled = True
        try:
            await self.live.send_text(message)
        except Exception as error:
            self.write_debug(f"Text send error: {error}", "Text send error")
            return
        finally:
            event.input.disabled = False
            event.input.focus()

        self.flush_transcript()
        self.write_log(f"You: {message}")
        self.write_log("")

        event.input.clear()

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
            if event_type in (
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
        if had_active_transcript:
            self.log_entries.append("")

        self.transcript_text = ""
        self.transcript_speaker = None
        self.active_transcript_index = None

        if had_active_transcript:
            self.redraw_log()

    def reset_mic_indicator(self) -> None:
        self.mic_idle_timer = None
        self.query_one("#mic-status", Static).update("🎙 Listening")

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

    async def action_disconnect_and_quit(self) -> None:
        self.query_one("#chat-status", Static).update("○ Disconnecting...")

        try:
            await self.live.close()
        finally:
            self.connection_worker.cancel()
            self.exit()

if __name__ == "__main__":
    CoraApp().run()