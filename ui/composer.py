import io
import sys

from PIL import Image, ImageGrab
import pyperclip
from textual import events
from textual.actions import SkipAction
from textual.binding import Binding
from textual.message import Message
from textual.widgets import TextArea

# The backend scales high-detail images to fit 2048x2048, so larger only costs upload time.
IMAGE_MAX_SIDE = 2048
IMAGE_MARKER = "image attached · Esc removes"
SENDING_IMAGE = "sending image…"


def clipboard_png() -> bytes | None:
    """The clipboard's image as PNG, or None when it holds no image or cannot be read."""
    try:
        image = ImageGrab.grabclipboard()
    except Exception:
        # Some platforms need an external clipboard tool; fall back to pasting text.
        return None
    if not isinstance(image, Image.Image):
        return None
    image.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGBA")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class ChatComposer(TextArea):
    """Wrapped message editor: Enter sends; Shift+Enter adds a line."""

    BINDINGS = [
        Binding("ctrl+a", "select_all", "Select entire draft", show=False),
        Binding("escape", "remove_image", "Remove attached image", show=False),
    ]

    image_png: bytes | None = None

    class Submitted(Message):
        def __init__(self, composer: "ChatComposer"):
            super().__init__()
            self.composer = composer
            self.text = composer.text

    def _refresh_size(self) -> None:
        super()._refresh_size()
        # Wrapped height includes soft wraps, not just explicit newlines.
        self.styles.height = min(4, max(1, self.virtual_size.height))

    async def _on_key(self, event: events.Key) -> None:
        if event.key in ("enter", "shift+enter", "ctrl+j"):
            event.stop()
            event.prevent_default()
            if self.read_only or self.disabled:
                return
            if event.key == "enter":
                self.post_message(self.Submitted(self))
            else:
                self.replace("\n", *self.selection)
            return
        await super()._on_key(event)

    def action_paste(self) -> None:
        if self.read_only or self.disabled:
            return
        image_png = clipboard_png()
        if image_png is not None:
            self.attach_image(image_png)
            return
        try:
            text = pyperclip.paste() if sys.platform == "win32" else self.app.clipboard
        except pyperclip.PyperclipException:
            self.notify("Could not read the clipboard", severity="error")
            return
        # Match the native bracketed-paste path; multiline paste never submits.
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        result = self.replace(text, *self.selection)
        self.move_cursor(result.end_location)

    def attach_image(self, image_png: bytes) -> None:
        # One image per message: a new paste replaces the last.
        self.image_png = image_png
        # Both composers sit in a bordered box, so the marker shows on its edge.
        self.parent.border_subtitle = IMAGE_MARKER

    def remove_image(self) -> None:
        self.image_png = None
        self.parent.border_subtitle = ""

    def action_remove_image(self) -> None:
        if self.image_png is None:
            raise SkipAction()
        self.remove_image()