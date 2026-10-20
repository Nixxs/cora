"""Everything the dashboard shows, read together off the UI thread."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from memory.store import MemoryStore
from storage.models import (
    ConversationSummaryRecord, IdentityWorkerStateRecord, InteractionAppraisalRecord,
    InterestExplorationRecord, InterestWorkerStateRecord, MemoryRecord, RelationshipEventRecord,
)
from subconscious.identity.reflect_identity import REFLECT_EVERY
from subconscious.interests.evolve_interests import EVOLVE_EVERY
from subconscious.interests.exploration_store import DAILY_LIMIT, MIN_GAP
from subconscious.interests.interest_store import InterestStore
from subconscious.interests.interest_strength import settle
from subconscious.relationship.relationship_rules import RelationshipValues
from subconscious.relationship.relationship_store import RelationshipStore

# More than the widest tone ribbon or graph will show.
HISTORY_LIMIT = 200

# An interest reinforced (or spawned) this recently counts as rising; otherwise it is
# only decaying, so it counts as falling.
RISING_WINDOW = timedelta(days=3)

# Background job -> the newest time it saved something, in the order the jobs run.
LAST_RUN_COLUMNS = {
    "summaries": ConversationSummaryRecord.created_at,
    "extraction": MemoryRecord.created_at,
    "embedding": MemoryRecord.embedding_created_at,
    "duplicate check": MemoryRecord.dedup_checked_at,
    "appraisal": InteractionAppraisalRecord.appraised_at,
    "relationship": RelationshipEventRecord.created_at,
    "interests": InterestWorkerStateRecord.last_evolved_at,
    "exploration": InterestExplorationRecord.started_at,
    "identity": IdentityWorkerStateRecord.last_reflected_at,
}


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
    # Background job -> when it last saved something, or None if it never has.
    last_runs: dict[str, datetime | None]
    # Timed job -> when its cooldown ends, or None if it has never run (due now).
    next_due: dict[str, datetime | None]


def last_runs(engine: Engine) -> dict[str, datetime | None]:
    # One round trip: each job's newest time as a scalar subquery.
    query = select(*(select(func.max(column)).scalar_subquery() for column in LAST_RUN_COLUMNS.values()))
    with Session(engine) as session:
        return dict(zip(LAST_RUN_COLUMNS, session.execute(query).one()))


def next_due(engine: Engine, runs: dict[str, datetime | None]) -> dict[str, datetime | None]:
    """When each timed job's cooldown ends; the app checks every 10 minutes after that."""
    started_at = InterestExplorationRecord.started_at
    with Session(engine) as session:
        starts = session.scalars(select(started_at).order_by(started_at.desc()).limit(DAILY_LIMIT)).all()
    exploration = None
    if starts:
        exploration = starts[0] + MIN_GAP
        if len(starts) == DAILY_LIMIT:
            # A full day's attempts: wait for the oldest of them to leave the 24 hours.
            exploration = max(exploration, starts[-1] + timedelta(hours=24))
    evolved, reflected = runs["interests"], runs["identity"]
    return {
        "interests": evolved + EVOLVE_EVERY if evolved else None,
        "exploration": exploration,
        "identity": reflected + REFLECT_EVERY if reflected else None,
    }


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
    runs = last_runs(engine)
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
        last_runs=runs,
        next_due=next_due(engine, runs),
    )
