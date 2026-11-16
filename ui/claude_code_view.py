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
from textual.widgets import Label, Static

from claude_code.sessions import ENDED, FAILED, FINISHED, NEEDS_YOU, STARTED, WORKING, how_long
from claude_code.transcripts import (
    SHOWN_ENTRY_MAX_CHARS, SHOWN_EXCHANGES_MAX, SHOWN_TOTAL_MAX_CHARS, recent_exchanges, session_transcript,
)
from ui.context_view import DetailScreen, MarkdownView, Part
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


# The pop-up's labels for who said what, coloured as the chat's speakers are.
SAID_BY = {"you": ("You", "bold #7fb8d8"), "claude": ("Claude", "bold #7fbf73")}


def follow_parts(session, now, exchanges: list[dict] | None) -> list[Part]:
    """The pop-up's text: where and how the session is, then its latest exchanges, newest last,
    each shown as the Markdown it was written in."""
    where = Text(session.cwd, style=LABEL_STYLE)
    where.append(f"\n{session.state} for {how_long(now - session.since)} · "
                 f"last heard {ago(session.updated_at, now)}")
    parts: list[Part] = [where]
    if session.prompt:
        parts.append(Text.assemble(("Asked: ", LABEL_STYLE), session.prompt))
    if session.detail and session.detail != session.prompt:
        parts.append(Text.assemble(("Latest: ", LABEL_STYLE), session.detail))
    parts.append(Text("── latest exchanges ──", style=TRACK_STYLE))
    if exchanges is None:
        parts.append(Text("Its transcript can't be read (yet).", style=TRACK_STYLE))
    elif not exchanges:
        parts.append(Text("Nothing said yet.", style=TRACK_STYLE))
    for exchange in exchanges or []:
        who, style = SAID_BY["you" if exchange["who"] == "you" else "claude"]
        parts.append((Text(who, style=style), exchange["said"]))
    return parts


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
        super().__init__("Claude Code · loading…", "")
        self.session_id = session_id
        # find_session(session_id) -> the session as it is now, or None once it's no longer listed.
        self.find_session = find_session

    def on_mount(self) -> None:
        super().on_mount()
        self.follow()
        self.set_interval(FOLLOW_SECONDS, self.follow)

    @work(group="claude-session-follow", exclusive=True, exit_on_error=False)
    async def follow(self) -> None:
        session = self.find_session(self.session_id)
        if session is None:
            self.show("Claude Code", [Text("This session isn't listed any more.")])
            return
        exchanges = await asyncio.to_thread(read_exchanges, session)
        now = datetime.now(timezone.utc)
        self.show(f"Claude Code · {session.project} · {session.state} for {how_long(now - session.since)}",
                  follow_parts(session, now, exchanges))

    def show(self, title: str, parts: list[Part]) -> None:
        if not self.is_attached:
            return
        self.query_one("#context-view-title", Label).update(title)
        self.query_one("#context-view-text", MarkdownView).show(parts, keep_at_end=True)


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
