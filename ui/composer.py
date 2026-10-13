import sys

import pyperclip
from textual import events
from textual.binding import Binding
from textual.message import Message
from textual.widgets import TextArea


class ChatComposer(TextArea):
    """Wrapped message editor: Enter sends; Shift+Enter adds a line."""

    BINDINGS = [Binding("ctrl+a", "select_all", "Select entire draft", show=False)]

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
        try:
            text = pyperclip.paste() if sys.platform == "win32" else self.app.clipboard
        except pyperclip.PyperclipException:
            self.notify("Could not read the clipboard", severity="error")
            return
        # Match the native bracketed-paste path; multiline paste never submits.
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        result = self.replace(text, *self.selection)
        self.move_cursor(result.end_location)