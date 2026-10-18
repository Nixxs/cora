"""Save Cora's identity traits and their history. The rules live in identity_rules.

Every write locks identity_traits and re-checks the rules against the traits as
they are now, so a reflection based on a stale read can never break a limit.
Callers pass `now`, so one reflection uses one consistent clock reading.

    python -m subconscious.identity.identity_store [list [--all] | history ID | retire ID --reason TEXT]
"""

import argparse
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime
import json
import sys
from uuid import UUID

from dotenv import load_dotenv
from sqlalchemy import Engine, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from storage.database import build_engine
from storage.models import (
    IdentityEventEvidenceRecord, IdentityEventRecord, IdentityEventTraitRecord,
    IdentityTraitRecord, IdentityWorkerStateRecord,
)
from subconscious.identity.identity_rules import Change, Trait, check_reflection, is_held
from subconscious.interests.interest_store import validate_now

STATEMENT_MAX_CHARS = 300
SUBJECT_MAX_CHARS = 200
REASON_MAX_CHARS = 1000
# Fits all MAX_HELD statements; only whole statements are ever sent.
IDENTITY_CONTEXT_MAX_CHARS = 4000
EVIDENCE_COLUMNS = {
    "interest_event": "interest_event_id", "exploration": "exploration_id", "conversation": "conversation_id",
}


@dataclass(frozen=True)
class Event:
    id: UUID
    trait_id: UUID
    kind: str  # form, revise or retire
    statement: str  # the trait's wording after this event
    confidence_before: float | None
    confidence_after: float
    reason: str
    created_at: datetime


