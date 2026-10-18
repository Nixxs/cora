"""Apply saved appraisals to each person's relationship, once each, in conversation order.

    python -m subconscious.relationship.relationship_store [show | history [--json] |
        reset --reason TEXT [--to EVENT_ID] [--dry-run]]
"""

import argparse
from collections.abc import Callable
from dataclasses import asdict, fields
from datetime import datetime
import json
import sys
from uuid import UUID

from dotenv import load_dotenv
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from storage.database import build_engine
from storage.models import (
    ConversationRecord, InteractionAppraisalRecord, PersonRecord, RelationshipEventRecord, RelationshipRecord,
)
from subconscious.relationship.appraisal import Appraisal
from subconscious.relationship.relationship_context import describe_relationship
from subconscious.relationship.relationship_rules import BASELINE, RelationshipValues, apply_appraisal, drift

RESET_REASON_MAX_CHARS = 1000


def current_person_id():
    """The current person is the earliest met, matching PeopleStore.current_person."""
    return (select(PersonRecord.id).order_by(PersonRecord.first_met_at, PersonRecord.id)
            .limit(1).scalar_subquery())


def clean_reason(reason: str) -> str:
    reason = reason.strip()
    if not reason or len(reason) > RESET_REASON_MAX_CHARS:
        raise ValueError(f"Reset reason must be 1 to {RESET_REASON_MAX_CHARS} characters")
    return reason


