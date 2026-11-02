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
import json
from math import isfinite
import sys
from uuid import UUID

from dotenv import load_dotenv
from sqlalchemy import Engine, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from storage.database import build_engine
from storage.models import (
    DesireEventRecord, DesireRecord, DesireSourceRecord, DesireWorkerStateRecord, InterestEventRecord,
)
from subconscious.desires.desire_rules import (
    ACTIONS, ENDED, HELD, INITIAL_STRENGTH, LIVE, MAX_REINFORCE_DELTA, REINFORCE_DELTAS, Change, can_form,
    changed, current_strength, promoted, settle,
)
from subconscious.identity.identity_rules import EVIDENCE_SOURCES
from subconscious.interests.interest_strength import reinforce as reinforced

DESCRIPTION_MAX_CHARS = 300
TEXT_MAX_CHARS = 1000
DESIRE_CONTEXT_MAX_CHARS = 2000
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


def clean_change(change: Change) -> Change:
    """Check a reflection's change is well formed; whether it's allowed is decided when applying it."""
    if change.action not in ACTIONS:
        raise ValueError("Unknown desire action")
    note = clean_text(change.note, "note")
    if any(item.source not in EVIDENCE_SOURCES for item in change.evidence):
        raise ValueError("Unknown experience source")
    if change.action == "form":
        if change.desire_id is not None or change.strength is not None:
            raise ValueError("A new desire has no id or strength")
        if not change.interest_ids and not change.evidence:
            raise ValueError("A new desire must say what it grew from")
        return Change("form", note, description=clean_text(change.description, "description", DESCRIPTION_MAX_CHARS),
                      motivation=clean_text(change.motivation, "motivation"),
                      interest_ids=change.interest_ids, evidence=change.evidence)
    if change.desire_id is None or change.description is not None or change.motivation is not None \
            or change.interest_ids:
        raise ValueError(f"To {change.action.replace('_', ' ')} a desire, give only its id, why and support")
    if change.action == "reinforce":
        if change.strength not in REINFORCE_DELTAS:
            raise ValueError("Unknown reinforcement strength")
        if not change.evidence:
            raise ValueError("A reinforcement must cite what renewed it")
    elif change.strength is not None or change.evidence:
        raise ValueError("Letting go takes only an id and why")
    return Change(change.action, note, desire_id=change.desire_id, strength=change.strength,
                  evidence=change.evidence)


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


