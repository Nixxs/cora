import itertools
import weakref

from rich.markdown import CodeBlock, Markdown
from rich import box
from rich.panel import Panel
from rich.segment import Segment
from rich.style import Style
from rich.syntax import Syntax
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import HorizontalGroup
from textual.selection import Selection
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Button, RichLog, Static
# Importing textual_image probes the terminal, so it must happen before the app starts.
from textual_image.renderable import HalfcellImage

from .image_view import ImageScreen

# Kept small so the half-cells look finer; wide images fill at most this many cells,
# tall ones at most this many rows.
CHAT_IMAGE_WIDTH = 24
CHAT_IMAGE_HEIGHT = 10
# Style meta must be simple values, so chat images are looked up by number when clicked.
_image_numbers = itertools.count(1)
_chat_images = weakref.WeakValueDictionary()
LINK_STYLE = Style(color="#7aa2f7", underline=True)


class BorderLine(Widget):
    """A horizontal border run that fills whatever width it is given."""

    def render(self) -> str:
        return "─" * self.size.width


class TitleBar(HorizontalGroup):
    """A panel's top border, drawn as widgets so its buttons can sit on the line."""

    def __init__(self, view: str) -> None:
        super().__init__(id="title-bar")
        self.view = view

    def compose(self) -> ComposeResult:
        yield Static("╭─ CORA ", classes="title-line")
        yield BorderLine(id="title-fill")
        yield Button(self.view, id="view-button", tooltip=f"Switch to the {self.view}")
        yield Static("─", classes="title-line")
        yield Button("settings", id="settings-button", tooltip="Model settings")
        yield Static("─", classes="title-line")
        yield Button("close", id="close-button", tooltip="Quit Cora")
        yield Static("─╮", classes="title-line")


class BlueCodeBlock(CodeBlock):
    def __rich_console__(self, console, options):
        syntax = Syntax(
            str(self.text).rstrip("\n"),
            self.lexer_name,
            theme=self.theme,
            background_color="#10141b",
            word_wrap=True,
            padding=0,
        )
        yield Panel(
            syntax,
            box=box.ROUNDED,
            padding=(0, 1),
            style="on #202838",
            border_style=Style(
                color="#415575", bgcolor="#141820", meta={"code_border": True}
            ),
        )


class ChatMarkdown(Markdown):
    elements = {**Markdown.elements, "fence": BlueCodeBlock, "code_block": BlueCodeBlock}


class ChatImage:
    """An image drawn with coloured half-cells, so it scrolls with the chat in any terminal.

    Clicking it opens the full image sharply in a pop-up.
    """

    def __init__(self, image, caption: str) -> None:
        self.image = image
        self.caption = caption
        # Chat redraws often; drawing from a small copy keeps them cheap.
        self.preview = image.copy()
        self.preview.thumbnail((200, 200))
        self.number = next(_image_numbers)
        _chat_images[self.number] = self
        self.meta = Style(meta={"chat_image": self.number})

    def __rich_console__(self, console, options):
        if self.preview.width >= self.preview.height:
            cells = HalfcellImage(self.preview, width=min(CHAT_IMAGE_WIDTH, options.max_width), height="auto")
        else:
            cells = HalfcellImage(self.preview, width="auto", height=CHAT_IMAGE_HEIGHT)
        for segment in console.render(cells, options):
            # Marked so a click opens it and copying chat text skips the block characters.
            yield Segment(segment.text, (segment.style or Style()) + self.meta)


def chat_link(url: str) -> Style:
    """Style for text that opens `url` in the browser when clicked in the chat."""
    return LINK_STYLE + Style(meta={"chat_link": url})


class SelectableRichLog(RichLog):
    """Expose RichLog's rendered lines to Textual's mouse selection system."""

    def on_click(self, event) -> None:
        url = event.style.meta.get("chat_link")
        if url:
            self.app.open_url(url)
        image = _chat_images.get(event.style.meta.get("chat_image"))
        if image is not None:
            self.app.push_screen(ImageScreen(image.image, image.caption))

    def get_selection(self, selection: Selection) -> tuple[str, str]:
        selected_lines = []
        for y, line in enumerate(self.lines):
            span = selection.get_span(y)
            if span is None:
                continue
            text = line.text
            start, end = span
            if any(segment.style and segment.style.meta.get("chat_image") for segment in line):
                continue
            if any(segment.style and segment.style.meta.get("code_border") for segment in line):
                if text.startswith(("╭", "╰")):
                    continue
                # Remove decoration, adjusting selection columns to match.
                text = text[2:-2]
                start, end = max(0, start - 2), end if end == -1 else max(0, end - 2)
            text = text.rstrip()
            selected_lines.append(text[start:] if end == -1 else text[start:end])
        return "\n".join(selected_lines), "\n"

    def render_line(self, y: int) -> Strip:
        scroll_x, scroll_y = self.scroll_offset
        line_y = scroll_y + y
        selection = self.text_selection
        if selection is not None and line_y < len(self.lines):
            text = Text.assemble(*(
                (segment.text, segment.style) for segment in self.lines[line_y]
            ))
            span = selection.get_span(line_y)
            if span is not None:
                start, end = span
                text.stylize(
                    self.screen.get_component_rich_style("screen--selection"),
                    start,
                    len(text) if end == -1 else end,
                )
            strip = Strip(text.render(self.app.console))
            strip = strip.crop_extend(
                scroll_x, scroll_x + self.scrollable_content_region.width, self.rich_style
            ).apply_style(self.rich_style)
        else:
            strip = super().render_line(y)
        # Mouse events need source coordinates, including when scrolled.
        return strip.apply_offsets(scroll_x, line_y)