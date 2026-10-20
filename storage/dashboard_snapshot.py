"""Everything the dashboard shows, read together off the UI thread."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Engine

from subconscious.relationship.relationship_rules import RelationshipValues
from subconscious.relationship.relationship_store import RelationshipStore


@dataclass(frozen=True)
class DashboardSnapshot:
    # Drifted to the load time, as Cora would feel it now; None before any update.
    relationship: RelationshipValues | None


def read_dashboard_snapshot(engine: Engine, now: datetime) -> DashboardSnapshot:
    return DashboardSnapshot(relationship=RelationshipStore(engine).current_values(now=now))
