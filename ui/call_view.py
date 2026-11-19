"""A call she placed for a task, followed as it happens: a line beside the chat (and in the
dashboard's tasks) while it rings, while she's on it and for a while after, and a pop-up with its
transcript as it comes. The view is TaskCallsMixin.task_call_view()'s: who, how it stands, for
how long, and what's been said, "them" and "cora" line by line."""

from collections.abc import Callable

from rich.text import Text
from textual.events import Click
from textual.widgets import Static

from ui.claude_code_view import CLAUDE_STYLES
from ui.context_view import DetailScreen, MarkdownView, Part
from ui.meters import LABEL_STYLE, TRACK_STYLE

STATE_STYLES = {"ringing": CLAUDE_STYLES["started"], "on the call": CLAUDE_STYLES["working"],
                "ended": CLAUDE_STYLES["finished"], "didn't connect": CLAUDE_STYLES["failed"]}
SAID_BY = {"them": ("Them", "bold #7fb8d8"), "cora": ("Cora", "bold #7fbf73")}
# The latest line in the summary is cut to this; the pop-up has it whole.
PREVIEW_CHARS = 100
FOLLOW_SECONDS = 1


def call_summary(view: dict) -> Text:
    """The call in a line or two: who, how it stands and for how long, then the latest thing said."""
    line = Text("● ", style=STATE_STYLES.get(view["state"], LABEL_STYLE))
    line.append(f"call with {view['who']}")
    line.append(f"  {view['state']} · {view['for']}", style=LABEL_STYLE)
    if view["lines"]:
        latest = view["lines"][-1]
        said = latest["said"] if len(latest["said"]) <= PREVIEW_CHARS else latest["said"][:PREVIEW_CHARS - 1] + "…"
        line.append(f"\n  {SAID_BY[latest['who']][0]}: {said}", style=TRACK_STYLE)
    return line


def call_parts(view: dict) -> list[Part]:
    """The pop-up's text: the task and how the call stands, then everything said, newest last."""
    parts: list[Part] = [Text(f"{view['number']} · for \"{view['goal']}\"\n{view['state']} · {view['for']}",
                              style=LABEL_STYLE)]
    if not view["lines"]:
        parts.append(Text("Nothing said yet.", style=TRACK_STYLE))
    for line in view["lines"]:
        who, style = SAID_BY[line["who"]]
        parts.append(Text.assemble((f"{who}: ", style), line["said"]))
    return parts


class CallScreen(DetailScreen):
    """The call, followed while open: it looks again every second, and keeps to the bottom (the
    newest) unless you've scrolled up to read."""

    def __init__(self, get_view: Callable[[], dict | None]) -> None:
        super().__init__("Call", "")
        self.get_view = get_view

    def on_mount(self) -> None:
        super().on_mount()
        self.follow()
        self.set_interval(FOLLOW_SECONDS, self.follow)

    def follow(self) -> None:
        if not self.is_attached:
            return
        view = self.get_view()
        title = Text("Call") if view is None else Text(f"Call with {view['who']} · {view['state']} · {view['for']}")
        self.query_one("#context-view-title").update(title)
        self.query_one("#context-view-text", MarkdownView).show(
            [Text("The call isn't shown any more.")] if view is None else call_parts(view), keep_at_end=True)


class CallEntry(Static):
    """The call in a line or two; hidden while there's none. A click opens it in a pop-up that follows it."""

    def __init__(self, get_view: Callable[[], dict | None], **options) -> None:
        super().__init__("", classes="openable call-entry", **options)
        self.get_view = get_view
        self.tooltip = "click to follow the call"
        self.display = False

    def refresh_view(self) -> bool:
        """Shown as the call is now; whether there's one to show."""
        view = self.get_view()
        self.display = view is not None
        if view is not None:
            self.update(call_summary(view))
        return view is not None

    def on_click(self, event: Click) -> None:
        if self.get_view() is not None:
            self.app.push_screen(CallScreen(self.get_view))