class RelationshipStore:
    """Use a caller-owned engine; each operation owns and closes its session."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def current_values(self, now: datetime) -> RelationshipValues | None:
        """The current person's values after drifting to now; None before any update."""
        with Session(self.engine) as session:
            relationship = session.scalar(
                select(RelationshipRecord).where(RelationshipRecord.person_id == current_person_id())
            )
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

    def history(self) -> list[RelationshipEventRecord]:
        """The current person's relationship events, oldest first; each is a snapshot."""
        with Session(self.engine) as session:
            return list(session.scalars(
                select(RelationshipEventRecord).where(RelationshipEventRecord.person_id == current_person_id())
                .order_by(RelationshipEventRecord.created_at, RelationshipEventRecord.id)
            ))

    def apply_waiting_appraisals(self) -> list[RelationshipEventRecord]:
        """Apply every appraisal not yet applied, oldest conversation first; return the new events.

        A relationship's clock is its latest conversation's start: trust and warmth drift
        over the time between conversations, so a first-run catch-up matches living it.
        Conversations that started before the person's latest reset are never applied.
        """
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            waiting = session.execute(
                select(InteractionAppraisalRecord, ConversationRecord.started_at)
                .join(ConversationRecord, ConversationRecord.id == InteractionAppraisalRecord.conversation_id)
                .where(~select(RelationshipEventRecord.id).where(
                    RelationshipEventRecord.conversation_id == InteractionAppraisalRecord.conversation_id
                ).exists())
                .where(~select(RelationshipEventRecord.id).where(
                    RelationshipEventRecord.person_id == InteractionAppraisalRecord.person_id,
                    RelationshipEventRecord.kind == "reset",
                    RelationshipEventRecord.created_at >= ConversationRecord.started_at,
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

    def reset(self, *, reason: str, now: datetime, to_event_id: UUID | None = None) -> RelationshipEventRecord:
        """Restore the current person's trust and warmth to baseline or an earlier event.

        Familiarity is shared history, so it stays. Appends a reset event and deletes
        nothing; appraisals of conversations started before it are never applied.
        """
        reason = clean_reason(reason)
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            person_id = session.scalar(select(current_person_id()))
            if person_id is None:
                raise LookupError("No person to reset a relationship with")
            relationship = session.get(RelationshipRecord, person_id, with_for_update=True)
            if relationship is None:
                relationship = RelationshipRecord(person_id=person_id, updated_at=now, **asdict(BASELINE))
                session.add(relationship)
                session.flush()
            target = BASELINE
            if to_event_id is not None:
                target = session.get(RelationshipEventRecord, to_event_id)
                if target is None or target.person_id != person_id:
                    raise LookupError("No such relationship event for the current person")
            relationship.trust, relationship.warmth = target.trust, target.warmth
            relationship.updated_at = max(relationship.updated_at, now)
            event = RelationshipEventRecord(
                person_id=person_id, kind="reset", reason=reason, created_at=now,
                trust=relationship.trust, warmth=relationship.warmth, familiarity=relationship.familiarity,
            )
            session.add(event)
            session.flush()
        return event


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


def format_values(values: RelationshipValues) -> str:
    return f"trust={values.trust:.2f} warmth={values.warmth:.2f} familiarity={values.familiarity:.2f}"


def event_values(event: RelationshipEventRecord) -> RelationshipValues:
    return RelationshipValues(event.trust, event.warmth, event.familiarity)


def event_json(event: RelationshipEventRecord) -> dict:
    return {
        "id": str(event.id), "kind": event.kind, "created_at": event.created_at.isoformat(),
        "conversation_id": event.conversation_id and str(event.conversation_id), "reason": event.reason,
        **asdict(event_values(event)),
    }


def reset_command(store: RelationshipStore, args, now: datetime) -> list[str]:
    reason = clean_reason(args.reason)
    before = store.current_values(now) or BASELINE
    target = BASELINE
    if args.to is not None:
        target = next((event for event in store.history() if event.id == args.to), None)
        if target is None:
            raise LookupError("No such relationship event for the current person")
    after = RelationshipValues(target.trust, target.warmth, before.familiarity)
    print(f"Now:   {format_values(before)}\nAfter: {format_values(after)}\nReason: {reason}")
    if args.dry_run:
        return ["Dry run: nothing saved."]
    try:
        answer = input('Type "reset" to save this reset: ')
    except EOFError:
        answer = ""
    if answer.strip() != "reset":
        return ["Cancelled: nothing saved."]
    event = store.reset(reason=reason, now=now, to_event_id=args.to)
    return [f"Reset saved as event {event.id}. A running Cora keeps her current feeling "
            "until she next connects."]


def main() -> None:
    parser = argparse.ArgumentParser(description="Show, export or reset how Cora feels about the person.")
    parser.set_defaults(command="show")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("show", help="current values and how they read in her prompt")
    history = commands.add_parser("history", help="every relationship event, oldest first")
    history.add_argument("--json", action="store_true", help="export the events as JSON")
    reset = commands.add_parser("reset", help="restore trust and warmth; history is kept")
    reset.add_argument("--reason", required=True)
    reset.add_argument("--to", type=UUID, help="restore an earlier event's values instead of the baseline")
    reset.add_argument("--dry-run", action="store_true", help="show the change without saving it")
    args = parser.parse_args()
    load_dotenv()
    now = datetime.now().astimezone()
    try:
        engine = build_engine()
        try:
            store = RelationshipStore(engine)
            if args.command == "reset":
                lines = reset_command(store, args, now)
            elif args.command == "history":
                events = store.history()
                if args.json:
                    lines = [json.dumps([event_json(event) for event in events], indent=2, ensure_ascii=False)]
                else:
                    lines = [f"{event.id}  {event.created_at.astimezone():%Y-%m-%d %H:%M}  {event.kind:<9} "
                             f"{format_values(event_values(event))}"
                             + (f"\n     {event.reason}" if event.reason else "") for event in events]
                    lines = lines or ["No relationship events yet."]
            else:
                values = store.current_values(now)
                lines = (["No relationship yet."] if values is None
                         else [format_values(values), describe_relationship(values)])
        finally:
            engine.dispose()
    except (LookupError, ValueError) as error:
        sys.exit(f"{error}.")
    except Exception as error:
        sys.exit(f"Relationship not loaded ({type(error).__name__}). Check the database and migrations.")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
