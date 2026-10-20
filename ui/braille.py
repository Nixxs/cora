from textual.reactive import reactive
from textual.widget import Widget


# A braille cell is 2 dots wide and 4 high; each dot is one bit added to U+2800.
# Indexed [column][row from the top].
BRAILLE_DOTS = ((0x01, 0x02, 0x04, 0x40), (0x08, 0x10, 0x20, 0x80))


def braille_graph(values, width: int, height: int, maximum: float = 1.0) -> list[str]:
    """Draw the latest values as a filled area graph, two values per cell, newest on the right."""
    if width <= 0 or height <= 0:
        return []
    dots_high = height * 4
    recent = list(values)[-width * 2:]
    padded = [0.0] * (width * 2 - len(recent)) + recent
    cells = [[0] * width for _ in range(height)]
    for x, value in enumerate(padded):
        share = min(max(value / maximum, 0.0), 1.0) if maximum > 0 else 0.0
        for dot in range(round(share * dots_high)):  # Dot 0 is the bottom row.
            cells[height - 1 - dot // 4][x // 2] |= BRAILLE_DOTS[x % 2][3 - dot % 4]
    return ["".join(chr(0x2800 + cell) for cell in row) for row in cells]


class BrailleGraph(Widget):
    """A btop-style braille graph that fills whatever size it is given."""

    values = reactive(())

    def __init__(self, maximum: float = 1.0, **kwargs) -> None:
        super().__init__(**kwargs)
        self.maximum = maximum

    def render(self) -> str:
        return "\n".join(braille_graph(self.values, self.size.width, self.size.height, self.maximum))
