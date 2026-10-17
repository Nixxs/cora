"""Apply saved appraisals to each person's relationship, once each, in conversation order."""

from collections.abc import Callable
from dataclasses import asdict, fields
from datetime import datetime

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from storage.models import (
    ConversationRecord, InteractionAppraisalRecord, PersonRecord, RelationshipEventRecord, RelationshipRecord,
)
from subconscious.relationship.appraisal import Appraisal
from subconscious.relationship.relationship_context import describe_relationship
from subconscious.relationship.relationship_rules import BASELINE, RelationshipValues, apply_appraisal, drift


class RelationshipStore:
    """Use a caller-owned engine; each operation owns and closes its session."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def current_values(self, now: datetime) -> RelationshipValues | None:
        """The current person's values after drifting to now; None before any update."""
        with Session(self.engine) as session:
            relationship = session.scalar(select(RelationshipRecord).where(
                RelationshipRecord.person_id == select(PersonRecord.id)
                .order_by(PersonRecord.first_met_at, PersonRecord.id).limit(1).scalar_subquery()
            ))
        if relationship is None:
            return None
        return drift(
            RelationshipValues(relationship.trust, relationship.warmth, relationship.familiarity),
            now - relationship.updated_at,
        )

    def get_startup_context(self, now: datetime) -> str:
        """Describe how Cora feels about the current person as of now; empty before any update."""
        values = self.current_values(now)
        return "" if values is None else describe_relationship(values)

    def apply_waiting_appraisals(self) -> list[RelationshipEventRecord]:
        """Apply every appraisal not yet applied, oldest conversation first; return the new events.

        A relationship's clock is its latest conversation's start: trust and warmth drift
        over the time between conversations, so a first-run catch-up matches living it.
        """
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            waiting = session.execute(
                select(InteractionAppraisalRecord, ConversationRecord.started_at)
                .join(ConversationRecord, ConversationRecord.id == InteractionAppraisalRecord.conversation_id)
                .where(~select(RelationshipEventRecord.id).where(
                    RelationshipEventRecord.conversation_id == InteractionAppraisalRecord.conversation_id
                ).exists())
                .order_by(ConversationRecord.started_at, ConversationRecord.id)
            ).all()
            relationships = {}
            events = []
            for record, started_at in waiting:
                relationship = relationships.get(record.person_id)
                if relationship is None:
                    relationship = session.get(RelationshipRecord, record.person_id, with_for_update=True)
                    if relationship is None:
                        relationship = RelationshipRecord(
                            person_id=record.person_id, updated_at=started_at, **asdict(BASELINE),
                        )
                        session.add(relationship)
                        session.flush()
                    relationships[record.person_id] = relationship
                values = drift(
                    RelationshipValues(relationship.trust, relationship.warmth, relationship.familiarity),
                    started_at - relationship.updated_at,
                )
                values = apply_appraisal(
                    values, Appraisal(**{field.name: getattr(record, field.name) for field in fields(Appraisal)}),
                )
                relationship.trust, relationship.warmth, relationship.familiarity = (
                    values.trust, values.warmth, values.familiarity,
                )
                relationship.updated_at = max(relationship.updated_at, started_at)
                event = RelationshipEventRecord(
                    person_id=record.person_id, kind="appraisal", conversation_id=record.conversation_id,
                    **asdict(values),
                )
                session.add(event)
                events.append(event)
            session.flush()
        return events


def update_relationships(engine: Engine, *, write_activity: Callable[[str, str], None]) -> None:
    """Background step: apply waiting appraisals and log the result; failures retry next pass."""
    try:
        events = RelationshipStore(engine).apply_waiting_appraisals()
    except Exception as error:
        write_activity(f"Relationship update stopped ({type(error).__name__}).", "Relationship update failed")
        return
    if events:
        latest = events[-1]
        write_activity(
            f"Relationship updated from {len(events)} conversation(s): trust={latest.trust:.2f}, "
            f"warmth={latest.warmth:.2f}, familiarity={latest.familiarity:.2f}",
            "Relationship updated",
        )
