"""A pop-up asking the person to approve one tool call that changes something."""

import json

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static, TextArea

from services.confirmation import ConfirmationRequest


class ConfirmScreen(ModalScreen):
    """Deny has focus and there are no letter shortcuts, so typing elsewhere can't approve by accident."""

    BINDINGS = [("escape", "deny", "Deny")]

    def __init__(self, request: ConfirmationRequest, answer) -> None:
        super().__init__()
        self.request = request
        self.answer = answer

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-view"):
            yield Label(f"Cora wants to use {self.request.title}", id="confirm-title")
            yield Static(Text.assemble(
                (self.request.tool, "bold #e0e6ef"), ("  changes things", "#d7a06a"),
            ))
            # Exactly what will be sent; scrolls and can be selected, but not edited.
            yield TextArea(
                json.dumps(self.request.arguments, indent=2, ensure_ascii=False),
                read_only=True, id="confirm-arguments",
            )
            with Horizontal(id="confirm-buttons"):
                yield Button("Deny", id="confirm-deny")
                yield Button("Approve", id="confirm-approve", variant="warning")
            yield Label("Nothing runs until you approve. Esc denies.", id="confirm-hint")

    def on_mount(self) -> None:
        self.query_one("#confirm-deny", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.answer(self.request.id, event.button.id == "confirm-approve")
        self.dismiss()

    def action_deny(self) -> None:
        self.answer(self.request.id, False)
        self.dismiss()
