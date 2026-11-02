"""Pure desire rules: fading, reinforcement, promotion from candidate, the caps and status changes.

No database, model, or clock access: callers pass in timestamps. A stored strength is
only a snapshot at `strength_at`, decayed the same way as interests but more slowly, so
a desire nothing reinforces fades over a few weeks. One that fades below LET_GO_THRESHOLD
is let go; an ended desire (fulfilled or let go) never changes again, and a new wish forms
as a new desire.

A desire starts as a candidate and becomes held once reinforced past HELD_THRESHOLD while
she holds fewer than MAX_HELD; candidates waiting behind those are capped too.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from subconscious.identity.identity_rules import Evidence
from subconscious.interests.interest_strength import effective_strength, validate_strength

INITIAL_STRENGTH = 0.3
HELD_THRESHOLD = 0.45  # a candidate becomes held at or above this, when there is room
LET_GO_THRESHOLD = 0.1  # a live desire below this has faded and is let go
# Held at 0.5 and never reinforced, a desire is let go after about three weeks.
HALF_LIFE = timedelta(days=10)
MAX_COUNTED_ELAPSED = timedelta(days=21)  # so one long absence cannot erase every desire
MAX_HELD = 3  # held + pursuing
MAX_CANDIDATES = 5
MAX_REINFORCE_DELTA = 0.15  # largest single reinforcement the store accepts
# The person's answers when she asks about a desire. 'not now' rests it from her steps for a while.
ANSWERS = ("yes", "not_now", "instead", "note")
PAUSED_FOR = timedelta(days=7)

STATUSES = ("candidate", "held", "pursuing", "fulfilled", "let_go")
LIVE = ("candidate", "held", "pursuing")
HELD = ("held", "pursuing")
ENDED = ("fulfilled", "let_go")

# What one daily reflection may do: at most one new desire, a few reinforcements, one let go.
ACTIONS = ("form", "reinforce", "fulfil", "let_go")
MAX_PER_REFLECTION = {"form": 1, "reinforce": 3, "fulfil": 1, "let_go": 1}
REINFORCE_DELTAS = {"some": 0.075, "strong": MAX_REINFORCE_DELTA}

# Status changes she (or a later step) makes on purpose: event kind -> (allowed from, to).
CHANGES = {
    "pursue": (("held",), "pursuing"),
    "pause": (("pursuing",), "held"),
    "fulfil": (HELD, "fulfilled"),
    "let_go": (LIVE, "let_go"),
}


def validate_status(status: str) -> str:
    if status not in STATUSES:
        raise ValueError("Unknown desire status")
    return status


def current_strength(strength: float, strength_at: datetime, now: datetime) -> float:
    """The stored strength decayed by elapsed time, with the desire half-life."""
    return effective_strength(
        strength, strength_at, now, half_life=HALF_LIFE, max_elapsed=MAX_COUNTED_ELAPSED,
    )


def settle(strength: float, strength_at: datetime, status: str, now: datetime) -> tuple[float, str]:
    """Return the strength and status to save with `now` as the new strength_at.

    An ended desire keeps its last strength. Settling only lowers strength, so it can let
    a desire go but never promotes one; promotion goes through reinforcement.
    """
    validate_status(status)
    validate_strength(strength)
    if status in ENDED:
        return strength, status
    settled = current_strength(strength, strength_at, now)
    return settled, "let_go" if settled < LET_GO_THRESHOLD else status


def promoted(status: str, strength: float, held_count: int) -> str:
    """A candidate strong enough becomes held while she holds fewer than MAX_HELD; else unchanged."""
    validate_status(status)
    validate_strength(strength)
    validate_count(held_count)
    if status == "candidate" and strength >= HELD_THRESHOLD and held_count < MAX_HELD:
        return "held"
    return status


def can_form(candidate_count: int) -> bool:
    """Whether another candidate may form."""
    validate_count(candidate_count)
    return candidate_count < MAX_CANDIDATES


def changed(kind: str, status: str) -> str:
    """The status after a deliberate change, or ValueError when it doesn't apply to this status."""
    validate_status(status)
    if kind not in CHANGES:
        raise ValueError("Unknown desire change")
    allowed, after = CHANGES[kind]
    if status not in allowed:
        raise ValueError(f"A {status} desire can't {kind.replace('_', ' ')}")
    return after


def validate_count(count: int) -> None:
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("Desire count must be a non-negative integer")



@dataclass(frozen=True)
class Change:
    """One change a reflection proposes; the store re-checks it against the rules before applying it."""
    action: str  # one of ACTIONS
    note: str  # why
    desire_id: UUID | None = None  # reinforce and let_go
    description: str | None = None  # form
    motivation: str | None = None  # form
    strength: str | None = None  # reinforce: a key of REINFORCE_DELTAS
    interest_ids: tuple[UUID, ...] = ()  # form: interests it grew from
    evidence: tuple[Evidence, ...] = ()  # experiences it grew from (form) or that renew it (reinforce)
