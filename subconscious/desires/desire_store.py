"""Save Cora's desires and their history. Strength and status rules live in desire_rules.

Every write takes a table lock so the caps hold even if two writers race. Each write
settles the desire it touches first, so a desire that faded meanwhile is let go (with a
'fade' event) rather than reinforced or changed. Callers pass `now`, so one cycle uses one
consistent clock reading.

Run `python -m subconscious.desires.desire_store` to list her desires.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
import sys
from uuid import UUID

from dotenv import load_dotenv
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session

from storage.database import build_engine
from storage.models import DesireEventRecord, DesireRecord, DesireSourceRecord
from subconscious.desires.desire_rules import (
    ENDED, HELD, INITIAL_STRENGTH, MAX_REINFORCE_DELTA, can_form, changed, current_strength, promoted, settle,
)
from subconscious.interests.interest_strength import reinforce as reinforced

DESCRIPTION_MAX_CHARS = 300
TEXT_MAX_CHARS = 1000
FADE_NOTE = "Faded without anything reinforcing it"
HOLD_NOTE = "Wanted it enough to hold on to"


@dataclass(frozen=True)
class Desire:
    id: UUID
    description: str
    motivation: str
    strength: float  # snapshot at strength_at; use current_strength for the current value
    strength_at: datetime
    status: str
    created_at: datetime
    last_reinforced_at: datetime | None
    ended_at: datetime | None


@dataclass(frozen=True)
class DesireEvent:
    kind: str
    strength_before: float | None
    strength_after: float
    note: str
    created_at: datetime


@dataclass(frozen=True)
class Sources:
    """What a desire grew from."""
    interest_ids: tuple[UUID, ...] = ()
    exploration_ids: tuple[UUID, ...] = ()
    conversation_ids: tuple[UUID, ...] = ()


def clean_text(value: str, name: str, max_chars: int = TEXT_MAX_CHARS) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Desire {name} must not be blank")
    value = value.strip()
    if len(value) > max_chars:
        raise ValueError(f"Desire {name} must be at most {max_chars} characters")
    return value


def validate_now(now: datetime) -> None:
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")


def validate_delta(delta: float) -> None:
    if (isinstance(delta, bool) or not isinstance(delta, (int, float)) or not isfinite(delta)
            or not 0 < delta <= MAX_REINFORCE_DELTA):
        raise ValueError(f"Reinforcement must be above 0 and at most {MAX_REINFORCE_DELTA}")


def snapshot(record: DesireRecord) -> Desire:
    return Desire(
        record.id, record.description, record.motivation, record.strength, record.strength_at, record.status,
        record.created_at, record.last_reinforced_at, record.ended_at,
    )


class DesireStore:
    def __init__(self, engine: Engine):
        self.engine = engine

    def list_desires(self, *, include_ended: bool = False) -> list[Desire]:
        """Return stored snapshots, strongest first."""
        query = select(DesireRecord)
        if not include_ended:
            query = query.where(DesireRecord.status.not_in(ENDED))
        with Session(self.engine) as session:
            records = session.scalars(query.order_by(
                DesireRecord.strength.desc(), DesireRecord.created_at, DesireRecord.id,
            ))
            return [snapshot(record) for record in records]

    def history(self, desire_id: UUID) -> list[DesireEvent]:
        """A desire's events, oldest first."""
        with Session(self.engine) as session:
            records = session.scalars(select(DesireEventRecord).where(
                DesireEventRecord.desire_id == desire_id,
            ).order_by(DesireEventRecord.created_at, DesireEventRecord.id))
            return [DesireEvent(record.kind, record.strength_before, record.strength_after, record.note,
                                record.created_at) for record in records]

    def sources(self, desire_id: UUID) -> Sources:
        with Session(self.engine) as session:
            rows = session.scalars(select(DesireSourceRecord).where(
                DesireSourceRecord.desire_id == desire_id,
            ).order_by(DesireSourceRecord.id)).all()
        return Sources(
            tuple(row.interest_id for row in rows if row.interest_id is not None),
            tuple(row.exploration_id for row in rows if row.exploration_id is not None),
            tuple(row.conversation_id for row in rows if row.conversation_id is not None),
        )

    def form(
        self, description: str, motivation: str, note: str, *, now: datetime, sources: Sources = Sources(),
    ) -> Desire | None:
        """Form a candidate desire, or return None when the candidate cap is full."""
        description = clean_text(description, "description", DESCRIPTION_MAX_CHARS)
        motivation = clean_text(motivation, "motivation")
        note = clean_text(note, "note")
        validate_now(now)
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            self._lock(session)
            if not can_form(self._count(session, now, ("candidate",))):
                return None
            record = DesireRecord(
                description=description, motivation=motivation, strength=INITIAL_STRENGTH,
                strength_at=now, status="candidate",
            )
            session.add(record)
            session.flush()
            self._add_event(session, record.id, "form", None, record.strength, note)
            session.add_all(self._source_records(record.id, sources))
            return snapshot(record)

    def reinforce(self, desire_id: UUID, delta: float, note: str, *, now: datetime) -> Desire:
        """Settle then reinforce; a candidate strong enough becomes held when there is room.

        A desire that faded meanwhile is returned let go, unreinforced; an ended one raises.
        """
        validate_delta(delta)
        note = clean_text(note, "note")
        validate_now(now)
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            self._lock(session)
            record = self._settled(session, desire_id, now)
            if record.status in ENDED:
                return snapshot(record)
            after = reinforced(record.strength, delta)
            self._add_event(session, record.id, "reinforce", record.strength, after, note)
            record.strength, record.last_reinforced_at = after, now
            status = promoted(record.status, after, self._count(session, now, HELD, exclude_id=record.id))
            if status != record.status:
                self._add_event(session, record.id, "hold", after, after, HOLD_NOTE)
                record.status = status
            return snapshot(record)

    def change(self, desire_id: UUID, kind: str, note: str, *, now: datetime) -> Desire:
        """A deliberate status change: 'pursue', 'pause', 'fulfil' or 'let_go', with why.

        A desire that faded meanwhile is returned let go, unchanged; a change that doesn't
        apply to its status raises ValueError.
        """
        note = clean_text(note, "note")
        validate_now(now)
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            self._lock(session)
            record = self._settled(session, desire_id, now)
            if record.status == "let_go":
                return snapshot(record)
            record.status = changed(kind, record.status)
            if record.status in ENDED:
                record.ended_at = max(now, record.created_at)
            self._add_event(session, record.id, kind, record.strength, record.strength, note)
            return snapshot(record)

    def note_progress(self, desire_id: UUID, note: str, *, now: datetime) -> Desire:
        """Note what happened while she pursues a held desire; strength is unchanged."""
        note = clean_text(note, "note")
        validate_now(now)
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            self._lock(session)
            record = self._settled(session, desire_id, now)
            if record.status == "let_go":
                return snapshot(record)
            if record.status not in HELD:
                raise ValueError(f"A {record.status} desire can't have progress")
            self._add_event(session, record.id, "progress", record.strength, record.strength, note)
            return snapshot(record)

    def settle_all(self, now: datetime) -> list[Desire]:
        """Save faded strength for every live desire, letting go of any that faded away."""
        validate_now(now)
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            self._lock(session)
            records = list(session.scalars(select(DesireRecord).where(
                DesireRecord.status.not_in(ENDED)).order_by(DesireRecord.id)))
            for record in records:
                self._settle(session, record, now)
            return [snapshot(record) for record in records]

    def _settled(self, session: Session, desire_id: UUID, now: datetime) -> DesireRecord:
        record = session.get(DesireRecord, desire_id)
        if record is None:
            raise LookupError("Desire does not exist")
        self._settle(session, record, now)
        return record

    def _settle(self, session: Session, record: DesireRecord, now: datetime) -> None:
        if record.status in ENDED:
            return
        settled, status = settle(record.strength, record.strength_at, record.status, now)
        if status == "let_go":
            self._add_event(session, record.id, "fade", record.strength, settled, FADE_NOTE)
            record.ended_at = max(now, record.created_at)
        record.strength, record.status = settled, status
        record.strength_at = max(record.strength_at, now)

    @staticmethod
    def _count(session: Session, now: datetime, statuses: tuple[str, ...], exclude_id: UUID | None = None) -> int:
        """Count desires in these statuses that have not faded by now, so stale rows don't block new ones."""
        records = session.scalars(select(DesireRecord).where(DesireRecord.status.in_(statuses)))
        return sum(
            1 for record in records
            if record.id != exclude_id
            and settle(record.strength, record.strength_at, record.status, now)[1] != "let_go"
        )

    @staticmethod
    def _source_records(desire_id: UUID, sources: Sources) -> Iterable[DesireSourceRecord]:
        for interest_id in sorted(set(sources.interest_ids)):
            yield DesireSourceRecord(desire_id=desire_id, interest_id=interest_id)
        for exploration_id in sorted(set(sources.exploration_ids)):
            yield DesireSourceRecord(desire_id=desire_id, exploration_id=exploration_id)
        for conversation_id in sorted(set(sources.conversation_ids)):
            yield DesireSourceRecord(desire_id=desire_id, conversation_id=conversation_id)

    @staticmethod
    def _lock(session: Session) -> None:
        # Conflicts with itself and with plain writes, but not with reads.
        session.execute(text("LOCK TABLE public.desires IN SHARE ROW EXCLUSIVE MODE"))

    @staticmethod
    def _add_event(session: Session, desire_id: UUID, kind: str, before: float | None, after: float,
                   note: str) -> None:
        # clock_timestamp, not the transaction's start, so events written together keep their order.
        session.add(DesireEventRecord(
            desire_id=desire_id, kind=kind, strength_before=before, strength_after=after, note=note,
            created_at=func.clock_timestamp(),
        ))
        session.flush()


def main() -> None:
    load_dotenv()
    try:
        engine = build_engine()
        try:
            desires = DesireStore(engine).list_desires(include_ended=True)
        finally:
            engine.dispose()
    except Exception as error:
        sys.exit(f"Desires not loaded ({type(error).__name__}). Check the database and migrations.")
    if not desires:
        print("No desires yet.")
    now = datetime.now(timezone.utc)
    for desire in desires:
        strength = current_strength(desire.strength, desire.strength_at, now) if desire.status not in ENDED \
            else desire.strength
        print(f"{strength:.2f} {desire.status:<9} {desire.description}")
        print(f"     {desire.motivation}")


if __name__ == "__main__":
    main()
