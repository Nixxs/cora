"""Beside the chat's box: Cora's voice, a call she's placed for a task while it's on (and for a
while after), and below them Claude Code's sessions (while she listens for them), the house (while
Sensibo or presence is set up) and the open tasks. The same graph and lines as the dashboard's boxes,
so the chat shows what's going on without leaving the history. Hidden when the window is too narrow
to spare it.

Like the dashboard's, the boxes below voice minimise to chips (a click on the box's title), here on a
tray at the panel's foot; a click on a chip opens the box again. Which are minimised is remembered."""

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, HorizontalGroup, Vertical, VerticalScroll
from textual.events import Resize
from textual.widgets import Static

from claude_code.sessions import NEEDS_YOU
from devices.audio_bands import BAND_COUNT
from ui.braille import BrailleGraph
from ui.call_view import CallEntry
from ui.chat_widgets import BorderLine
from ui.claude_code_view import ClaudeSessionList
from ui.dashboard import FALL_PER_TICK, HOME_REFRESH_SECONDS, BoxChip, fill_task_list, home_text

# Narrower than this (in columns), the chat keeps the whole width.
SIDE_PANEL_MIN_WIDTH = 110
VOICE_SECONDS = 0.05
SESSIONS_SECONDS = 1
TASKS_SECONDS = 15
# The boxes below voice that minimise, top to bottom, with their titles.
SIDE_BOXES = {"side-claude": "claude code", "side-home": "home", "side-tasks": "tasks"}


