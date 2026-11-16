"""A pop-up showing one dashboard item's full text: a context item, a feeling, an interest, a task.

The text is shown as Markdown, as the chat shows hers, so headings, bold, lists, tables and code
read as they're meant to. Unlike chat Markdown, a single line break stays a line break and tags
stay as written: much of what's shown (prompts, instructions, transcripts) isn't written as
Markdown, and none of it should lose lines or words. JSON is shown as a code block."""

import json
import re

from markdown_it import MarkdownIt
from rich.console import Group, RenderableType
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.events import Click, Resize
from textual.screen import ModalScreen
from textual.widgets import Label

from context_window import ContextItem, short_tokens
from ui.chat_widgets import ChatMarkdown, SelectableRichLog

BACKTICKS = re.compile(r"`+")


class PopupMarkdown(ChatMarkdown):
    """Chat Markdown that keeps the text's own line breaks and shows tags as text."""

    def __init__(self, markup: str, **options) -> None:
        super().__init__(markup, code_theme="monokai", **options)
        parser = MarkdownIt().enable(["strikethrough", "table"]).disable(["html_block", "html_inline"])
        self.parsed = parser.parse(markup)
        for token in self.parsed:
            for child in token.children or []:
                if child.type == "softbreak":
                    child.type = "hardbreak"


def as_markdown(text: str) -> str:
    """The text to show as Markdown: JSON as a code block, anything else as it is."""
    stripped = text.strip()
    if stripped[:1] in ("{", "["):
        try:
            json.loads(stripped)
        except ValueError:
            return text
        # Longer than any run of backticks inside, so nothing in it can end the block.
        fence = "`" * max([3, *(len(run) + 1 for run in BACKTICKS.findall(stripped))])
        return f"{fence}json\n{stripped}\n{fence}"
    return text


# One part of a pop-up's text: Markdown, styled text shown as it is, or a label over Markdown.
Part = str | Text | tuple[Text, str]


def part_text(part: Part) -> str:
    if isinstance(part, str):
        return part
    if isinstance(part, Text):
        return part.plain
    label, markdown = part
    return f"{label.plain}\n{markdown}"


def part_shown(part: Part) -> RenderableType:
    if isinstance(part, str):
        return PopupMarkdown(part)
    if isinstance(part, Text):
        return part
    label, markdown = part
    return Group(label, PopupMarkdown(markdown))


class MarkdownView(SelectableRichLog):
    """A pop-up's text in parts, a blank line between them (see Part). It can be selected and
    copied, and is wrapped afresh when the pop-up is resized."""

    def __init__(self, **options) -> None:
        super().__init__(wrap=True, min_width=1, markup=False, auto_scroll=False, **options)
        self.parts: list[Part] = []
        self.drawn_width = 0

    @property
    def text(self) -> str:
        """What's shown, as plain text (Markdown as written)."""
        return "\n\n".join(part_text(part) for part in self.parts)

    def show(self, parts: list[Part], keep_at_end: bool = False) -> None:
        """Shown in place of what was; with keep_at_end, at the bottom if it was already there (or
        is shown for the first time), otherwise where you'd scrolled to."""
        if self.parts and parts == self.parts:
            # Unchanged: left as it is, so what you've selected stays selected.
            return
        first = not self.parts
        at_end = self.scroll_y >= self.max_scroll_y - 1
        place = self.scroll_y
        self.parts = list(parts)
        self.draw()
        if keep_at_end and (first or at_end):
            self.scroll_end(animate=False, immediate=True)
        else:
            self.scroll_to(y=place, animate=False, immediate=True)

    def draw(self) -> None:
        self.drawn_width = self.scrollable_content_region.width
        self.clear()
        for number, part in enumerate(self.parts):
            if number:
                self.write("", scroll_end=False)
            # Expanded to the box's width; Markdown would otherwise wrap at the console's 80.
            self.write(part_shown(part), expand=True, scroll_end=False)

    def on_resize(self, event: Resize) -> None:
        # Written lines keep the width they were wrapped to, so they're wrapped again to the new one.
        if self.parts and self.scrollable_content_region.width != self.drawn_width:
            place = self.scroll_y
            self.draw()
            self.scroll_to(y=place, animate=False, immediate=True)


class DetailScreen(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Close")]

    def __init__(self, title: str, text: str) -> None:
        super().__init__()
        self.title_text = title
        self.text = text

    def compose(self) -> ComposeResult:
        with Vertical(id="context-view"):
            yield Label(self.title_text, id="context-view-title", markup=False)
            yield MarkdownView(id="context-view-text")
            yield Label("Esc or click outside to close", id="context-view-hint")

    def on_mount(self) -> None:
        view = self.query_one("#context-view-text", MarkdownView)
        if self.text.strip():
            view.show([as_markdown(self.text.strip())])
        view.focus()

    def on_click(self, event: Click) -> None:
        # Clicks inside the box are for reading and selecting; only the dimmed backdrop closes it.
        if event.widget is self:
            self.dismiss()


class ContextScreen(DetailScreen):
    """The text behind one item of the dashboard's context box."""

    def __init__(self, item: ContextItem) -> None:
        super().__init__(f"{item.name} · {item.side} · ~{short_tokens(item.tokens)} tokens", item.text)
        self.item = item
