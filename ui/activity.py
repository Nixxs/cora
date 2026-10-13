"""Bounded diagnostic history and updates to the activity panel."""

from collections import deque
from datetime import datetime

from textual.widgets import Collapsible, RichLog


class Activity:
    def __init__(self, panel: Collapsible, output: RichLog):
        self.panel = panel
        self.output = output
        self.entries = deque(maxlen=500)

    def write(self, message: str, summary: str) -> None:
        # Activity is independent of the active conversation entry.
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.entries.append(f"{timestamp}  {message}")
        self.panel.title = f"Activity · {summary}"
        self.panel.set_class(
            any(word in summary.lower() for word in ("error", "failed", "incomplete")),
            "activity-error",
        )
        if not self.panel.collapsed:
            self.redraw()

    def redraw(self) -> None:
        width = self.output.scrollable_content_region.width
        if self.panel.collapsed or width <= 0:
            return
        self.output.clear()
        for entry in self.entries:
            self.output.write(entry, width=width, scroll_end=False)
        self.output.scroll_end(animate=False)