class SidePanel(Vertical):
    """voice_bands() -> (Cora's, the person's) loudness bands; claude_sessions() -> the sessions,
    newest first, or None while Cora isn't listening for Claude Code; home_view() and presence_view()
    as the dashboard's home box takes them; load_tasks() -> the open tasks (awaited; None when they
    can't be read); save_minimised(box ids) whenever one is minimised or opened."""

    def __init__(self, voice_bands: Callable, claude_sessions: Callable,
                 task_call: Callable[[], dict | None] = lambda: None, *,
                 home_view: Callable = lambda: None, presence_view: Callable = lambda: None,
                 load_tasks: Callable[[], Awaitable] | None = None,
                 save_minimised: Callable[[list[str]], None] = lambda boxes: None, **options) -> None:
        super().__init__(**options)
        self.voice_bands = voice_bands
        self.claude_sessions = claude_sessions
        self.task_call = task_call
        self.home_view = home_view
        self.presence_view = presence_view
        self.load_tasks = load_tasks
        self.save_minimised = save_minimised
        self.minimised: set[str] = set()
        # Boxes not set up (Claude Code not listened for; no Sensibo or presence): no box, no chip.
        # Home is left out until the app's settings are loaded.
        self.absent: set[str] = {"side-home"}
        self.tasks = None
        # Set by ready(): until then the app's home and tasks can't be read.
        self.loaded = False

    def compose(self) -> ComposeResult:
        # Cora's voice rises above the line and the person's hangs below it.
        with Vertical(id="side-voice", classes="dashboard-box"):
            yield BrailleGraph(id="side-speaker-graph", bars=True)
            yield BorderLine(id="side-voice-axis")
            yield BrailleGraph(id="side-mic-graph", downward=True, bars=True)
        with Vertical(id="side-call", classes="dashboard-box"):
            yield CallEntry(self.task_call, id="side-call-entry")
        with Vertical(id="side-claude", classes="dashboard-box"):
            # One entry per session; a click follows it in a pop-up.
            yield ClaudeSessionList(self.claude_sessions, id="side-claude-scroll")
        with Vertical(id="side-home", classes="dashboard-box"):
            with VerticalScroll(id="side-home-scroll", classes="side-scroll"):
                yield Static(id="side-home-rooms")
        with Vertical(id="side-tasks", classes="dashboard-box"):
            with VerticalScroll(id="side-tasks-scroll", classes="side-scroll"):
                # One Openable per task: a click opens it in full.
                with Vertical(id="side-task-list"):
                    yield Static("reading…", classes="claude-none")
        # The minimised boxes, as chips.
        with HorizontalGroup(id="side-tray"):
            yield HorizontalGroup(id="side-chips")
            yield BorderLine(id="side-tray-fill")

    def on_mount(self) -> None:
        self.query_one("#side-voice").border_title = "voice"
        self.query_one("#side-call").border_title = "call"
        for box_id, title in SIDE_BOXES.items():
            self.query_one(f"#{box_id}").border_title = f"▾ {title}"
        self.timers = [self.set_interval(VOICE_SECONDS, self.sample_voice),
                       self.set_interval(SESSIONS_SECONDS, self.show_claude_code),
                       self.set_interval(HOME_REFRESH_SECONDS, self.show_home),
                       self.set_interval(TASKS_SECONDS, self.refresh_tasks)]
        if not self.display:
            # Hidden already (a narrow window): they start when there's room.
            for timer in self.timers:
                timer.pause()
        # Home and tasks wait for the app's settings and database (ready() once they're loaded).
        self.show_claude_code()
        self.lay_out()

    def show(self, showing: bool) -> None:
        """Shown or hidden with the room there is; its timers rest while it's hidden."""
        if self.display == showing:
            return
        self.display = showing
        for timer in getattr(self, "timers", []):
            if showing:
                timer.resume()
            else:
                timer.pause()
        if showing:
            self.show_all()

    def ready(self, minimised) -> None:
        """Once the app's settings and database are up: the saved minimised boxes, and every box."""
        self.loaded = True
        self.set_minimised(minimised)
        self.show_all()

    def show_all(self) -> None:
        self.show_claude_code()
        if self.loaded:
            self.show_home()
            self.refresh_tasks()
        self.lay_out()

    def showing(self) -> bool:
        """On the screen in front and not hidden: nothing to draw otherwise."""
        return self.display and self.is_attached and self.app.screen is self.screen

    # Minimising the boxes below voice to chips on the tray.

    def set_minimised(self, boxes) -> None:
        """The saved choice, once it's loaded (not saved again)."""
        self.minimised = {box for box in boxes if box in SIDE_BOXES}
        self.lay_out()

    def on_click(self, event) -> None:
        """A click on a box's top border (where its title is) minimises it."""
        node = event.widget
        while node is not None and node is not self and node.id not in SIDE_BOXES:
            node = node.parent
        if node is not None and node is not self and event.screen_y == node.region.y:
            event.stop()
            self.minimised.add(node.id)
            self.boxes_changed()

    def restore_box(self, box_id: str) -> None:
        if box_id in self.minimised:
            self.minimised.discard(box_id)
            self.boxes_changed()

    def boxes_changed(self) -> None:
        self.lay_out()
        self.save_minimised([box for box in SIDE_BOXES if box in self.minimised])
        if "side-tasks" not in self.minimised and self.tasks is None:
            self.refresh_tasks()

    def set_absent(self, box_id: str, absent: bool) -> None:
        if (box_id in self.absent) != absent:
            (self.absent.add if absent else self.absent.discard)(box_id)
            self.lay_out()

    def lay_out(self) -> None:
        """Each box shown while it's set up and open; voice keeps a small box while Claude Code's
        sessions take the rest; the tray only while something's minimised."""
        if not self.is_mounted:
            return
        for box_id in SIDE_BOXES:
            self.query_one(f"#{box_id}").display = box_id not in self.absent and box_id not in self.minimised
        self.set_class(self.query_one("#side-claude").display, "with-claude-code")
        self.call_later(self.show_chips)

    async def show_chips(self) -> None:
        chips = self.query_one("#side-chips", HorizontalGroup)
        await chips.remove_children()
        shown = [box for box in SIDE_BOXES if box in self.minimised and box not in self.absent]
        widgets = []
        for index, box in enumerate(shown):
            if index:
                widgets.append(Static("─", classes="tray-line"))
            widgets.append(BoxChip(box, SIDE_BOXES[box]))
        await chips.mount_all(widgets)
        self.query_one("#side-tray").display = bool(shown)

    # What each box shows.

    def sample_voice(self) -> None:
        if not self.showing():
            return
        speaker, mic = self.voice_bands()
        for graph_id, bands in (("#side-speaker-graph", speaker), ("#side-mic-graph", mic)):
            graph = self.query_one(graph_id, BrailleGraph)
            current = graph.values or (0.0,) * BAND_COUNT
            latest = bands or (0.0,) * BAND_COUNT
            graph.values = tuple(max(new, old - FALL_PER_TICK) for old, new in zip(current, latest))

    def show_claude_code(self) -> None:
        if not self.showing():
            return
        self.set_class(self.query_one("#side-call-entry", CallEntry).refresh_view(), "with-call")
        sessions = self.claude_sessions()
        self.set_absent("side-claude", sessions is None)
        if sessions is None:
            return
        self.query_one("#side-claude-scroll", ClaudeSessionList).show(sessions, datetime.now(timezone.utc))
        waiting = sum(1 for session in sessions if session.state == NEEDS_YOU)
        self.query_one("#side-claude").border_subtitle = (
            f"{waiting} need{'s' if waiting == 1 else ''} you" if waiting else "")

    def show_home(self) -> None:
        if not self.loaded or not self.showing():
            return
        view, presence = self.home_view(), self.presence_view()
        self.set_absent("side-home", view is None and presence is None)
        if "side-home" in self.absent or "side-home" in self.minimised:
            return
        shown, subtitle = home_text(view, presence, datetime.now(timezone.utc))
        self.query_one("#side-home-rooms", Static).update(shown)
        self.query_one("#side-home").border_subtitle = subtitle

    @work(group="side-tasks", exclusive=True, exit_on_error=False)
    async def refresh_tasks(self) -> None:
        if self.load_tasks is None or not self.loaded or not self.showing() or "side-tasks" in self.minimised:
            return
        tasks = await self.load_tasks()
        if tasks is None:
            return
        self.tasks = list(tasks)
        fill_task_list(self.query_one("#side-task-list", Vertical), self.tasks, datetime.now(timezone.utc))
        self.query_one("#side-tasks").border_subtitle = f"{len(self.tasks)} open" if self.tasks else ""


class ChatRow(Horizontal):
    """The chat's history with the side panel beside it, when there's room for both."""

    def on_resize(self, event: Resize) -> None:
        for panel in self.query(SidePanel):
            panel.show(event.size.width >= SIDE_PANEL_MIN_WIDTH)
