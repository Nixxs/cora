"""Labelled horizontal meters for dashboard values between 0 and 1."""

from datetime import date, timedelta
import math

from rich.text import Text
from textual.events import Click, MouseMove
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget


# Small squares leave gaps between cells and rows, so bars don't merge into one block.
CELL = "■"
LABEL_STYLE = "#8f98a8"
TRACK_STYLE = "#415575"
HOVER_STYLE = "on #202838"
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


class ContextMeters(Meters):
    """Meters whose rows can be clicked; the hovered row is highlighted."""

    hover_row = reactive(None)

    class Selected(Message):
        def __init__(self, meters: "ContextMeters", index: int) -> None:
            super().__init__()
            self.meters = meters
            self.index = index

        @property
        def control(self) -> "ContextMeters":
            return self.meters

    def render(self) -> Text:
        text = super().render()
        if self.rows and self.hover_row is not None:
            # Each row is exactly the widget's width, then a newline.
            start = self.hover_row * (self.size.width + 1)
            text.stylize(HOVER_STYLE, start, start + self.size.width)
        return text

    def on_mouse_move(self, event: MouseMove) -> None:
        self.hover_row = event.y if 0 <= event.y < len(self.rows) else None
        self.tooltip = "click to see what's in it" if self.hover_row is not None else None

    def on_leave(self) -> None:
        self.hover_row = None

    def on_click(self, event: Click) -> None:
        if 0 <= event.y < len(self.rows):
            self.post_message(self.Selected(self, event.y))


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


SPEND_BAR_ROWS = 4
BAR_EIGHTHS = " ▁▂▃▄▅▆▇█"
MONTH_INITIALS = "JFMAMJJASOND"
PAST_MONTH_STYLE = "#6fa888"
THIS_MONTH_STYLE = "#8fcca6"
NO_SPEND_HINT = (
    "To see what Cora costs each month, add an OpenAI Admin key (Restricted, Usage API: Read) "
    "in Settings > General."
)


def money(amount: float, currency: str = "usd") -> str:
    return f"${amount:,.2f}" if currency.lower() == "usd" else f"{amount:,.2f} {currency.upper()}"


class SpendChart(Widget):
    """This month's OpenAI spend, and a bar per month for the last year; hover a bar for its amount.

    Without an Admin key (or when costs can't be read) it shows a short line instead, whose
    tooltip and click say what's needed.
    """

    spend = reactive(None, layout=True)  # an openai_spend.Spend, or None
    message = reactive("loading…", layout=True)  # shown instead of the chart, with `hint` explaining it
    hint = reactive("")

    def show(self, spend=None, message: str = "", hint: str = "") -> None:
        self.spend, self.message, self.hint = spend, message, hint
        self.tooltip = hint or None

    def chart_width(self) -> int:
        """As wide as the heatmap above it: its initials and its weeks, without the last gap."""
        return max(1 + 2 * min(max((self.size.width - 2) // 2, 0), MAX_WEEKS), 0)

    def visible_months(self) -> int:
        """Months that fit, each at least a bar and a gap; the latest are kept."""
        return min((self.chart_width() + 1) // 2, len(self.spend.months)) if self.spend else 0

    def bar_spans(self) -> list[tuple[int, int]]:
        """(first column, width) of each visible month's bar: equal bars spread across the chart's
        width, the first at its left edge and the last ending at its right; spare columns go into the gaps."""
        visible, width = self.visible_months(), self.chart_width()
        if not visible:
            return []
        bar = max((width + 1) // visible - 1, 1)
        if visible == 1:
            return [(width - bar, bar)]
        return [(round(index * (width - bar) / (visible - 1)), bar) for index in range(visible)]

    def get_content_height(self, container, viewport, width: int) -> int:
        return 1 if self.spend is None else SPEND_BAR_ROWS + 2

    def render(self) -> Text:
        if self.spend is None:
            return Text.assemble(("OpenAI spend · ", LABEL_STYLE), (self.message, TRACK_STYLE))
        spans = self.bar_spans()
        months = self.spend.months[-len(spans):] if spans else ()
        this_month = self.spend.months[-1][1]
        text = Text.assemble(("OpenAI this month ", LABEL_STYLE),
                             (money(this_month, self.spend.currency), "bold #e0e6ef"))
        highest = max((amount for _, amount in months), default=0.0)
        levels = SPEND_BAR_ROWS * 8
        for row in range(SPEND_BAR_ROWS):
            text.append("\n")
            column = 0
            for index, ((_, amount), (start, width)) in enumerate(zip(months, spans)):
                text.append(" " * (start - column))
                column = start + width
                level = math.ceil(amount / highest * levels) if highest and amount else 0
                filled = min(max(level - (SPEND_BAR_ROWS - 1 - row) * 8, 0), 8)
                if not filled and row == SPEND_BAR_ROWS - 1:
                    # A month with no spend: a dot in the middle of its bar.
                    cell, style = f"{'·':^{width}}", TRACK_STYLE
                else:
                    style = THIS_MONTH_STYLE if index == len(months) - 1 else PAST_MONTH_STYLE
                    cell = BAR_EIGHTHS[filled] * width
                text.append(cell, style=style)
        text.append("\n")
        column = 0
        for (month, _), (start, width) in zip(months, spans):
            text.append(" " * (start - column))
            text.append(f"{MONTH_INITIALS[month.month - 1]:^{width}}", style=LABEL_STYLE)
            column = start + width
        return text

    def month_at(self, x: int, y: int) -> int | None:
        """The index into spend.months of the bar at (x, y), or None off a bar."""
        if self.spend is None or not 1 <= y <= SPEND_BAR_ROWS + 1:
            return None
        spans = self.bar_spans()
        for index, (start, width) in enumerate(spans):
            if start <= x < start + width:
                return len(self.spend.months) - len(spans) + index
        return None

    def on_mouse_move(self, event: MouseMove) -> None:
        if self.spend is None:
            self.tooltip = self.hint or None
            return
        index = self.month_at(event.x, event.y)
        if index is None:
            self.tooltip = (f"Spend for {self.spend.whose}, from OpenAI's costs (a few hours behind)"
                            if event.y == 0 else None)
            return
        month, amount = self.spend.months[index]
        so_far = " so far" if index == len(self.spend.months) - 1 else ""
        self.tooltip = f"{month:%b %Y}{so_far}: {money(amount, self.spend.currency)}"

    def on_click(self, event: Click) -> None:
        if self.spend is None and self.hint:
            self.app.notify(self.hint, timeout=10)
