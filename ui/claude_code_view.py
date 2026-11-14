"""Claude Code's sessions as the dashboard and the chat's side panel list them, and the pop-up that
follows one: what it's doing, and its latest exchanges as they come.

The pop-up reads the session's transcript to show you, on this PC; nothing it reads goes to Cora
(her own reading is behind Settings > Claude Code > Let her read transcripts)."""

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.events import Click
from textual.widgets import Label, Static, TextArea

from claude_code.sessions import ENDED, FAILED, FINISHED, NEEDS_YOU, STARTED, WORKING, how_long
from claude_code.transcripts import (
    SHOWN_ENTRY_MAX_CHARS, SHOWN_EXCHANGES_MAX, SHOWN_TOTAL_MAX_CHARS, recent_exchanges, session_transcript,
)
from ui.context_view import DetailScreen
from ui.meters import LABEL_STYLE, TRACK_STYLE
from ui.when import ago

CLAUDE_STYLES = {NEEDS_YOU: "#e0b45c", FAILED: "#d27a7a", WORKING: "#7fb8d8", STARTED: "#7fb8d8",
                 FINISHED: "#8fc49a"}
CLAUDE_ORDER = (NEEDS_YOU, FAILED, WORKING, FINISHED, STARTED, ENDED)
# An ended session stays listed this long.
CLAUDE_ENDED_SHOWN = timedelta(minutes=15)
# A session's line of detail in the list is cut to this; the pop-up has it whole.
DETAIL_PREVIEW_CHARS = 120
# How often an open pop-up looks for what's new.
FOLLOW_SECONDS = 2
NO_SESSIONS = "no sessions yet"


def shown_sessions(sessions, now) -> list:
    """The sessions listed: needs you and failed first, newest first within each; ended ones for a while."""
    return sorted(
        (session for session in sessions if session.state != ENDED or now - session.since < CLAUDE_ENDED_SHOWN),
        key=lambda session: (CLAUDE_ORDER.index(session.state), -session.since.timestamp()),
    )


def session_text(session, now) -> Text:
    """One session: its project, what it's doing and since when, then what it last said or was asked."""
    line = Text("● ", style=CLAUDE_STYLES.get(session.state, LABEL_STYLE))
    line.append(session.project)
    line.append(f"  {session.state} · {ago(session.since, now)}", style=LABEL_STYLE)
    if session.detail:
        detail = session.detail
        if len(detail) > DETAIL_PREVIEW_CHARS:
            detail = detail[:DETAIL_PREVIEW_CHARS - 1] + "…"
        line.append(f"\n  {detail}", style=TRACK_STYLE)
    return line


def claude_code_lines(sessions, now) -> Text:
    """All the listed sessions as one text, a blank line between them."""
    lines = Text("\n\n").join(session_text(session, now) for session in shown_sessions(sessions, now))
    return lines or Text(NO_SESSIONS, style=TRACK_STYLE)


def follow_text(session, now, exchanges: list[dict] | None) -> str:
    """The pop-up's text: where and how the session is, then its latest exchanges, newest last."""
    lines = [session.cwd, f"{session.state} for {how_long(now - session.since)} · "
                          f"last heard {ago(session.updated_at, now)}"]
    if session.prompt:
        lines += ["", f"Asked: {session.prompt}"]
    if session.detail and session.detail != session.prompt:
        lines += ["", f"Latest: {session.detail}"]
    lines += ["", "── latest exchanges ──", ""]
    if exchanges is None:
        lines.append("Its transcript can't be read (yet).")
    elif not exchanges:
        lines.append("Nothing said yet.")
    for exchange in exchanges or []:
        who = "You" if exchange["who"] == "you" else "Claude"
        lines += [f"{who}: {exchange['said']}", ""]
    return "\n".join(lines).rstrip() + "\n"


def read_exchanges(session) -> list[dict] | None:
    """Blocking (file reads): the session's latest exchanges, or None without a readable transcript."""
    path = session_transcript(session.session_id, session.transcript_path)
    if path is None:
        return None
    return recent_exchanges(path, most=SHOWN_EXCHANGES_MAX, total_max=SHOWN_TOTAL_MAX_CHARS,
                            entry_max=SHOWN_ENTRY_MAX_CHARS)


class ClaudeSessionScreen(DetailScreen):
    """One session, followed while open: it looks again every couple of seconds, and keeps to the
    bottom (the newest) unless you've scrolled up to read."""

    def __init__(self, session_id: str, find_session: Callable) -> None:
        super().__init__("Claude Code", "Loading…")
        self.session_id = session_id
        # find_session(session_id) -> the session as it is now, or None once it's no longer listed.
        self.find_session = find_session
        self.shown_once = False

    def on_mount(self) -> None:
        super().on_mount()
        self.follow()
        self.set_interval(FOLLOW_SECONDS, self.follow)

    @work(group="claude-session-follow", exclusive=True, exit_on_error=False)
    async def follow(self) -> None:
        session = self.find_session(self.session_id)
        if session is None:
            self.show("Claude Code", "This session isn't listed any more.\n")
            return
        exchanges = await asyncio.to_thread(read_exchanges, session)
        now = datetime.now(timezone.utc)
        self.show(f"Claude Code · {session.project} · {session.state} for {how_long(now - session.since)}",
                  follow_text(session, now, exchanges))

    def show(self, title: str, text: str) -> None:
        if not self.is_attached:
            return
        self.query_one("#context-view-title", Label).update(title)
        area = self.query_one("#context-view-text", TextArea)
        if area.text == text:
            return
        at_end = area.scroll_y >= area.max_scroll_y - 1
        area.load_text(text)
        if at_end or not self.shown_once:
            area.scroll_end(animate=False)
        self.shown_once = True


class SessionEntry(Static):
    """One session in the list; a click opens it in a pop-up that follows it."""

    def __init__(self, session_id: str, content: Text, open_session: Callable[[str], None]) -> None:
        super().__init__(content, classes="openable claude-session")
        self.session_id = session_id
        self.open_session = open_session
        self.tooltip = "click to follow it"

    def on_click(self, event: Click) -> None:
        self.open_session(self.session_id)


class ClaudeSessionList(VerticalScroll):
    """The sessions, one clickable entry each. get_sessions() -> the sessions, newest first, or None."""

    def __init__(self, get_sessions: Callable, **options) -> None:
        super().__init__(**options)
        self.get_sessions = get_sessions
        self.listed: list[str] | None = None

    def compose(self) -> ComposeResult:
        yield Static(NO_SESSIONS, classes="claude-none")

    def show(self, sessions, now) -> None:
        shown = shown_sessions(sessions, now)
        listed = [session.session_id for session in shown]
        if listed != self.listed:
            # Which sessions, or their order, changed: listed afresh (otherwise each is only updated).
            self.listed = listed
            self.remove_children()
            if shown:
                self.mount_all([SessionEntry(session.session_id, session_text(session, now), self.open_session)
                                for session in shown])
            else:
                self.mount(Static(NO_SESSIONS, classes="claude-none"))
            return
        for entry, session in zip(self.query(SessionEntry), shown):
            entry.update(session_text(session, now))

    def find_session(self, session_id: str):
        return next((session for session in self.get_sessions() or [] if session.session_id == session_id), None)

    def open_session(self, session_id: str) -> None:
        self.app.push_screen(ClaudeSessionScreen(session_id, self.find_session))
