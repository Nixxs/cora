"""What each Claude Code session is doing, as its hooks report it; kept for this run only, with
the last day's sessions read from their transcripts when she starts listening (see
transcripts.past_sessions).

Events come from Claude Code on this PC, but what Claude or you wrote in them is still just text:
shown to you, never taken by Cora as instructions.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re

STARTED = "started"
WORKING = "working"
FINISHED = "finished"
NEEDS_YOU = "needs you"
FAILED = "failed"
ENDED = "ended"

# Worth a line in Activity; working is what you just did yourself.
NOTEWORTHY = {STARTED, FINISHED, NEEDS_YOU, FAILED, ENDED}
# Notifications that mean Claude is waiting on you. idle_prompt only repeats that it finished.
WAITING_NOTIFICATIONS = {"permission_prompt", "elicitation_dialog", "elicitation_url_dialog", "agent_needs_input"}
# Events that mean Claude is carrying on, so whatever it was waiting on has been answered.
CARRYING_ON = {"PostToolUse"}
DETAIL_MAX_CHARS = 300
SESSION_ID_MAX_CHARS = 100
CWD_MAX_CHARS = 1000
# The most sessions kept; the longest quiet go first.
SESSIONS_MAX = 50

# What Cora tells you about (Settings > Claude Code), once a session has sat that way this long.
TELL_CHOICES = {
    "all": frozenset({NEEDS_YOU, FINISHED, FAILED}),
    "needs": frozenset({NEEDS_YOU, FAILED}),
    "off": frozenset(),
}
DEFAULT_TELL = "all"
TELL_AFTER = timedelta(minutes=3)


def short(text, limit: int = DETAIL_MAX_CHARS) -> str:
    """One line of at most limit characters, from whatever was given ("" for anything not text)."""
    if not isinstance(text, str):
        return ""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def project_name(cwd: str) -> str:
    """The project's folder name: D:\\Development\\cora -> cora."""
    parts = [part for part in re.split(r"[\\/]", cwd) if part]
    return parts[-1] if parts else cwd


def describe(event: dict) -> tuple[str, str | None] | None:
    """(state, detail) for an event that changes what a session is doing, else None.

    A detail of None keeps working on the prompt it was given.
    """
    name = event.get("hook_event_name")
    if name == "SessionStart":
        # Compacting starts the session afresh mid-task; it's still doing what it was.
        return None if event.get("source") == "compact" else (STARTED, short(event.get("source")))
    if name == "UserPromptSubmit":
        return WORKING, short(event.get("prompt"))
    if name in CARRYING_ON:
        return WORKING, None
    if name == "Stop":
        return FINISHED, short(event.get("last_assistant_message"))
    if name == "StopFailure":
        return FAILED, short(event.get("error_details") or event.get("error"))
    if name == "Notification" and event.get("notification_type") in WAITING_NOTIFICATIONS:
        return NEEDS_YOU, short(event.get("message"))
    if name == "PermissionRequest" and isinstance(event.get("tool_name"), str):
        return NEEDS_YOU, short(f"Claude needs your permission to use {event['tool_name']}")
    if name == "SessionEnd":
        return ENDED, short(event.get("reason"))
    return None


@dataclass
class ClaudeSession:
    session_id: str
    cwd: str
    state: str
    detail: str
    # When it was last heard from, and when it came to be in this state.
    updated_at: datetime
    since: datetime
    # The last prompt it was given.
    prompt: str = ""
    # Whether Cora has had her say about this state (told you, or let it pass).
    told: bool = False
    # Its transcript file, as Claude Code reported it (checked again before it's ever read).
    transcript_path: str = ""

    @property
    def project(self) -> str:
        return project_name(self.cwd)

    @property
    def changed(self) -> bool:
        """Whether the event just heard put it in this state."""
        return self.since == self.updated_at

    def line(self) -> str:
        """For Activity: 'cora: needs you (Claude needs your permission to use Bash)'."""
        if self.state in (NEEDS_YOU, FAILED) and self.detail:
            return f"{self.project}: {self.state} ({self.detail})"
        return f"{self.project}: {self.state}"


def clock_time(when: datetime, now: datetime) -> str:
    """'2:41 pm' if it was today, else 'Mon 11:58 pm', in this PC's time zone."""
    local = when.astimezone()
    shown = f"{local:%I:%M %p}".lstrip("0").lower()
    return shown if local.date() == now.astimezone().date() else f"{local:%a} {shown}"


# Markdown's emphasis and code marks, which a Signal message or her voice would give as they are.
MARKDOWN_MARKS = re.compile(r"\*\*|__|`")
# Where the rest is, when what Claude last said was cut short.
THE_REST = " (All of it is under Claude Code on Cora's dashboard, or on the web page.)"


