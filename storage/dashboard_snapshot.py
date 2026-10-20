"""Everything the dashboard shows, read together off the UI thread."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Engine

from subconscious.relationship.relationship_rules import RelationshipValues
from subconscious.relationship.relationship_store import RelationshipStore

# More than the widest tone ribbon or history graph will show.
HISTORY_LIMIT = 200


@dataclass(frozen=True)
class DashboardSnapshot:
    # Drifted to the load time, as Cora would feel it now; None before any update.
    relationship: RelationshipValues | None
    # The values after each recent relationship change, oldest first.
    history: tuple[RelationshipValues, ...]
    # How the person treated Cora in recent everyday conversations, oldest first.
    tones: tuple[str, ...]


def read_dashboard_snapshot(engine: Engine, now: datetime) -> DashboardSnapshot:
    relationships = RelationshipStore(engine)
    return DashboardSnapshot(
        relationship=relationships.current_values(now=now),
        history=tuple(
            RelationshipValues(event.trust, event.warmth, event.familiarity)
            for event in relationships.history()[-HISTORY_LIMIT:]
        ),
        tones=tuple(relationships.recent_tones(HISTORY_LIMIT)),
    )
