"""Save Cora's interests and their history. Strength rules live in interest_strength.

Every write takes a table lock so the live-interest cap holds even if two writers
race. Callers pass `now`, so one cycle uses one consistent clock reading.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from math import isfinite
import sys
from typing import TYPE_CHECKING
from uuid import UUID

from dotenv import load_dotenv
from sqlalchemy import Engine, and_, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from storage.database import build_engine
from storage.models import (
    InterestEventMemoryRecord, InterestEventRecord, InterestRecord, InterestWorkerStateRecord,
    MemoryRecord, MessageRecord,
)
from subconscious.interest_strength import (
    INITIAL_STRENGTH, MAX_REINFORCE_DELTA, effective_strength, has_room,
    needs_room, next_status, reinforce, settle,
)

if TYPE_CHECKING:  # interest_proposals imports this module
    from subconscious.interest_proposals import Proposal

TITLE_MAX_CHARS = 200
TEXT_MAX_CHARS = 1000
FADE_REASON = "Faded without recent reinforcement"
MEMORY_BATCH_LIMIT = 50
# created_at is the saving transaction's start time, so a slow save can commit a row
# older than one already read. Skipping very recent rows lets those saves land first.
MEMORY_COMMIT_MARGIN = timedelta(minutes=5)
INTEREST_CONTEXT_MAX_CHARS = 3000

Cursor = tuple[datetime, UUID]


@dataclass(frozen=True)
class Interest:
    id: UUID
    title: str
    description: str
    why_emerged: str
    strength: float  # snapshot at strength_at; use effective_strength for the current value
    strength_at: datetime
    status: str
    parent_interest_id: UUID | None
    created_at: datetime
    last_reinforced_at: datetime | None


@dataclass(frozen=True)
class NewMemory:
    id: UUID
    kind: str
    text: str
    created_at: datetime


@dataclass(frozen=True)
class MemoryBatch:
    memories: tuple[NewMemory, ...]  # ready, non-duplicate memories, oldest first
    after: Cursor | None  # the saved cursor this batch was read from
    cursor: Cursor | None  # save this once the batch is processed; equals `after` if nothing new
    waiting: bool  # stopped at a memory that is not yet embedded and dedup-checked
    more: bool = False  # the batch filled its limit, so more memories may follow


@dataclass(frozen=True)
class Evolution:
    reinforced: tuple[Interest, ...]
    spawned: Interest | None
    refused: int  # changes skipped because the live-interest cap was full


def clean_text(value: str, name: str, max_chars: int = TEXT_MAX_CHARS) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Interest {name} must not be blank")
    value = value.strip()
    if len(value) > max_chars:
        raise ValueError(f"Interest {name} must be at most {max_chars} characters")
    return value


def validate_now(now: datetime) -> None:
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")


def validate_delta(delta: float) -> None:
    if (isinstance(delta, bool) or not isinstance(delta, (int, float)) or not isfinite(delta)
            or not 0 < delta <= MAX_REINFORCE_DELTA):
        raise ValueError(f"Reinforcement must be above 0 and at most {MAX_REINFORCE_DELTA}")


def snapshot(record: InterestRecord) -> Interest:
    return Interest(
        record.id, record.title, record.description, record.why_emerged, record.strength,
        record.strength_at, record.status, record.parent_interest_id, record.created_at,
        record.last_reinforced_at,
    )


class InterestStore:
    def __init__(self, engine: Engine):
        self.engine = engine

    def list_interests(self, *, include_dormant: bool = False) -> list[Interest]:
        """Return stored snapshots, strongest first."""
        query = select(InterestRecord)
        if not include_dormant:
            query = query.where(InterestRecord.status != "dormant")
        with Session(self.engine) as session:
            records = session.scalars(query.order_by(
                InterestRecord.strength.desc(), InterestRecord.created_at, InterestRecord.id,
            ))
            return [snapshot(record) for record in records]

    def get_startup_context(self, *, now: datetime) -> str:
        """Live interests as bounded JSON of whole records, strongest first; read-only."""
        validate_now(now)
        ranked = []
        for interest in self.list_interests():
            # Read-time settling only; saving decay is left to the evolution cycle.
            strength, status = settle(interest.strength, interest.strength_at, interest.status, now)
            if status != "dormant":
                ranked.append((strength, {
                    "title": interest.title, "description": interest.description, "status": status,
                }))
        ranked.sort(key=lambda item: item[0], reverse=True)
        selected, context = [], ""
        for _, record in ranked:
            candidate = json.dumps([*selected, record], ensure_ascii=False)
            if len(candidate) > INTEREST_CONTEXT_MAX_CHARS:
                break
            selected.append(record)
            context = candidate
        return context

    def read_new_memories(self, limit: int = MEMORY_BATCH_LIMIT) -> MemoryBatch:
        """Read memories after the saved cursor, stopping at the first one not yet ready.

        Stopping (rather than skipping) means a memory that is still being embedded or
        dedup-checked is never passed over; duplicates are passed over but not returned.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MEMORY_BATCH_LIMIT:
            raise ValueError(f"Memory batch limit must be an integer from 1 to {MEMORY_BATCH_LIMIT}")
        with Session(self.engine) as session:
            after = self._read_cursor(session)
            query = select(
                MemoryRecord.id, MemoryRecord.kind, MemoryRecord.text, MemoryRecord.created_at,
                MemoryRecord.embedding.is_not(None).label("embedded"),
                MemoryRecord.dedup_checked_at, MemoryRecord.duplicate_of,
            ).join(MessageRecord, MemoryRecord.source_message_id == MessageRecord.id).where(
                MessageRecord.role == "user",
                MemoryRecord.created_at < func.now() - MEMORY_COMMIT_MARGIN,
            )
            if after is not None:
                query = query.where(or_(MemoryRecord.created_at > after[0], and_(
                    MemoryRecord.created_at == after[0], MemoryRecord.id > after[1],
                )))
            rows = session.execute(query.order_by(MemoryRecord.created_at, MemoryRecord.id).limit(limit)).all()
        memories, cursor, waiting = [], after, False
        for row in rows:
            if not row.embedded or row.dedup_checked_at is None:
                waiting = True
                break
            cursor = (row.created_at, row.id)
            if row.duplicate_of is None:
                memories.append(NewMemory(row.id, row.kind, row.text, row.created_at))
        return MemoryBatch(tuple(memories), after, cursor, waiting, not waiting and len(rows) == limit)

    def advance_memory_cursor(self, batch: MemoryBatch) -> bool:
        """Save the batch's cursor if no one moved it since the batch was read."""
        with Session(self.engine) as session, session.begin():
            return self._advance_cursor(session, batch)

    def spawn(
        self, title: str, description: str, why_emerged: str, reason: str, *, now: datetime,
        parent_interest_id: UUID | None = None, memory_ids: Iterable[UUID] = (),
    ) -> Interest | None:
        """Create an emerging interest, or return None when the live cap is full."""
        title = clean_text(title, "title", TITLE_MAX_CHARS)
        description = clean_text(description, "description")
        why_emerged = clean_text(why_emerged, "why_emerged")
        reason = clean_text(reason, "reason")
        validate_now(now)
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            self._lock(session)
            return self._spawn(session, title, description, why_emerged, reason, now,
                               parent_interest_id, memory_ids)

    def reinforce(
        self, interest_id: UUID, delta: float, reason: str, *, now: datetime,
        memory_ids: Iterable[UUID] = (),
    ) -> Interest | None:
        """Settle then reinforce. Return None, changing nothing, if a revive would exceed the cap."""
        validate_delta(delta)
        reason = clean_text(reason, "reason")
        validate_now(now)
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            self._lock(session)
            return self._reinforce(session, interest_id, delta, reason, now, memory_ids)

    def settle_all(self, now: datetime) -> list[Interest]:
        """Save decayed strength and status for every interest, recording any fades."""
        validate_now(now)
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            self._lock(session)
            return self._settle_all(session, now)

    def last_evolved_at(self) -> datetime | None:
        with Session(self.engine) as session:
            state = session.get(InterestWorkerStateRecord, 1)
            return None if state is None else state.last_evolved_at

    def apply_evolution(self, batch: MemoryBatch, proposal: "Proposal", *, now: datetime) -> Evolution | None:
        """Settle, apply a validated proposal, move the cursor and stamp the cycle in ONE transaction.

        Returns None, changing nothing, if the cursor moved since the batch was read, so a
        retried or concurrent cycle can never reinforce from the same memories twice.
        """
        validate_now(now)
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            self._lock(session)
            if not self._advance_cursor(session, batch):
                return None
            self._settle_all(session, now)
            reinforced, refused = [], 0
            for change in proposal.reinforcements:
                interest = self._reinforce(session, change.interest_id, change.delta, change.reason,
                                           now, change.memory_ids)
                if interest is None:
                    refused += 1
                else:
                    reinforced.append(interest)
            spawned = None
            if proposal.spawn is not None:
                new = proposal.spawn
                spawned = self._spawn(session, new.title, new.description, new.why_emerged, new.reason,
                                      now, new.parent_interest_id, new.memory_ids)
                refused += spawned is None
            session.get(InterestWorkerStateRecord, 1).last_evolved_at = now
            return Evolution(tuple(reinforced), spawned, refused)

    def _spawn(
        self, session: Session, title: str, description: str, why_emerged: str, reason: str,
        now: datetime, parent_interest_id: UUID | None, memory_ids: Iterable[UUID],
    ) -> Interest | None:
        if not has_room(self._live_count(session, now)):
            return None
        record = InterestRecord(
            title=title, description=description, why_emerged=why_emerged,
            strength=INITIAL_STRENGTH, strength_at=now,
            status=next_status("emerging", INITIAL_STRENGTH), parent_interest_id=parent_interest_id,
        )
        session.add(record)
        session.flush()
        self._add_event(session, record.id, "spawn", None, record.strength, reason, memory_ids)
        return snapshot(record)

    def _reinforce(
        self, session: Session, interest_id: UUID, delta: float, reason: str, now: datetime,
        memory_ids: Iterable[UUID],
    ) -> Interest | None:
        record = session.get(InterestRecord, interest_id)
        if record is None:
            raise LookupError("Interest does not exist")
        settled, settled_status = settle(record.strength, record.strength_at, record.status, now)
        after = reinforce(settled, delta)
        status = next_status(settled_status, after)
        if needs_room(settled_status, status) and not has_room(
                self._live_count(session, now, exclude_id=record.id)):
            return None
        self._fade_if_needed(session, record, settled, settled_status)
        kind = "revive" if settled_status == "dormant" and status != "dormant" else "reinforce"
        self._add_event(session, record.id, kind, settled, after, reason, memory_ids)
        record.strength, record.status = after, status
        record.strength_at = max(record.strength_at, now)
        record.last_reinforced_at = now
        return snapshot(record)

    def _settle_all(self, session: Session, now: datetime) -> list[Interest]:
        records = list(session.scalars(select(InterestRecord).order_by(InterestRecord.id)))
        for record in records:
            settled, status = settle(record.strength, record.strength_at, record.status, now)
            self._fade_if_needed(session, record, settled, status)
            record.strength, record.status = settled, status
            record.strength_at = max(record.strength_at, now)
        return [snapshot(record) for record in records]

    @staticmethod
    def _lock(session: Session) -> None:
        # Conflicts with itself and with plain writes, but not with reads.
        session.execute(text("LOCK TABLE public.interests IN SHARE ROW EXCLUSIVE MODE"))

    @staticmethod
    def _read_cursor(session: Session) -> Cursor | None:
        state = session.get(InterestWorkerStateRecord, 1)
        if state is None or state.memory_cursor_id is None:
            return None
        return state.memory_cursor_created_at, state.memory_cursor_id

    @staticmethod
    def _advance_cursor(session: Session, batch: MemoryBatch) -> bool:
        """Compare-and-set inside the caller's transaction, so cursor and changes commit together."""
        if batch.cursor is None or batch.cursor == batch.after:
            return False
        session.execute(insert(InterestWorkerStateRecord).values(id=1).on_conflict_do_nothing())
        state = session.scalar(select(InterestWorkerStateRecord).with_for_update())
        current = None if state.memory_cursor_id is None else (
            state.memory_cursor_created_at, state.memory_cursor_id)
        if current != batch.after:
            return False
        state.memory_cursor_created_at, state.memory_cursor_id = batch.cursor
        state.updated_at = func.current_timestamp()
        return True

    @staticmethod
    def _live_count(session: Session, now: datetime, exclude_id: UUID | None = None) -> int:
        """Count interests that are live after decay, so stale stored statuses don't block growth."""
        records = session.scalars(select(InterestRecord).where(InterestRecord.status != "dormant"))
        return sum(
            1 for record in records
            if record.id != exclude_id
            and settle(record.strength, record.strength_at, record.status, now)[1] != "dormant"
        )

    def _fade_if_needed(self, session: Session, record: InterestRecord, settled: float, status: str) -> None:
        if record.status != "dormant" and status == "dormant":
            self._add_event(session, record.id, "fade", record.strength, settled, FADE_REASON, ())

    @staticmethod
    def _add_event(
        session: Session, interest_id: UUID, kind: str, before: float | None, after: float,
        reason: str, memory_ids: Iterable[UUID],
    ) -> None:
        event = InterestEventRecord(
            interest_id=interest_id, kind=kind, strength_before=before, strength_after=after, reason=reason,
        )
        session.add(event)
        session.flush()
        session.add_all(InterestEventMemoryRecord(event_id=event.id, memory_id=memory_id)
                        for memory_id in sorted(set(memory_ids)))


def main() -> None:
    load_dotenv()
    try:
        engine = build_engine()
        try:
            interests = InterestStore(engine).list_interests(include_dormant=True)
        finally:
            engine.dispose()
    except Exception as error:
        sys.exit(f"Interests not loaded ({type(error).__name__}). Check the database and migrations.")
    if not interests:
        print("No interests yet.")
    now = datetime.now(timezone.utc)
    for interest in interests:
        strength = effective_strength(interest.strength, interest.strength_at, now)
        print(f"{strength:.2f} {interest.status:<8} {interest.title}")
        print(f"     {interest.why_emerged}")


if __name__ == "__main__":
    main()