def how_long(span: timedelta) -> str:
    """'under a minute', '4 minutes', '2 hours', '3 days'."""
    seconds = span.total_seconds()
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            count = int(seconds // size)
            return f"{count} {unit}{'' if count == 1 else 's'}"
    return "under a minute"


def summary(session: ClaudeSession, now: datetime) -> dict:
    """One session as Cora's claude_code tool gives it to her."""
    found = {"project": session.project, "folder": session.cwd, "state": session.state,
             "for": how_long(now - session.since), "last_heard": f"{how_long(now - session.updated_at)} ago"}
    if session.detail:
        found["detail"] = session.detail
    if session.prompt and session.prompt != session.detail:
        found["last_prompt"] = session.prompt
    return found


def tell_text(sessions: list[ClaudeSession], now: datetime) -> str:
    """What Cora tells you about sessions left waiting, one line each, with the time each stopped
    (how long ago would only ever be the wait, TELL_AFTER) and what Claude last said, as plain
    text, saying where the rest is if it was cut short."""
    lines = []
    for session in sessions:
        at = clock_time(session.since, now)
        if session.state == NEEDS_YOU:
            line = f"Claude Code in {session.project} has been waiting on you since {at}"
        elif session.state == FAILED:
            line = f"Claude Code stopped with an error in {session.project} at {at}"
        else:
            line = f"Claude Code finished in {session.project} at {at}"
        detail = MARKDOWN_MARKS.sub("", session.detail or "").strip()
        if detail.endswith("…"):
            detail += THE_REST
        lines.append(f"{line}: {detail}" if detail else f"{line}.")
    return "\n".join(lines)


class SessionTracker:
    """Each session's latest state, by session id."""

    def __init__(self, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.clock = clock
        self.sessions: dict[str, ClaudeSession] = {}

    def update(self, event: dict) -> ClaudeSession | None:
        """The session as it is now, or None for an event that says nothing new (or isn't one)."""
        if not isinstance(event, dict):
            return None
        session_id, cwd = event.get("session_id"), event.get("cwd")
        if not (isinstance(session_id, str) and 0 < len(session_id) <= SESSION_ID_MAX_CHARS):
            return None
        described = describe(event)
        if described is None:
            return None
        state, detail = described
        known = self.sessions.get(session_id)
        if not isinstance(cwd, str) or not cwd or len(cwd) > CWD_MAX_CHARS:
            cwd = known.cwd if known is not None else "?"
        transcript_path = event.get("transcript_path")
        if not isinstance(transcript_path, str) or not transcript_path or len(transcript_path) > CWD_MAX_CHARS:
            transcript_path = known.transcript_path if known is not None else ""
        now = self.clock()
        prompt = known.prompt if known is not None else ""
        if event.get("hook_event_name") == "UserPromptSubmit":
            prompt = detail
        if detail is None:
            detail = prompt
        if known is not None and known.state == state:
            session = ClaudeSession(session_id, cwd, state, detail, now, known.since, prompt, known.told,
                                    transcript_path)
        else:
            session = ClaudeSession(session_id, cwd, state, detail, now, now, prompt, transcript_path=transcript_path)
        self.sessions[session_id] = session
        if len(self.sessions) > SESSIONS_MAX:
            oldest = min(self.sessions.values(), key=lambda kept: kept.updated_at)
            del self.sessions[oldest.session_id]
        return session

    def add_past(self, sessions: list[ClaudeSession]) -> int:
        """Sessions from before she was listening (read from their transcripts); any already heard
        stays as heard. How many were added."""
        added = 0
        for session in sessions:
            if session.session_id not in self.sessions:
                self.sessions[session.session_id] = session
                added += 1
        while len(self.sessions) > SESSIONS_MAX:
            oldest = min(self.sessions.values(), key=lambda kept: kept.updated_at)
            del self.sessions[oldest.session_id]
        return added

    def recent(self) -> list[ClaudeSession]:
        """Newest first."""
        return sorted(self.sessions.values(), key=lambda session: session.updated_at, reverse=True)

    def due(self, states, after: timedelta = TELL_AFTER) -> list[ClaudeSession]:
        """Sessions in one of states for at least after, not told about yet; each comes up once.

        Any session past the wait has had its turn, told or not, so changing what you're told
        about never brings up old ones.
        """
        now = self.clock()
        due = []
        for session in self.sessions.values():
            if session.told or now - session.since < after:
                continue
            session.told = True
            if session.state in states:
                due.append(session)
        return sorted(due, key=lambda session: session.since)
