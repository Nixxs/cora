import io
import sys

from PIL import Image, ImageGrab
import pyperclip
from textual import events
from textual.actions import SkipAction
from textual.binding import Binding
from textual.message import Message
from textual.widgets import Button, TextArea

# The backend scales high-detail images to fit 2048x2048, so larger only costs upload time.
IMAGE_MAX_SIDE = 2048
IMAGE_MARKER = "image attached · Esc removes"
SENDING_IMAGE = "sending image…"
NO_IMAGE_TOOL = "To paste images, install wl-clipboard (Wayland) or xclip (X11)."

# Who typed messages go to, in the order the chip moves through them: Cora (as always), Rocky
# alone (like "@rocky", never through OpenAI), or both as a group chat (Rocky answers, then Cora).
CHAT_MODES = ("cora", "rocky", "group")
MODE_NAMES = {"cora": "Cora", "rocky": "Rocky", "group": "Group"}
MODE_PLACEHOLDERS = {
    "cora": "Type a message to Cora...",
    "rocky": "Type to Rocky (private: Cora doesn't see it)...",
    "group": "Type to Cora and Rocky (Cora sees it all)...",
}
MODE_TOOLTIPS = {
    "cora": "Your messages go to Cora. Click to talk to Rocky privately instead.",
    "rocky": "Your messages go to Rocky alone; Cora doesn't see them. Click for a group chat.",
    "group": "A group chat: Rocky answers each message first, then Cora (she sees his answers). Click for Cora.",
}
# While her voice is on the chat is hers alone (Rocky and a group chat don't work well with it): the
# chip stays on Cora, dimmed, and says why. She can still consult Rocky herself.
VOICE_ON_TOOLTIP = ("Cora's voice is on, so messages go to her alone (she can still consult Rocky). Rocky and "
                    "group chat come back when she's in standby.")
VOICE_ON_NOTE = "Cora's voice is on, so the chat is hers alone. Rocky and group chat come back in standby."


def next_mode(mode: str) -> str:
    return CHAT_MODES[(CHAT_MODES.index(mode) + 1) % len(CHAT_MODES)] if mode in CHAT_MODES else CHAT_MODES[0]


class ChatModeChip(Button):
    """Beside a chat box: who your typed messages go to. The app moves it on when clicked
    (CoraApp.set_chat_mode), and it shows the app's mode wherever it's drawn. Hidden while
    there's nobody but Cora to talk to (no model chosen for Rocky)."""

    def on_mount(self) -> None:
        offered = getattr(self.app, "rocky_offered", lambda: False)()
        voice_on = not getattr(self.app, "standby", True)
        mode = getattr(self.app, "chat_mode", CHAT_MODES[0]) if offered and not voice_on else CHAT_MODES[0]
        self.show(mode, offered, voice_on)

    def show(self, mode: str, offered: bool = True, voice_on: bool = False) -> None:
        """mode, shown; hidden unless Rocky is offered; on Cora and dimmed while her voice is on."""
        self.display = offered
        self.label = f"{MODE_NAMES[mode]} ›"
        self.set_class(voice_on, "voice-on")
        self.tooltip = VOICE_ON_TOOLTIP if voice_on else MODE_TOOLTIPS[mode]
        for each in CHAT_MODES:
            self.set_class(each == mode, f"mode-{each}")
        for composer in self.parent.query(ChatComposer):
            composer.placeholder = MODE_PLACEHOLDERS[mode]


def png_of(image: Image.Image) -> bytes:
    """An image as the chat sends it: at most IMAGE_MAX_SIDE a side, as PNG."""
    image.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGBA")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def clipboard_png() -> bytes | None:
    """The clipboard's image as PNG, or None when it holds no image or cannot be read.

    On Linux, Pillow reads it with wl-paste or xclip and raises NotImplementedError
    when neither is installed.
    """
    try:
        image = ImageGrab.grabclipboard()
    except NotImplementedError:
        raise
    except Exception:
        # An unreadable clipboard falls back to pasting text.
        return None
    if not isinstance(image, Image.Image):
        return None
    return png_of(image)


# What +img offers in the picker.
IMAGE_FILES = [("Images", "*.png *.jpg *.jpeg *.gif *.webp *.bmp *.tif *.tiff"), ("All files", "*.*")]


def pick_image_file() -> str | None:
    """The image file chosen in the system's own file picker (+img), or None if it was cancelled.
    It waits for the picker, so it runs in a thread; raises if no picker can open (no display, or
    Python without tkinter)."""
    import tkinter
    from tkinter import filedialog

    root = tkinter.Tk()
    root.withdraw()
    # In front of the terminal, not behind it.
    root.attributes("-topmost", True)
    try:
        path = filedialog.askopenfilename(parent=root, title="An image for Cora", filetypes=IMAGE_FILES)
    finally:
        root.destroy()
    return path or None


def file_png(path: str) -> bytes:
    """An image file as the chat sends it; raises if it isn't an image Pillow can read."""
    with Image.open(path) as image:
        image.load()
        return png_of(image)


class ChatComposer(TextArea):
    """Wrapped message editor: Enter sends; Shift+Enter adds a line."""

    BINDINGS = [
        Binding("ctrl+a", "select_all", "Select entire draft", show=False),
        Binding("escape", "remove_image", "Remove attached image", show=False),
    ]

    image_png: bytes | None = None
    told_no_image_tool = False

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
            image_png = clipboard_png()
        except NotImplementedError:
            image_png = None
            # Said once per box, since text pastes still work without the tool.
            if not self.told_no_image_tool:
                self.told_no_image_tool = True
                self.notify(NO_IMAGE_TOOL, severity="warning")
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

    @property
    def frame(self):
        """The bordered box it sits in (past the row it shares with its mode chip), where the
        image marker shows."""
        parent = self.parent
        return parent.parent if parent.has_class("composer-row") else parent

    def attach_image(self, image_png: bytes) -> None:
        # One image per message: a new paste replaces the last.
        self.image_png = image_png
        # Both composers sit in a bordered box, so the marker shows on its edge.
        self.frame.border_subtitle = IMAGE_MARKER

    def remove_image(self) -> None:
        self.image_png = None
        self.frame.border_subtitle = ""

    def action_remove_image(self) -> None:
        if self.image_png is None:
            raise SkipAction()
        self.remove_image()