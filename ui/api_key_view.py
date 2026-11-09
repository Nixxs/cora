"""The first-start pop-up asking for an OpenAI API key, without which Cora can't connect."""

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static


class ApiKeyScreen(ModalScreen[bool]):
    """Dismissed with True once a key is checked and saved; Esc leaves it for Settings > General."""

    BINDINGS = [("escape", "later", "Later")]

    def __init__(self, check_and_save) -> None:
        super().__init__()
        # check_and_save(key) -> None once saved, or why not (never the key).
        self.check_and_save = check_and_save

    def compose(self) -> ComposeResult:
        with Vertical(id="api-key-view"):
            yield Label("Welcome to Cora", id="api-key-title")
            yield Static(
                "Cora thinks with OpenAI's models, so she needs your OpenAI API key before she can start. "
                "Create one at platform.openai.com/api-keys. It's checked with OpenAI, then kept in "
                "this PC's credential store (never in a file); you can replace it in Settings > General.",
                id="api-key-help",
            )
            yield Input(password=True, placeholder="sk-...", id="api-key-input")
            yield Static("", id="api-key-problem")
            with Horizontal(id="api-key-buttons"):
                yield Button("Later", id="api-key-later")
                yield Button("Save and start", id="api-key-save", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#api-key-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.save()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "api-key-save":
            self.save()
        else:
            self.action_later()

    def action_later(self) -> None:
        self.dismiss(False)

    def save(self) -> None:
        field_ = self.query_one("#api-key-input", Input)
        key = field_.value.strip()
        if not key:
            self.query_one("#api-key-problem", Static).update("Paste your key first.")
            return
        self.check(key)

    @work(group="api-key-check", exclusive=True)
    async def check(self, key: str) -> None:
        problem_line = self.query_one("#api-key-problem", Static)
        save = self.query_one("#api-key-save", Button)
        problem_line.update("Checking the key with OpenAI…")
        save.disabled = True
        try:
            problem = await self.check_and_save(key)
        finally:
            save.disabled = False
        if problem is None:
            self.dismiss(True)
        else:
            problem_line.update(problem)
