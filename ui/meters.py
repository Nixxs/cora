"""Labelled horizontal meters for dashboard values between 0 and 1."""

from rich.text import Text
from textual.reactive import reactive
from textual.widget import Widget


# Small squares leave gaps between cells and rows, so bars don't merge into one block.
CELL = "■"
LABEL_STYLE = "#8f98a8"
TRACK_STYLE = "#415575"
TONE_STYLES = {"warm": "#8fcca6", "neutral": "#8f98a8", "curt": "#e6c07b", "hostile": "#f0a38a"}


def meter_bar(value: float, width: int) -> str:
    """The filled part of a meter `width` cells wide; value is clamped to 0..1."""
    if width <= 0:
        return ""
    return CELL * round(min(max(value, 0.0), 1.0) * width)


class Meters(Widget):
    """One `label ■■■■■■ 62%` line per row, sized to the widget's width."""

    # (label, value) pairs, or (label, value, text) to show text instead of the percentage.
    rows = reactive(())
    placeholder = reactive("")

    def __init__(self, placeholder: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self.placeholder = placeholder

    def render(self) -> Text:
        if not self.rows:
            return Text(self.placeholder, style=TRACK_STYLE)
        label_width = max(len(row[0]) for row in self.rows)
        shown = [row[2] if len(row) > 2 else f"{row[1]:.0%}" for row in self.rows]
        shown_width = max(4, *(len(item) for item in shown))
        # A space either side of the bar, then the right-aligned value.
        bar_width = self.size.width - label_width - shown_width - 2
        text = Text()
        for index, ((label, value, *_), value_text) in enumerate(zip(self.rows, shown)):
            if index:
                text.append("\n")
            filled = meter_bar(value, bar_width)
            text.append(f"{label:<{label_width}} ", style=LABEL_STYLE)
            text.append(filled)
            text.append(CELL * (bar_width - len(filled)), style=TRACK_STYLE)
            text.append(f" {value_text:>{shown_width}}", style=LABEL_STYLE)
        return text


class ToneRibbon(Widget):
    """One coloured cell per conversation's tone, newest on the right; older ones drop off."""

    tones = reactive(None)  # None until the first snapshot loads

    def render(self) -> Text:
        if self.tones is None:
            return Text()
        if not self.tones:
            return Text("no everyday conversations yet", style=TRACK_STYLE)
        text = Text()
        for tone in self.tones[-self.size.width:]:
            text.append(CELL, style=TONE_STYLES[tone])
        return text
