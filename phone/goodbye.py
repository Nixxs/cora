"""Hanging up after goodbye, without waiting on the model to do it.

On calls that aren't the person's, end_call needs her voice model to delegate the hang-up, and it
often doesn't bother once it has said goodbye. So the call watches what's said: when her turn ends
in a farewell ("bye", "take care", "have a good day"), and the other end says nothing more than a
farewell back, the call ends once her goodbye has played and the line has been quiet for
QUIET_SECONDS. Anything else said (by either side), or work she's still doing in her backend
(such as passing on what she learned), keeps the call open.
"""

import re
from time import monotonic

# Matched against the end of a turn only, so "before I say goodbye, ..." doesn't count.
FAREWELL = re.compile(
    r"\b(?:good\s?-?bye|bye(?:\s?-?bye)?|take care|talk (?:to you )?soon|speak (?:to you )?soon|"
    r"see you|catch you later|have an? (?:good|great|nice|lovely|wonderful) \w+)\b",
    re.IGNORECASE,
)
# How much of the end of a turn is checked for a farewell.
TAIL_CHARS = 60
# A farewell back from them is short; anything longer is more conversation.
SHORT_REPLY_CHARS = 60
# Quiet on the line after her goodbye has played, before hanging up.
QUIET_SECONDS = 2.0
# Backend work that's taking longer than this no longer holds the call open.
DELEGATION_MAX_SECONDS = 20.0


def ends_in_farewell(text: str) -> bool:
    return bool(FAREWELL.search(" ".join(text.split())[-TAIL_CHARS:]))


class GoodbyeWatch:
    """Fed the call's transcript and events; due() says when to hang up."""

    def __init__(self, clock=monotonic) -> None:
        self.clock = clock
        self.her_text = ""
        self.their_text = ""
        self.her_farewell = False
        self.last_activity = clock()
        self.last_playing = clock()
        self.delegating_since: float | None = None
        self.fired = False

    def heard(self, role: str, delta: str) -> None:
        """A transcript delta: role "assistant" is her, "user" the other end."""
        self.last_activity = self.clock()
        if role == "assistant":
            if self.their_text:
                # A new turn of hers.
                self.her_text, self.their_text = "", ""
            self.her_text += delta
            self.her_farewell = ends_in_farewell(self.her_text)
        else:
            self.their_text += delta

    def delegation(self, started: bool) -> None:
        self.last_activity = self.clock()
        self.delegating_since = self.clock() if started else None

    def activity(self) -> None:
        """Something else is happening in her session (her backend working)."""
        self.last_activity = self.clock()

    @property
    def armed(self) -> bool:
        if not self.her_farewell:
            return False
        said = " ".join(self.their_text.split())
        return not said or (len(said) <= SHORT_REPLY_CHARS and ends_in_farewell(said))

    def due(self, playing: bool) -> bool:
        """Whether to hang up now; `playing` is whether her audio is still playing down the line."""
        now = self.clock()
        if playing:
            self.last_playing = now
        if self.fired or not self.armed or playing:
            return False
        if self.delegating_since is not None and now - self.delegating_since < DELEGATION_MAX_SECONDS:
            return False
        if now - max(self.last_activity, self.last_playing) < QUIET_SECONDS:
            return False
        self.fired = True
        return True
