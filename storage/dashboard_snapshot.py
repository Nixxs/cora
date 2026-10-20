"""Everything the dashboard shows, read together off the UI thread."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Engine

from memory.store import MemoryStore
from subconscious.interests.interest_store import InterestStore
from subconscious.interests.interest_strength import settle
from subconscious.relationship.relationship_rules import RelationshipValues
from subconscious.relationship.relationship_store import RelationshipStore

# More than the widest tone ribbon or graph will show.
HISTORY_LIMIT = 200

# An interest reinforced (or spawned) this recently counts as rising; otherwise it is
# only decaying, so it counts as falling.
RISING_WINDOW = timedelta(days=3)


@dataclass(frozen=True)
class DashboardSnapshot:
    # Drifted to the load time, as Cora would feel it now; None before any update.
    relationship: RelationshipValues | None
    # The values after each recent relationship change, oldest first.
    history: tuple[RelationshipValues, ...]
    # How the person treated Cora in recent everyday conversations, oldest first.
    tones: tuple[str, ...]
    # Memory kind -> how many, not counting duplicates.
    memory_kinds: dict[str, int]
    # New memories per local day, oldest first, ending today.
    memory_days: tuple[int, ...]
    # Background step -> how many memories wait for it.
    memory_pending: dict[str, int]
    # (title, strength decayed to the load time, rising) for each live interest, strongest first.
    interests: tuple[tuple[str, float, bool], ...]
    # (interest title, finding) for the newest finding on any live interest, or None.
    latest_finding: tuple[str, str] | None


def live_interests(engine: Engine, now: datetime) -> tuple[
    tuple[tuple[str, float, bool], ...], tuple[str, str] | None,
]:
    """The live interests, strongest first, and the newest finding on any of them."""
    store = InterestStore(engine)
    live = {}
    for interest in store.list_interests():
        # Read-time settling only, like Cora's startup context; nothing is saved.
        strength, status = settle(interest.strength, interest.strength_at, interest.status, now)
        if status != "dormant":
            grown_at = interest.last_reinforced_at or interest.created_at
            live[interest.id] = (interest.title, strength, now - grown_at <= RISING_WINDOW)
    findings = store.latest_findings(list(live)) if live else {}
    # Each finding is (text, found_at); keep the most recently found.
    newest = max(findings.items(), key=lambda item: item[1][1], default=None)
    latest = (live[newest[0]][0], newest[1][0]) if newest else None
    return tuple(sorted(live.values(), key=lambda item: item[1], reverse=True)), latest


def read_dashboard_snapshot(engine: Engine, now: datetime) -> DashboardSnapshot:
    relationships = RelationshipStore(engine)
    memories = MemoryStore(engine)
    interests, latest_finding = live_interests(engine, now)
    return DashboardSnapshot(
        relationship=relationships.current_values(now=now),
        history=tuple(
            RelationshipValues(event.trust, event.warmth, event.familiarity)
            for event in relationships.history()[-HISTORY_LIMIT:]
        ),
        tones=tuple(relationships.recent_tones(HISTORY_LIMIT)),
        memory_kinds=memories.kind_counts(),
        memory_days=tuple(memories.daily_counts(now, HISTORY_LIMIT)),
        memory_pending=memories.pending_counts(),
        interests=interests,
        latest_finding=latest_finding,
    )
