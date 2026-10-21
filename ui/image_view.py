"""A pop-up showing a chat image sharply (Sixel or Kitty graphics when the terminal has them)."""

from textual.app import ComposeResult
from textual.containers import Center, Vertical
from textual.screen import ModalScreen
from textual.widgets import Label
# Importing the widget picks the terminal's best image method; it must happen before the app starts.
from textual_image.widget import Image


class ImageScreen(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Close")]

    def __init__(self, image, caption: str) -> None:
        super().__init__()
        self.image = image
        self.caption = caption

    def compose(self) -> ComposeResult:
        with Vertical(id="image-view"):
            yield Label(self.caption, id="image-view-caption")
            with Center(id="image-view-frame"):
                yield Image(self.image, id="image-view-image")
            yield Label("Esc or click to close", id="image-view-hint")

    def on_click(self) -> None:
        self.dismiss()

    def refresh(self, *regions, **kwargs):
        # Textual repaints this screen whenever something changes on the chat behind it
        # (only that case passes regions). Each repaint redraws the Sixel picture, which
        # made it blink, so the dimmed chat behind stays frozen while the picture is open.
        if regions:
            return self
        return super().refresh(**kwargs)
