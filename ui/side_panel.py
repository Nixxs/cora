"""Beside the chat's box: Cora's voice, and below it Claude Code's sessions while she listens for
them. The same graph and lines as the dashboard's voice and claude code boxes, so the chat shows
what's going on without leaving the history. Hidden when the window is too narrow to spare it."""

from collections.abc import Callable
from datetime import datetime, timezone

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.events import Resize

from claude_code.sessions import NEEDS_YOU
from devices.audio_bands import BAND_COUNT
from ui.braille import BrailleGraph
from ui.chat_widgets import BorderLine
from ui.claude_code_view import ClaudeSessionList
from ui.dashboard import FALL_PER_TICK

# Narrower than this (in columns), the chat keeps the whole width.
SIDE_PANEL_MIN_WIDTH = 110
VOICE_SECONDS = 0.05
SESSIONS_SECONDS = 1


class SidePanel(Vertical):
    """voice_bands() -> (Cora's, the person's) loudness bands; claude_sessions() -> the sessions,
    newest first, or None while Cora isn't listening for Claude Code."""

    def __init__(self, voice_bands: Callable, claude_sessions: Callable, **options) -> None:
        super().__init__(**options)
        self.voice_bands = voice_bands
        self.claude_sessions = claude_sessions

    def compose(self) -> ComposeResult:
        # Cora's voice rises above the line and the person's hangs below it.
        with Vertical(id="side-voice", classes="dashboard-box"):
            yield BrailleGraph(id="side-speaker-graph", bars=True)
            yield BorderLine(id="side-voice-axis")
            yield BrailleGraph(id="side-mic-graph", downward=True, bars=True)
        with Vertical(id="side-claude", classes="dashboard-box"):
            # One entry per session; a click follows it in a pop-up.
            yield ClaudeSessionList(self.claude_sessions, id="side-claude-scroll")

    def on_mount(self) -> None:
        self.query_one("#side-voice").border_title = "voice"
        self.query_one("#side-claude").border_title = "claude code"
        self.timers = [self.set_interval(VOICE_SECONDS, self.sample_voice),
                       self.set_interval(SESSIONS_SECONDS, self.show_claude_code)]
        if not self.display:
            # Hidden already (a narrow window): they start when there's room.
            for timer in self.timers:
                timer.pause()
        self.show_claude_code()

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
            self.show_claude_code()

    def showing(self) -> bool:
        """On the screen in front and not hidden: nothing to draw otherwise."""
        return self.display and self.is_attached and self.app.screen is self.screen

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
        sessions = self.claude_sessions()
        self.set_class(sessions is not None, "with-claude-code")
        if sessions is None:
            return
        self.query_one("#side-claude-scroll", ClaudeSessionList).show(sessions, datetime.now(timezone.utc))
        waiting = sum(1 for session in sessions if session.state == NEEDS_YOU)
        self.query_one("#side-claude").border_subtitle = (
            f"{waiting} need{'s' if waiting == 1 else ''} you" if waiting else "")


class ChatRow(Horizontal):
    """The chat's history with the side panel beside it, when there's room for both."""

    def on_resize(self, event: Resize) -> None:
        for panel in self.query(SidePanel):
            panel.show(event.size.width >= SIDE_PANEL_MIN_WIDTH)
