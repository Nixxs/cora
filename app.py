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
            yield RichLog(id="chat-output", wrap=True)
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

        self.live = CoraLive()
        self.connection_worker = self.connect_to_cora()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        message = event.value.strip()

        if not message:
            return

        chat_output = self.query_one("#chat-output", RichLog)
        chat_output.write(f"You: {message}")
        chat_output.write("")

        event.input.clear()

    @work
    async def connect_to_cora(self) -> None:
        status = self.query_one("#chat-status", Static)
        output = self.query_one("#chat-output", RichLog)

        status.update("○ Connecting...")

        try:
            await self.live.run(
                self.handle_live_event,
                self.handle_mic_status,
            )
        except Exception as error:
            status.update("○ Connection failed")
            output.write(f"Connection error: {error}")
        else:
            status.update("○ Disconnected")

    def handle_live_event(self, event) -> None:
        status = self.query_one("#chat-status", Static)
        output = self.query_one("#chat-output", RichLog)

        if event.type == "session.started":
            status.update("● Connected")

        elif event.type == "error":
            status.update("○ API error")
            output.write(f"API error: {event.error.message}")

        elif event.type == "session.closed":
            output.write(f"Session ended: {event.reason}")

    def handle_mic_status(self, working: bool) -> None:
        mic_status = self.query_one("#mic-status", Static)

        if working:
            mic_status.update("🎙 Mic")
        else:
            mic_status.update("🎙× Mic")

    async def action_disconnect_and_quit(self) -> None:
        self.query_one("#chat-status", Static).update("○ Disconnecting...")

        try:
            await self.live.close()
        finally:
            self.connection_worker.cancel()
            self.exit()

if __name__ == "__main__":
    CoraApp().run()