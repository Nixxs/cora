"""Pure interest-strength rules: time decay, reinforcement and status changes.

No database, model, or clock access: callers pass in timestamps. A stored strength
is only a snapshot at `strength_at`. The store settles (saves the decayed value and
status with a new `strength_at`) only during an evolution cycle or when an event
touches an interest. Decay counts at most MAX_COUNTED_ELAPSED between settlements,
so one long absence cannot erase an interest.

Only conversation memories add strength. Cora's own exploration records findings
but gives no strength, so an interest cannot keep itself alive without the user.
"""

from datetime import datetime, timedelta
from math import isfinite

INITIAL_STRENGTH = 0.2
ACTIVE_THRESHOLD = 0.25  # emerging/dormant interests become active at or above this
DORMANT_THRESHOLD = 0.1  # emerging/active interests become dormant below this
HALF_LIFE = timedelta(days=7)
MAX_COUNTED_ELAPSED = timedelta(days=14)
MAX_LIVE_INTERESTS = 5  # emerging + active; dormant interests do not count
MAX_REINFORCE_DELTA = 0.15  # largest single reinforcement the store accepts

STATUSES = ("emerging", "active", "dormant")


def validate_strength(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise ValueError("Strength must be a finite number")
    if not 0 <= value <= 1:
        raise ValueError("Strength must be from 0 to 1")
    return float(value)


def effective_strength(
    strength: float, strength_at: datetime, now: datetime, *,
    half_life: timedelta = HALF_LIFE, max_elapsed: timedelta = MAX_COUNTED_ELAPSED,
) -> float:
    """Return the stored strength decayed by elapsed wall-clock time."""
    validate_strength(strength)
    if strength_at.tzinfo is None or now.tzinfo is None:
        raise ValueError("Timestamps must be timezone-aware")
    if half_life <= timedelta(0):
        raise ValueError("Half-life must be positive")
    elapsed = min(max(now - strength_at, timedelta(0)), max_elapsed)
    return strength * 0.5 ** (elapsed / half_life)


def reinforce(strength: float, delta: float) -> float:
    """Move toward 1 by a fraction of the remaining room, so strong interests saturate."""
    validate_strength(strength)
    validate_strength(delta)
    return strength + delta * (1 - strength)


def next_status(status: str, strength: float) -> str:
    """Apply hysteresis: values between the thresholds keep the current status."""
    if status not in STATUSES:
        raise ValueError("Unknown interest status")
    validate_strength(strength)
    if strength < DORMANT_THRESHOLD:
        return "dormant"
    if strength >= ACTIVE_THRESHOLD:
        return "active"
    # Weak but not faded: an emerging interest stays emerging; others keep their state.
    return status


def settle(strength: float, strength_at: datetime, status: str, now: datetime) -> tuple[float, str]:
    """Return the decayed strength and status to save with `now` as the new strength_at.

    Decay only lowers strength, so settling can fade an interest but never revives a
    dormant one; reviving goes through reinforcement and the live-interest cap.
    """
    settled = effective_strength(strength, strength_at, now)
    if status == "dormant":
        return settled, "dormant"
    return settled, next_status(status, settled)


def needs_room(status_before: str | None, status_after: str) -> bool:
    """A spawn (no previous status) or a dormant interest becoming live adds a live interest."""
    if status_before is not None and status_before not in STATUSES:
        raise ValueError("Unknown interest status")
    if status_after not in STATUSES:
        raise ValueError("Unknown interest status")
    return status_after != "dormant" and status_before in (None, "dormant")


def has_room(live_count: int) -> bool:
    """Whether another interest may become live. Reinforcing a live interest never needs room."""
    if isinstance(live_count, bool) or not isinstance(live_count, int) or live_count < 0:
        raise ValueError("Live interest count must be a non-negative integer")
    return live_count < MAX_LIVE_INTERESTS
