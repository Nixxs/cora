from textual import work

from textual.app import App,ComposeResult
from textual.widgets import Footer, Header, RichLog, Input, Static
from textual.containers import Vertical

from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()
client = AsyncOpenAI()

class Chat(Vertical):
    """Chat window widget"""

    previous_response_id: str | None = None
    busy = False

    def compose(self) -> ComposeResult:
        with Vertical(id="chat-panel"):
            yield RichLog(id="chat-output", wrap=True)
            yield Static("", id="chat-status")

        yield Input(placeholder="your message to cora..", id="chat-input")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        message = event.value.strip()

        if not message or self.busy:
            return

        self.query_one("#chat-output", RichLog).write(f"You: {message}\n")
        event.input.clear()

        self.busy = True
        event.input.disabled = True
        self.dot_count = 0
        self.animate_dots()
        self.dots_timer.resume()

        self.get_reply(message)

    def on_mount(self) -> None:
        self.dot_count = 0
        self.dots_timer = self.set_interval(
            0.5, self.animate_dots, pause=True
        )

    def animate_dots(self) -> None:
        self.dot_count = self.dot_count % 3 + 1
        self.query_one("#chat-status", Static).update(
            f"Cora: {'.' * self.dot_count}"
        )

    @work
    async def get_reply(self, message: str) -> None:
        output = self.query_one("#chat-output", RichLog)
        chat_input = self.query_one("#chat-input", Input)

        try:
            response = await client.responses.create(
                model="gpt-6.1-sol",
                instructions="You are a helpful assistant. Keep replies concise.",
                input=message,
                previous_response_id=self.previous_response_id,
                store=True,
            )

            self.previous_response_id = response.id
            output.write(f"Cora: {response.output_text}\n")

        except Exception as error:
            output.write(f"Request failed: {error}\n")

        finally:
            self.dots_timer.pause()
            self.query_one("#chat-status", Static).update("")

            self.busy = False
            chat_input.disabled = False
            chat_input.focus()

class Cora(App):
    """textual ui for OpenAI Chat"""

    CSS_PATH = "cora.tcss"

    BINDINGS = [
        ("d", "toggle_dark", "Toggle Dark Mode")
    ]

    def compose(self) -> ComposeResult:
        """Application main layout"""

        yield Header()
        yield Chat()
        yield Footer()

    def on_mount(self) -> None:
        chat = self.query_one("#chat-output", RichLog)
        chat.write("Cora: Hello Nick, what are we working on today?\n")

    def action_toggle_dark(self) -> None:
        """dark mode toggle"""

        match self.theme:
            case "textual-light":
                self.theme = "textual-dark"
            case "textual-dark":
                self.theme = "textual-light"
            case _:
                print("unknown theme")

if __name__ == "__main__":
    app = Cora()
    app.run()

