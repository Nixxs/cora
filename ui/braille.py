from math import cos, pi
from time import monotonic

from rich.style import Style
from rich.text import Text
from textual.color import Color
from textual.reactive import reactive
from textual.widget import Widget


# A pulsing graph's newest point brightens and fades back over this many seconds.
PULSE_SECONDS = 2.0
# A growing graph rises from the bottom to its values over this many seconds when they first arrive.
GROW_SECONDS = 1.0
WHITE = Color(255, 255, 255)

# A braille cell is 2 dots wide and 4 high; each dot is one bit added to U+2800.
# Indexed [column][row from the top].
BRAILLE_DOTS = ((0x01, 0x02, 0x04, 0x40), (0x08, 0x10, 0x20, 0x80))


def braille_graph(
    values, width: int, height: int, maximum: float = 1.0, downward: bool = False,
) -> list[str]:
    """Draw the latest values as a filled area graph, two values per cell, newest on the right.

    Columns grow up from the bottom, or down from the top when downward.
    """
    if width <= 0 or height <= 0:
        return []
    dots_high = height * 4
    recent = list(values)[-width * 2:]
    padded = [0.0] * (width * 2 - len(recent)) + recent
    cells = [[0] * width for _ in range(height)]
    for x, value in enumerate(padded):
        share = min(max(value / maximum, 0.0), 1.0) if maximum > 0 else 0.0
        for dot in range(round(share * dots_high)):  # Dot 0 is the row the column grows from.
            row = dot // 4 if downward else height - 1 - dot // 4
            dot_row = dot % 4 if downward else 3 - dot % 4
            cells[row][x // 2] |= BRAILLE_DOTS[x % 2][dot_row]
    return ["".join(chr(0x2800 + cell) for cell in row) for row in cells]


def braille_bars(
    values, width: int, height: int, maximum: float = 1.0, downward: bool = False,
) -> list[str]:
    """Spread the values evenly across the full width as bars, with a gap after each."""
    columns, count = width * 2, len(values)
    spread = []
    for x in range(columns):
        index = x * count // columns if count else 0
        last_column = (x + 1) * count // columns != index
        # Only leave gaps when every bar is at least two dot columns wide.
        spread.append(0.0 if not count or (last_column and columns >= count * 2) else values[index])
    return braille_graph(spread, width, height, maximum, downward)


class BrailleGraph(Widget):
    """A btop-style braille graph that fills whatever size it is given.

    As bars, each value is one bar across the width rather than a moment in a scrolling history.
    """

    values = reactive(())

    def __init__(
        self, maximum: float = 1.0, downward: bool = False, bars: bool = False, pulse: bool = False,
        grow: bool = False, **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.maximum = maximum
        self.downward = downward
        self.bars = bars
        self.pulse = pulse
        self.grow = grow
        self.grow_started = None
        self.grow_timer = None

    def on_mount(self) -> None:
        if self.pulse:
            self.set_interval(0.1, self.refresh)

    def watch_values(self, old, new) -> None:
        # Only the first values grow in; later refreshes just redraw.
        if self.grow and new and not old:
            self.grow_started = None
            self.grow_timer = self.set_interval(1 / 30, self.refresh)

    def render(self) -> str | Text:
        draw = braille_bars if self.bars else braille_graph
        values = self.values
        if self.grow_timer is not None:
            # The clock starts at the first frame drawn, so a slow first layout can't skip the growth.
            if self.grow_started is None:
                self.grow_started = monotonic()
            progress = min((monotonic() - self.grow_started) / GROW_SECONDS, 1.0)
            if progress == 1.0:
                self.grow_timer.stop()
                self.grow_timer = None
            # Eases out: fast at first, slowing as it reaches the real values.
            values = tuple(value * (1 - (1 - progress) ** 2) for value in values)
        rows = draw(values, self.size.width, self.size.height, self.maximum, self.downward)
        if not (self.pulse and self.values):
            return "\n".join(rows)
        # 0 to 1 and back: brightest halfway through each pulse.
        level = (1 - cos(2 * pi * monotonic() / PULSE_SECONDS)) / 2
        style = Style(color=self.styles.color.blend(WHITE, level).rich_color)
        text = Text("\n".join(rows))
        for row in range(len(rows)):
            # The last cell of each row holds the newest point.
            end = (row + 1) * (self.size.width + 1) - 1
            text.stylize(style, end - 1, end)
        return text