def clean(value: str | None, name: str, max_chars: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Identity {name} must not be blank")
    value = value.strip()
    if len(value) > max_chars:
        raise ValueError(f"Identity {name} must be at most {max_chars} characters")
    return value


def clean_change(change: Change) -> Change:
    if change.reason is None:
        raise ValueError("Identity reason is required")
    return replace(
        change, reason=clean(change.reason, "reason", REASON_MAX_CHARS),
        statement=clean(change.statement, "statement", STATEMENT_MAX_CHARS),
        subject=clean(change.subject, "subject", SUBJECT_MAX_CHARS),
    )


def snapshot(record: IdentityTraitRecord) -> Trait:
    return Trait(record.id, record.kind, record.statement, record.polarity, record.subject,
                 record.confidence, record.status)


class IdentityStore:
    def __init__(self, engine: Engine):
        self.engine = engine

    def list_traits(self, *, include_retired: bool = False) -> list[Trait]:
        """Active traits (optionally retired too), most confident first."""
        query = select(IdentityTraitRecord)
        if not include_retired:
            query = query.where(IdentityTraitRecord.status == "active")
        with Session(self.engine) as session:
            records = session.scalars(query.order_by(
                IdentityTraitRecord.status, IdentityTraitRecord.confidence.desc(),
                IdentityTraitRecord.created_at, IdentityTraitRecord.id,
            ))
            return [snapshot(record) for record in records]

    def get_startup_context(self) -> str:
        """Held trait statements as bounded JSON, most confident first; read-only."""
        statements, context = [], ""
        for trait in self.list_traits():
            if not is_held(trait):
                continue
            candidate = json.dumps([*statements, trait.statement], ensure_ascii=False)
            if len(candidate) > IDENTITY_CONTEXT_MAX_CHARS:
                break
            statements.append(trait.statement)
            context = candidate
        return context

    def history(self, trait_id: UUID) -> list[Event]:
        """One trait's events, oldest first."""
        with Session(self.engine) as session:
            if session.get(IdentityTraitRecord, trait_id) is None:
                raise LookupError("Trait does not exist")
            records = session.scalars(select(IdentityEventRecord).where(
                IdentityEventRecord.trait_id == trait_id,
            ).order_by(IdentityEventRecord.created_at, IdentityEventRecord.id))
            return [Event(record.id, record.trait_id, record.kind, record.statement, record.confidence_before,
                          record.confidence_after, record.reason, record.created_at) for record in records]

    def last_value_change_at(self) -> datetime | None:
        with Session(self.engine) as session:
            return self._last_value_change_at(session)

    def last_reflected_at(self) -> datetime | None:
        with Session(self.engine) as session:
            state = session.get(IdentityWorkerStateRecord, 1)
            return None if state is None else state.last_reflected_at

    def apply_reflection(
        self, changes: Iterable[Change], *, read_after: datetime | None, read_until: datetime, now: datetime,
    ) -> list[str | None] | None:
        """Re-check and apply a reflection's changes and stamp it, in ONE transaction.

        `read_after` is the last_reflected_at the reflection started from and `read_until`
        how far it read; that becomes the new last_reflected_at. Returns, for each change
        in order, None if applied or why it was not; or None, saving nothing, if another
        reflection moved last_reflected_at first.
        """
        changes = [clean_change(change) for change in changes]
        validate_now(now)
        validate_now(read_until)
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            self._lock(session)
            session.execute(insert(IdentityWorkerStateRecord).values(id=1).on_conflict_do_nothing())
            state = session.get(IdentityWorkerStateRecord, 1, with_for_update=True)
            if state.last_reflected_at != read_after:
                return None
            records = {record.id: record for record in session.scalars(
                select(IdentityTraitRecord).where(IdentityTraitRecord.status == "active"))}
            problems = check_reflection(
                changes, [snapshot(record) for record in records.values()],
                last_value_change_at=self._last_value_change_at(session), now=now,
            )
            for change, problem in zip(changes, problems):
                if problem is None:
                    self._apply(session, records.get(change.trait_id), change, now)
            state.last_reflected_at, state.updated_at = read_until, func.current_timestamp()
            return problems

    def retire(self, trait_id: UUID, reason: str, *, now: datetime) -> Trait | None:
        """Retire a trait by hand, keeping its history; None if it was already retired."""
        change = clean_change(Change("retire", reason, trait_id=trait_id))
        validate_now(now)
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            self._lock(session)
            record = session.get(IdentityTraitRecord, trait_id)
            if record is None:
                raise LookupError("Trait does not exist")
            if record.status != "active":
                return None
            self._apply(session, record, change, now)
            return snapshot(record)

    @staticmethod
    def _apply(session: Session, record: IdentityTraitRecord | None, change: Change, now: datetime) -> None:
        before = None if change.action == "form" else record.confidence
        if change.action != "form":
            # Never stamp an event before the trait's last one, even if the clock moved back.
            now = max(now, session.scalar(select(func.max(IdentityEventRecord.created_at)).where(
                IdentityEventRecord.trait_id == record.id)) or record.created_at)
        if change.action == "form":
            record = IdentityTraitRecord(
                kind=change.kind, statement=change.statement, polarity=change.polarity,
                subject=change.subject, confidence=change.confidence, created_at=now,
            )
            session.add(record)
            session.flush()
        elif change.action == "revise":
            record.statement, record.polarity, record.confidence = (
                change.statement, change.polarity, change.confidence)
        else:
            record.status, record.retired_at = "retired", max(now, record.created_at)
        event = IdentityEventRecord(
            trait_id=record.id, kind=change.action, statement=record.statement, confidence_before=before,
            confidence_after=record.confidence, reason=change.reason, created_at=now,
        )
        session.add(event)
        session.flush()
        session.add_all(IdentityEventTraitRecord(event_id=event.id, trait_id=trait_id)
                        for trait_id in sorted(set(change.cited_trait_ids)))
        session.add_all(IdentityEventEvidenceRecord(event_id=event.id, **{EVIDENCE_COLUMNS[item.source]: item.id})
                        for item in dict.fromkeys(change.evidence))

    @staticmethod
    def _lock(session: Session) -> None:
        # Conflicts with itself and with plain writes, but not with reads.
        session.execute(text("LOCK TABLE public.identity_traits IN SHARE ROW EXCLUSIVE MODE"))

    @staticmethod
    def _last_value_change_at(session: Session) -> datetime | None:
        return session.scalar(select(func.max(IdentityEventRecord.created_at)).join(
            IdentityTraitRecord, IdentityTraitRecord.id == IdentityEventRecord.trait_id,
        ).where(IdentityTraitRecord.kind == "value"))


def describe(trait: Trait) -> str:
    label = "retired" if trait.status == "retired" else "held" if is_held(trait) else "forming"
    kind = f"{trait.kind} ({trait.polarity} {trait.subject})" if trait.polarity else trait.kind
    return f"{trait.id}  {label:<7} {trait.confidence:.2f}  {kind}: {trait.statement}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Show Cora's identity traits, or retire one by hand.")
    parser.set_defaults(command="list", all=False)
    commands = parser.add_subparsers(dest="command")
    listing = commands.add_parser("list", help="active traits, most confident first")
    listing.add_argument("--all", action="store_true", help="include retired traits")
    history = commands.add_parser("history", help="how one trait formed and changed")
    history.add_argument("trait_id", type=UUID)
    retire = commands.add_parser("retire", help="retire a trait; its history is kept")
    retire.add_argument("trait_id", type=UUID)
    retire.add_argument("--reason", required=True)
    args = parser.parse_args()
    load_dotenv()
    try:
        engine = build_engine()
        try:
            store = IdentityStore(engine)
            if args.command == "history":
                lines = [f"{event.created_at:%Y-%m-%d %H:%M} {event.kind:<6} {event.confidence_after:.2f}  "
                         f"{event.statement}\n     {event.reason}" for event in store.history(args.trait_id)]
            elif args.command == "retire":
                trait = store.retire(args.trait_id, args.reason, now=datetime.now().astimezone())
                lines = ["Already retired." if trait is None else describe(trait)]
            else:
                lines = [describe(trait) for trait in store.list_traits(include_retired=args.all)]
                lines = lines or ["No traits yet."]
        finally:
            engine.dispose()
    except LookupError:
        sys.exit("No such trait.")
    except ValueError as error:
        sys.exit(str(error))
    except Exception as error:
        sys.exit(f"Identity not loaded ({type(error).__name__}). Check the database and migrations.")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
