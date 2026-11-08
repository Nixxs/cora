"""A pop-up showing one dashboard item's full text: a context item, a feeling, an interest, a task."""

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.events import Click
from textual.screen import ModalScreen
from textual.widgets import Label, TextArea

from context_window import ContextItem, short_tokens


class DetailScreen(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Close")]

    def __init__(self, title: str, text: str) -> None:
        super().__init__()
        self.title_text = title
        self.text = text

    def compose(self) -> ComposeResult:
        with Vertical(id="context-view"):
            yield Label(self.title_text, id="context-view-title", markup=False)
            # Read-only rather than a Static, so long text scrolls and can be selected and copied.
            yield TextArea(self.text.strip(), read_only=True, soft_wrap=True, id="context-view-text")
            yield Label("Esc or click outside to close", id="context-view-hint")

    def on_mount(self) -> None:
        self.query_one("#context-view-text", TextArea).focus()

    def on_click(self, event: Click) -> None:
        # Clicks inside the box are for reading and selecting; only the dimmed backdrop closes it.
        if event.widget is self:
            self.dismiss()


class ContextScreen(DetailScreen):
    """The text behind one item of the dashboard's context box."""

    def __init__(self, item: ContextItem) -> None:
        super().__init__(f"{item.name} · {item.side} · ~{short_tokens(item.tokens)} tokens", item.text)
        self.item = item
