"""A pop-up showing the text behind one item of the dashboard's context box."""

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.events import Click
from textual.screen import ModalScreen
from textual.widgets import Label, TextArea

from context_window import ContextItem, short_tokens


class ContextScreen(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Close")]

    def __init__(self, item: ContextItem) -> None:
        super().__init__()
        self.item = item

    def compose(self) -> ComposeResult:
        with Vertical(id="context-view"):
            yield Label(
                f"{self.item.name} · {self.item.side} · ~{short_tokens(self.item.tokens)} tokens",
                id="context-view-title",
            )
            # Read-only rather than a Static, so long text scrolls and can be selected and copied.
            yield TextArea(self.item.text.strip(), read_only=True, id="context-view-text")
            yield Label("Esc or click outside to close", id="context-view-hint")

    def on_mount(self) -> None:
        self.query_one("#context-view-text", TextArea).focus()

    def on_click(self, event: Click) -> None:
        # Clicks inside the box are for reading and selecting; only the dimmed backdrop closes it.
        if event.widget is self:
            self.dismiss()