def same_wish(left: str, right: str) -> bool:
    return " ".join(left.casefold().split()) == " ".join(right.casefold().split())


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

    def get_startup_context(self, *, now: datetime) -> str:
        """Held and pursued desires as bounded JSON, strongest first; read-only.

        Candidates are still forming, so they stay out, as traits still forming do. Whole
        records only: one that doesn't fit is left out rather than cut.
        """
        selected, context = [], ""
        for desire in self.held_desires(now=now):
            record = {"wish": desire.description, "why": desire.motivation, "status": desire.status}
            candidate = json.dumps([*selected, record], ensure_ascii=False)
            if len(candidate) <= DESIRE_CONTEXT_MAX_CHARS:
                selected.append(record)
                context = candidate
        return context

    def held_desires(self, *, now: datetime) -> list[Desire]:
        """Held and pursued desires that haven't faded by now, strongest first."""
        validate_now(now)
        held = [desire for desire in self.list_desires() if desire.status in HELD
                and settle(desire.strength, desire.strength_at, desire.status, now)[1] != "let_go"]
        return sorted(held, key=lambda desire: current_strength(desire.strength, desire.strength_at, now),
                      reverse=True)

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
            return snapshot(self._form(session, description, motivation, note, now, sources))

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
            if record.status not in ENDED:
                self._reinforce(session, record, delta, note, now)
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
            if record.status != "let_go":
                self._change(session, record, kind, note, now)
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

    def take_step(self, desire_id: UUID, note: str, *, now: datetime) -> Desire:
        """A step she took toward a held desire: the first starts pursuing it, later ones note progress.

        A desire that ended meanwhile (or faded) is returned unchanged.
        """
        note = clean_text(note, "note")
        validate_now(now)
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            self._lock(session)
            record = self._settled(session, desire_id, now)
            if record.status == "held":
                self._change(session, record, "pursue", note, now)
            elif record.status == "pursuing":
                self._add_event(session, record.id, "progress", record.strength, record.strength, note)
            return snapshot(record)

    def settle_all(self, now: datetime) -> list[Desire]:
        """Save faded strength for every live desire, letting go of any that faded away."""
        validate_now(now)
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            self._lock(session)
            return [snapshot(record) for record in self._settle_all(session, now)]

    def last_reflected_at(self) -> datetime | None:
        with Session(self.engine) as session:
            state = session.get(DesireWorkerStateRecord, 1)
            return None if state is None else state.last_reflected_at

    def apply_reflection(
        self, changes: Iterable[Change], *, read_after: datetime | None, read_until: datetime, now: datetime,
    ) -> list[str | None] | None:
        """Settle every desire, apply a reflection's changes and stamp it, in ONE transaction.

        `read_after` is the last_reflected_at the reflection started from and `read_until` how
        far it read; that becomes the new last_reflected_at. Returns, for each change in order,
        None if applied or why it was not; or None, saving nothing, if another reflection moved
        last_reflected_at first.
        """
        changes = [clean_change(change) for change in changes]
        validate_now(now)
        validate_now(read_until)
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            self._lock(session)
            session.execute(insert(DesireWorkerStateRecord).values(id=1).on_conflict_do_nothing())
            state = session.get(DesireWorkerStateRecord, 1, with_for_update=True)
            if state.last_reflected_at != read_after:
                return None
            self._settle_all(session, now)
            problems, touched = [], set()
            for change in changes:
                problem = self._apply(session, change, now, touched)
                problems.append(problem)
                if problem is None and change.desire_id is not None:
                    touched.add(change.desire_id)
            state.last_reflected_at, state.updated_at = read_until, func.current_timestamp()
            return problems

    def _apply(self, session: Session, change: Change, now: datetime, touched: set[UUID]) -> str | None:
        """Apply one reflection change if the rules allow it; otherwise say why not."""
        if change.action == "form":
            live = session.scalars(select(DesireRecord.description).where(DesireRecord.status.in_(LIVE)))
            if any(same_wish(description, change.description) for description in live):
                return "she already wants this"
            if not can_form(self._count(session, now, ("candidate",))):
                return "the candidate cap is full"
            self._form(session, change.description, change.motivation, change.note, now,
                       self._sources(session, change))
            return None
        if change.desire_id in touched:
            return "only one change per desire in a reflection"
        record = session.get(DesireRecord, change.desire_id)
        if record is None:
            return "no such desire"
        if record.status not in LIVE:
            return f"it is already {record.status.replace('_', ' ')}"
        if change.action == "reinforce":
            self._reinforce(session, record, REINFORCE_DELTAS[change.strength], change.note, now)
        else:
            self._change(session, record, "let_go", change.note, now)
        return None

    def _form(
        self, session: Session, description: str, motivation: str, note: str, now: datetime, sources: Sources,
    ) -> DesireRecord:
        record = DesireRecord(
            description=description, motivation=motivation, strength=INITIAL_STRENGTH,
            strength_at=now, status="candidate",
        )
        session.add(record)
        session.flush()
        self._add_event(session, record.id, "form", None, record.strength, note)
        session.add_all(self._source_records(record.id, sources))
        return record

    def _reinforce(self, session: Session, record: DesireRecord, delta: float, note: str, now: datetime) -> None:
        after = reinforced(record.strength, delta)
        self._add_event(session, record.id, "reinforce", record.strength, after, note)
        record.strength, record.last_reinforced_at = after, now
        status = promoted(record.status, after, self._count(session, now, HELD, exclude_id=record.id))
        if status != record.status:
            self._add_event(session, record.id, "hold", after, after, HOLD_NOTE)
            record.status = status

    def _change(self, session: Session, record: DesireRecord, kind: str, note: str, now: datetime) -> None:
        record.status = changed(kind, record.status)
        if record.status in ENDED:
            record.ended_at = max(now, record.created_at)
        self._add_event(session, record.id, kind, record.strength, record.strength, note)

    def _settled(self, session: Session, desire_id: UUID, now: datetime) -> DesireRecord:
        record = session.get(DesireRecord, desire_id)
        if record is None:
            raise LookupError("Desire does not exist")
        self._settle(session, record, now)
        return record

    def _settle_all(self, session: Session, now: datetime) -> list[DesireRecord]:
        records = list(session.scalars(select(DesireRecord).where(
            DesireRecord.status.not_in(ENDED)).order_by(DesireRecord.id)))
        for record in records:
            self._settle(session, record, now)
        return records

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
    def _sources(session: Session, change: Change) -> Sources:
        """What a new desire grew from; an interest change it cites counts as that interest."""
        interest_events = [item.id for item in change.evidence if item.source == "interest_event"]
        from_events = session.scalars(select(InterestEventRecord.interest_id).where(
            InterestEventRecord.id.in_(interest_events))).all() if interest_events else []
        return Sources(
            (*change.interest_ids, *from_events),
            tuple(item.id for item in change.evidence if item.source == "exploration"),
            tuple(item.id for item in change.evidence if item.source == "conversation"),
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
