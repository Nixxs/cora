"""Labelled horizontal meters for dashboard values between 0 and 1."""

from datetime import date, timedelta
import math

from rich.text import Text
from textual.events import MouseMove
from textual.reactive import reactive
from textual.widget import Widget


# Small squares leave gaps between cells and rows, so bars don't merge into one block.
CELL = "■"
LABEL_STYLE = "#8f98a8"
TRACK_STYLE = "#415575"
TONE_STYLES = {"warm": "#8fcca6", "neutral": "#8f98a8", "curt": "#e6c07b", "hostile": "#f0a38a"}
# Dim to bright: each step is a quarter of the busiest day.
HEAT_STYLES = ("#35574a", "#4f8068", "#6fa888", "#8fcca6")
WEEKDAYS = "MTWTFSS"
# The heatmap shows about a month at its narrowest and half a year at its widest.
MIN_WEEKS = 5
MAX_WEEKS = 26


def meter_bar(value: float, width: int) -> str:
    """The filled part of a meter `width` cells wide; value is clamped to 0..1."""
    if width <= 0:
        return ""
    return CELL * round(min(max(value, 0.0), 1.0) * width)


class Meters(Widget):
    """One `label ■■■■■■ 62%` line per row, sized to the widget's width."""

    # (label, value) pairs, or (label, value, text) to show text instead of the percentage.
    # A change re-lays out the widget, so an auto height grows to fit the new rows.
    rows = reactive((), layout=True)
    placeholder = reactive("", layout=True)

    def __init__(self, placeholder: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self.placeholder = placeholder

    def render(self) -> Text:
        if not self.rows:
            return Text(self.placeholder, style=TRACK_STYLE)
        # Labels take at most half the width; longer ones are cut short so bars keep room.
        label_width = min(max(len(row[0]) for row in self.rows), self.size.width // 2)
        shown = [row[2] if len(row) > 2 else f"{row[1]:.0%}" for row in self.rows]
        shown_width = max(4, *(len(item) for item in shown))
        # A space either side of the bar, then the right-aligned value.
        bar_width = self.size.width - label_width - shown_width - 2
        text = Text()
        for index, ((label, value, *_), value_text) in enumerate(zip(self.rows, shown)):
            if index:
                text.append("\n")
            filled = meter_bar(value, bar_width)
            if len(label) > label_width:
                label = label[:label_width - 1] + "…"
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


class CalendarHeatmap(Widget):
    """A week per column, Monday at the top and today in the right-hand column; brighter is busier."""

    days = reactive(None)  # counts per day, oldest first, ending today; None until loaded
    today_row = reactive(0)  # today's weekday, Monday = 0

    def visible_weeks(self) -> int:
        """Weeks that fit after the day initials, each a cell and a gap; capped so a wide box ends at the grid."""
        return min(max((self.size.width - 2) // 2, 0), MAX_WEEKS)

    def render(self) -> Text:
        if self.days is None:
            return Text()
        weeks = self.visible_weeks()
        busiest = max(self.days, default=0)
        text = Text()
        for row, initial in enumerate(WEEKDAYS):
            if row:
                text.append("\n")
            text.append(f"{initial} ", style=LABEL_STYLE)
            for week in range(weeks):
                days_ago = (weeks - 1 - week) * 7 + self.today_row - row
                if not 0 <= days_ago < len(self.days):
                    text.append("  ")  # later this week, or older than the data
                    continue
                count = self.days[-1 - days_ago]
                level = math.ceil(count / busiest * len(HEAT_STYLES)) - 1 if count else None
                text.append(CELL + " ", style=TRACK_STYLE if level is None else HEAT_STYLES[level])
        return text

    def days_ago_at(self, x: int, y: int) -> int | None:
        """How many days before today the cell at (x, y) is, or None off a cell."""
        # Cells sit on even columns after the initials; odd columns are gaps.
        if self.days is None or not 0 <= y < len(WEEKDAYS) or x < 2 or x % 2:
            return None
        weeks = self.visible_weeks()
        week = (x - 2) // 2
        days_ago = (weeks - 1 - week) * 7 + self.today_row - y
        return days_ago if week < weeks and 0 <= days_ago < len(self.days) else None

    def on_mouse_move(self, event: MouseMove) -> None:
        days_ago = self.days_ago_at(event.x, event.y)
        if days_ago is None:
            self.tooltip = None
            return
        count = self.days[-1 - days_ago]
        day = date.today() - timedelta(days=days_ago)
        self.tooltip = f"{day:%a %d %b}: {count} message{'' if count == 1 else 's'}"
