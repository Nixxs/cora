from rich.markdown import CodeBlock, Markdown
from rich import box
from rich.panel import Panel
from rich.style import Style
from rich.syntax import Syntax
from rich.text import Text
from textual.selection import Selection
from textual.strip import Strip
from textual.widgets import RichLog


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


class SelectableRichLog(RichLog):
    """Expose RichLog's rendered lines to Textual's mouse selection system."""

    def get_selection(self, selection: Selection) -> tuple[str, str]:
        selected_lines = []
        for y, line in enumerate(self.lines):
            span = selection.get_span(y)
            if span is None:
                continue
            text = line.text
            start, end = span
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