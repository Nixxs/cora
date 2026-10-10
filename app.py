from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, RichLog, Static


class CoraApp(App):

    CSS_PATH = "cora.tcss"
    BINDINGS = [
        ("ctrl+x", "quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="chat-panel"):
            yield RichLog(id="chat-output", wrap=True)
            yield Static("○ Not connected", id="chat-status")
            with Vertical(id="input-frame"):
                yield Input(
                    placeholder="Type a message to Cora...",
                    id="chat-input",
                )

    def on_mount(self) -> None:
        self.query_one("#chat-panel").border_title = "CORA"
        self.query_one("#chat-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        message = event.value.strip()

        if not message:
            return

        chat_output = self.query_one("#chat-output", RichLog)
        chat_output.write(f"You: {message}")
        chat_output.write("")

        event.input.clear()


if __name__ == "__main__":
    CoraApp().run()