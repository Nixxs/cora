from textual.app import App, ComposeResult
from textual.containers import Vertical, HorizontalGroup
from textual.widgets import Input, RichLog, Static

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

        self.live = CoraLive()
        self.connection_worker = self.connect_to_cora()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        message = event.value.strip()

        if not message:
            return

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
            )
        except Exception as error:
            status.update("○ Connection failed")
            self.write_log(f"Connection error: {error}")
        else:
            status.update("○ Disconnected")
        finally:
            self.flush_transcript()

    def handle_live_event(self, event) -> None:
        status = self.query_one("#chat-status", Static)
        mic_status = self.query_one("#mic-status", Static)

        if event.type == "session.started":
            status.update("● Connected")

        elif event.type == "session.input_transcript.delta":
            mic_status.update("🎙 Speech received")
            if self.mic_idle_timer is not None:
                self.mic_idle_timer.stop()
            # Transcript silence is a UI hint, not a confirmed end of speech.
            self.mic_idle_timer = self.set_timer(1.5, self.reset_mic_indicator)
            self.append_transcript("You", event.delta)

        elif event.type == "session.output_transcript.delta":
            self.append_transcript("Cora", event.delta)

        elif event.type == "session.delegation.created":
            delegation = event.delegation
            self.write_log(
                f"[debug] Delegation requested: "
                f"target={delegation.target}, id={delegation.id}"
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
                self.write_log(
                    f"[debug] Backend {event_type}: "
                    f"delegation_id={event.delegation_id}, "
                    f"response_id={response.get('id')}"
                )
                error = response.get("error") or backend_event.get("error")
                if error:
                    self.write_log(f"[debug] Backend error: {error}")
                if event_type == "error" and not error:
                    self.write_log(
                        f"[debug] Backend error: {backend_event.get('message')}"
                    )
                details = response.get("incomplete_details")
                if details:
                    self.write_log(f"[debug] Backend incomplete: {details}")
            elif event_type in (
                "response.web_search_call.in_progress",
                "response.web_search_call.searching",
                "response.web_search_call.completed",
            ):
                self.write_log(
                    f"[debug] Search {event_type}: "
                    f"delegation_id={event.delegation_id}, "
                    f"item_id={backend_event.get('item_id')}"
                )

        elif event.type == "error":
            status.update("○ API error")
            self.write_log(f"API error: {event.error.message}")

        elif event.type == "session.closed":
            self.flush_transcript()
            self.write_log(f"Session ended: {event.reason}")

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
            self.write_log(f"[mic] {message}")

    async def action_disconnect_and_quit(self) -> None:
        self.query_one("#chat-status", Static).update("○ Disconnecting...")

        try:
            await self.live.close()
        finally:
            self.connection_worker.cancel()
            self.exit()

if __name__ == "__main__":
    CoraApp().run()