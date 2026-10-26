"""Save evidence-linked memories without changing conversations or summaries."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from math import isfinite, log2
from uuid import UUID

from pgvector import Vector
from sqlalchemy import Engine, Float, and_, case, cast, exists, extract, func, or_, select
from sqlalchemy.orm import Session, aliased

from storage.models import (
    MEMORY_EMBEDDING_DIMENSIONS, MEMORY_EMBEDDING_MODEL,
    ConversationRecord, MemoryRecord, MessageRecord, PersonRecord,
)


MEMORY_KINDS = ("fact", "preference", "decision", "task")
STARTUP_MEMORY_LIMIT = 10
STARTUP_MEMORY_MAX_CHARS = 3000
MEMORY_SEARCH_MAX_RESULTS = 20
# Plans the person mentioned this recently can open a greeting ("how did it go?").
RECENT_PLAN_WINDOW = timedelta(days=7)
PLAN_KINDS = ("task", "decision")

# What the background embedding and deduplication passes pick up next, joined to source messages.
PENDING_EMBEDDING = (
    MemoryRecord.embedding.is_(None), MemoryRecord.duplicate_of.is_(None), MessageRecord.role == "user",
)
PENDING_DEDUPLICATION = (
    MemoryRecord.embedding.is_not(None), MemoryRecord.duplicate_of.is_(None),
    MemoryRecord.dedup_checked_at.is_(None), MessageRecord.role == "user",
    MemoryRecord.embedding_model == MEMORY_EMBEDDING_MODEL,
)
# Corrections come after the duplicate check; duplicates never correct anything.
PENDING_CORRECTION = (
    MemoryRecord.embedding.is_not(None), MemoryRecord.duplicate_of.is_(None),
    MemoryRecord.dedup_checked_at.is_not(None), MemoryRecord.correction_checked_at.is_(None),
    MessageRecord.role == "user", MemoryRecord.embedding_model == MEMORY_EMBEDDING_MODEL,
)
# Fading comes after the correction check, so it only reads memories that settled.
PENDING_FADE = (
    MemoryRecord.duplicate_of.is_(None), MemoryRecord.correction_checked_at.is_not(None),
    MemoryRecord.fade_checked_at.is_(None), MessageRecord.role == "user",
)
# Who said a memory: its source message's conversation's person, NULL for the primary person.
SPEAKER = select(ConversationRecord.person_id).where(
    ConversationRecord.id == MessageRecord.conversation_id,
).correlate(MessageRecord).scalar_subquery()
SAID_BY_PRIMARY = SPEAKER.is_(None)


def said_by(person_id: UUID | None):
    """Memories one person said: the primary person (None) or a client."""
    return SAID_BY_PRIMARY if person_id is None else SPEAKER == person_id


# Recall prefers the newer memory; a corrected one stays stored as history.
_newer = aliased(MemoryRecord)
NOT_CORRECTED = ~exists().where(_newer.corrects == MemoryRecord.id)
# Recall fades with age: a memory's weight halves every half-life, down to the floor.
# A dated memory whose day has passed counts as fully faded.
FADE_HALF_LIFE = timedelta(days=90)
FADE_FLOOR = 0.5
PASSED = func.coalesce(MemoryRecord.fades_at <= func.current_timestamp(), False)
NOT_PASSED = ~PASSED


def fade_weight(age: timedelta) -> float:
    """How much recall still counts a memory said this long ago, as search does."""
    return max(FADE_FLOOR, 0.5 ** (max(age, timedelta()) / FADE_HALF_LIFE))


def validate_search_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MEMORY_SEARCH_MAX_RESULTS:
        raise ValueError("Memory search limit must be an integer from 1 to 20")

def validate_memory_embedding(values: list[float]) -> list[float]:
    """Validate and normalise to the float32 values PostgreSQL actually stores."""
    if not isinstance(values, list) or len(values) != MEMORY_EMBEDDING_DIMENSIONS:
        raise ValueError("Wrong memory embedding dimensions")
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
        raise ValueError("Memory embedding must contain numeric values")
    try:
        vector = Vector(values).to_list()
    except (ValueError, OverflowError) as error:
        raise ValueError("Memory embedding cannot be represented as float32") from error
    if not all(isfinite(value) for value in vector) or not any(value != 0 for value in vector):
        raise ValueError("Memory embedding must be finite and nonzero")
    return vector


@dataclass(frozen=True)
class MemoryCandidate:
    source_message_id: UUID
    kind: str
    text: str
    evidence: str


@dataclass(frozen=True)
class MemoryMatch:
    memory_id: UUID
    kind: str
    text: str
    source_message_id: UUID
    conversation_id: UUID
    source_created_at: datetime
    distance: float
    faded: bool
    # The client who said it, or None for the primary person.
    said_by: str | None = None


@dataclass(frozen=True)
class DuplicateCandidate:
    memory_id: UUID
    text: str
    evidence: str
    source_created_at: datetime


class MemoryStore:
    def __init__(self, engine: Engine):
        self.engine = engine

    def search_memories(self, query_embedding: list[float], limit: int = 8) -> list[MemoryMatch]:
        """Return the closest embedded user memories, newer ones ranked ahead of faded ones."""
        validate_search_limit(limit)
        vector = validate_memory_embedding(query_embedding)
        distance = MemoryRecord.embedding.cosine_distance(vector)
        half_lives = cast(extract("epoch", func.current_timestamp() - MessageRecord.created_at), Float) / (
            FADE_HALF_LIFE.total_seconds())
        weight = case((PASSED, FADE_FLOOR), else_=func.power(
            0.5, func.least(func.greatest(half_lives, 0.0), log2(1 / FADE_FLOOR)),
        ))
        with Session(self.engine) as session:
            speaker_name = select(PersonRecord.display_name).where(
                PersonRecord.id == SPEAKER,
            ).correlate(MessageRecord).scalar_subquery()
            rows = session.execute(select(
                MemoryRecord.id, MemoryRecord.kind, MemoryRecord.text,
                MemoryRecord.source_message_id, MessageRecord.conversation_id,
                MessageRecord.created_at, distance.label("distance"), PASSED.label("faded"),
                speaker_name.label("said_by"),
            ).join(MessageRecord, MemoryRecord.source_message_id == MessageRecord.id).where(
                MemoryRecord.embedding.is_not(None),
                MemoryRecord.embedding_model == MEMORY_EMBEDDING_MODEL,
                MemoryRecord.duplicate_of.is_(None),
                MessageRecord.role == "user", NOT_CORRECTED,
            ).order_by(((1 - distance) * weight).desc(), MemoryRecord.id).limit(limit)).all()
            return [MemoryMatch(*row) for row in rows]

    def get_memory(self, memory_id: UUID) -> MemoryRecord | None:
        with Session(self.engine) as session:
            return session.get(MemoryRecord, memory_id)

    def kind_counts(self) -> dict[str, int]:
        """How many memories of each kind, not counting duplicates."""
        with Session(self.engine) as session:
            rows = session.execute(
                select(MemoryRecord.kind, func.count()).where(MemoryRecord.duplicate_of.is_(None))
                .group_by(MemoryRecord.kind)
            ).all()
        return {**dict.fromkeys(MEMORY_KINDS, 0), **dict(rows)}

    def daily_counts(self, now: datetime, days: int) -> list[int]:
        """New memories per local day the person said them, oldest first, ending today.

        Duplicates are not counted.
        """
        first = now.astimezone().date() - timedelta(days=days - 1)
        with Session(self.engine) as session:
            said_at = session.scalars(select(MessageRecord.created_at).join(
                MemoryRecord, MemoryRecord.source_message_id == MessageRecord.id,
            ).where(
                MemoryRecord.duplicate_of.is_(None), MessageRecord.created_at >= now - timedelta(days=days + 1),
            )).all()
        counts = [0] * days
        for time in said_at:
            day = (time.astimezone().date() - first).days
            if 0 <= day < days:
                counts[day] += 1
        return counts

    def recent_plan(self, now: datetime) -> str | None:
        """One random task or decision the person mentioned within the last week, if any."""
        with Session(self.engine) as session:
            return session.scalar(select(MemoryRecord.text).join(
                MessageRecord, MemoryRecord.source_message_id == MessageRecord.id,
            ).where(
                MemoryRecord.kind.in_(PLAN_KINDS), MemoryRecord.duplicate_of.is_(None),
                MessageRecord.role == "user", MessageRecord.created_at >= now - RECENT_PLAN_WINDOW,
                SAID_BY_PRIMARY,
                NOT_CORRECTED, or_(MemoryRecord.fades_at.is_(None), MemoryRecord.fades_at > now),
            ).order_by(func.random()).limit(1))

    def get_pending_embedding_ids(self) -> list[UUID]:
        with Session(self.engine) as session:
            return list(session.scalars(select(MemoryRecord.id).join(
                MessageRecord, MemoryRecord.source_message_id == MessageRecord.id,
            ).where(*PENDING_EMBEDDING).order_by(MemoryRecord.created_at, MemoryRecord.id).limit(100)))

    def get_pending_deduplication_ids(self) -> list[UUID]:
        with Session(self.engine) as session:
            return list(session.scalars(select(MemoryRecord.id).join(
                MessageRecord, MemoryRecord.source_message_id == MessageRecord.id,
            ).where(*PENDING_DEDUPLICATION).order_by(MemoryRecord.created_at, MemoryRecord.id).limit(100)))

    def get_pending_correction_ids(self) -> list[UUID]:
        with Session(self.engine) as session:
            return list(session.scalars(select(MemoryRecord.id).join(
                MessageRecord, MemoryRecord.source_message_id == MessageRecord.id,
            ).where(*PENDING_CORRECTION).order_by(MemoryRecord.created_at, MemoryRecord.id).limit(100)))

    def get_pending_fade_ids(self) -> list[UUID]:
        with Session(self.engine) as session:
            return list(session.scalars(select(MemoryRecord.id).join(
                MessageRecord, MemoryRecord.source_message_id == MessageRecord.id,
            ).where(*PENDING_FADE).order_by(MemoryRecord.created_at, MemoryRecord.id).limit(100)))

    def pending_counts(self) -> dict[str, int]:
        """How many memories wait at each background step."""
        with Session(self.engine) as session:
            return {
                name: session.scalar(select(func.count()).select_from(MemoryRecord).join(
                    MessageRecord, MemoryRecord.source_message_id == MessageRecord.id,
                ).where(*conditions))
                for name, conditions in (
                    ("embedding", PENDING_EMBEDDING), ("duplicate check", PENDING_DEDUPLICATION),
                    ("correction check", PENDING_CORRECTION), ("fade check", PENDING_FADE),
                )
            }

    def get_source_created_at(self, source_message_id: UUID) -> datetime:
        with Session(self.engine) as session:
            return session.execute(select(MessageRecord.created_at).where(
                MessageRecord.id == source_message_id,
            )).scalar_one()

    def get_duplicate_candidates(self, memory: MemoryRecord) -> list[DuplicateCandidate]:
        return self._nearest_earlier_memories(memory)

    def get_correction_candidates(self, memory: MemoryRecord) -> list[DuplicateCandidate]:
        """Like duplicate candidates, but skipping memories a newer one already corrects."""
        return self._nearest_earlier_memories(memory, NOT_CORRECTED)

    def _nearest_earlier_memories(self, memory: MemoryRecord, *conditions) -> list[DuplicateCandidate]:
        """Only the same person's memories: Sarah's home city never corrects or duplicates the person's."""
        vector = validate_memory_embedding(memory.embedding)
        with Session(self.engine) as session:
            person_id = session.scalar(select(SPEAKER).select_from(MessageRecord).where(
                MessageRecord.id == memory.source_message_id,
            ))
            rows = session.execute(select(
                MemoryRecord.id, MemoryRecord.text, MemoryRecord.evidence, MessageRecord.created_at,
            ).join(MessageRecord, MemoryRecord.source_message_id == MessageRecord.id).where(
                MemoryRecord.kind == memory.kind, MemoryRecord.duplicate_of.is_(None),
                MemoryRecord.embedding_model == MEMORY_EMBEDDING_MODEL,
                MemoryRecord.embedding.is_not(None), MessageRecord.role == "user",
                or_(MemoryRecord.created_at < memory.created_at, and_(
                    MemoryRecord.created_at == memory.created_at, MemoryRecord.id < memory.id,
                )),
                said_by(person_id),
                *conditions,
            ).order_by(MemoryRecord.embedding.cosine_distance(vector), MemoryRecord.id).limit(5)).all()
            return [DuplicateCandidate(*row) for row in rows]

    def save_deduplication(
        self, memory_id: UUID, source_text: str, duplicate_id: UUID | None,
        target_text: str | None = None,
    ) -> bool:
        ids = [memory_id] if duplicate_id is None else [memory_id, duplicate_id]
        with Session(self.engine) as session, session.begin():
            rows = {row.id: row for row in session.scalars(select(MemoryRecord).where(
                MemoryRecord.id.in_(ids),
            ).order_by(MemoryRecord.id).with_for_update())}
            memory = rows.get(memory_id)
            if memory is None:
                raise LookupError("Memory does not exist")
            if memory.text != source_text:
                raise ValueError("Memory changed during deduplication")
            if memory.dedup_checked_at is not None:
                return memory.duplicate_of is not None
            if duplicate_id is not None:
                target = rows.get(duplicate_id)
                if (target is None or target.id == memory.id or target.duplicate_of is not None
                        or target.text != target_text or target.kind != memory.kind
                        or (target.created_at, target.id) >= (memory.created_at, memory.id)):
                    raise ValueError("Duplicate target is not an earlier unchanged canonical memory")
                memory.duplicate_of = duplicate_id
            memory.dedup_checked_at = func.current_timestamp()
            return duplicate_id is not None

    def save_correction(
        self, memory_id: UUID, source_text: str, corrected_id: UUID | None,
        target_text: str | None = None,
    ) -> bool:
        ids = [memory_id] if corrected_id is None else [memory_id, corrected_id]
        with Session(self.engine) as session, session.begin():
            rows = {row.id: row for row in session.scalars(select(MemoryRecord).where(
                MemoryRecord.id.in_(ids),
            ).order_by(MemoryRecord.id).with_for_update())}
            memory = rows.get(memory_id)
            if memory is None:
                raise LookupError("Memory does not exist")
            if memory.text != source_text:
                raise ValueError("Memory changed during correction check")
            if memory.correction_checked_at is not None:
                return memory.corrects is not None
            if corrected_id is not None:
                target = rows.get(corrected_id)
                if (target is None or target.id == memory.id or target.duplicate_of is not None
                        or target.text != target_text or target.kind != memory.kind
                        or (target.created_at, target.id) >= (memory.created_at, memory.id)
                        or session.scalar(select(exists().where(MemoryRecord.corrects == corrected_id)))):
                    raise ValueError("Corrected memory is not an earlier unchanged current memory")
                memory.corrects = corrected_id
            memory.correction_checked_at = func.current_timestamp()
            return corrected_id is not None

    def save_fade(self, memory_id: UUID, source_text: str, fades_at: datetime | None) -> datetime | None:
        """Save when a memory fades (None = lasting); a repeat returns the first decision."""
        with Session(self.engine) as session, session.begin():
            memory = session.scalar(select(MemoryRecord).where(
                MemoryRecord.id == memory_id,
            ).with_for_update())
            if memory is None:
                raise LookupError("Memory does not exist")
            if memory.text != source_text:
                raise ValueError("Memory changed during fade check")
            if memory.fade_checked_at is not None:
                return memory.fades_at
            memory.fades_at = fades_at
            memory.fade_checked_at = func.current_timestamp()
            return fades_at

    def save_embedding(self, memory_id: UUID, source_text: str, values: list[float]) -> MemoryRecord:
        vector = validate_memory_embedding(values)
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            memory = session.scalar(select(MemoryRecord).where(
                MemoryRecord.id == memory_id,
            ).with_for_update())
            if memory is None:
                raise LookupError("Memory does not exist")
            if memory.text != source_text:
                raise ValueError("Memory text changed during embedding generation")
            if memory.embedding is not None:
                if (memory.embedding_model != MEMORY_EMBEDDING_MODEL
                        or validate_memory_embedding(memory.embedding) != vector):
                    raise ValueError("Memory already has a different embedding")
                return memory
            memory.embedding = vector
            memory.embedding_model = MEMORY_EMBEDDING_MODEL
            memory.embedding_created_at = func.current_timestamp()
            session.flush()
            session.refresh(memory)
            return memory

    def get_startup_context(self, person_id: UUID | None = None) -> str:
        """Read at most ten ranked memories one person said; return bounded, whole JSON records.

        None is the primary person; a client's id gives what she remembers from their own chats.
        """
        with Session(self.engine) as session:
            rows = session.execute(
                select(MemoryRecord.kind, MemoryRecord.text, MessageRecord.created_at).join(
                    MessageRecord, MemoryRecord.source_message_id == MessageRecord.id,
                ).where(
                    MessageRecord.role == "user", MemoryRecord.duplicate_of.is_(None), NOT_CORRECTED,
                    NOT_PASSED, said_by(person_id),
                ).order_by(
                    case((MemoryRecord.kind.in_(("fact", "preference")), 0), else_=1),
                    MessageRecord.created_at.desc(), MemoryRecord.id.desc(),
                ).limit(STARTUP_MEMORY_LIMIT)
            ).all()
        selected = []
        context = ""
        for kind, text, recorded_at in rows:
            record = {"kind": kind, "text": text,
                      "source_date": recorded_at.astimezone(timezone.utc).date().isoformat()}
            candidate = json.dumps([*selected, record], ensure_ascii=False)
            if len(candidate) > STARTUP_MEMORY_MAX_CHARS:
                break
            selected.append(record)
            context = candidate
        return context

    def get_memories(self, conversation_id: UUID) -> list[MemoryRecord]:
        """Return this conversation's memories; source messages identify the chat."""
        with Session(self.engine) as session:
            return list(session.scalars(
                select(MemoryRecord).join(
                    MessageRecord, MemoryRecord.source_message_id == MessageRecord.id,
                ).where(MessageRecord.conversation_id == conversation_id).order_by(
                    MessageRecord.sequence_number, MemoryRecord.kind, MemoryRecord.text,
                )
            ).all())

    def save_memories(self, conversation_id: UUID, candidates: list[MemoryCandidate]) -> list[MemoryRecord]:
        """Commit the entire validated batch; exact retries never overwrite rows."""
        if not candidates:
            return []
        for candidate in candidates:
            if candidate.kind not in MEMORY_KINDS:
                raise ValueError("Unknown memory kind")
            if not candidate.text.strip() or len(candidate.text) > 500:
                raise ValueError("Memory text must contain 1 to 500 characters")
            if not candidate.evidence.strip():
                raise ValueError("Memory evidence must not be blank")
        with Session(self.engine) as session, session.begin():
            conversation = session.scalar(select(ConversationRecord).where(
                ConversationRecord.id == conversation_id,
            ).with_for_update())
            if conversation is None:
                raise LookupError("Conversation does not exist")
            if conversation.status not in ("closed", "interrupted"):
                raise ValueError("Conversation must be closed or interrupted before extracting memories")
            for candidate in candidates:
                source = session.get(MessageRecord, candidate.source_message_id)
                if source is None or source.conversation_id != conversation_id or source.role != "user":
                    raise ValueError("Memory source must be a user message in this conversation")
                if candidate.evidence not in source.text:
                    raise ValueError("Memory evidence must be an exact quote from its source")
                existing = session.scalar(select(MemoryRecord).where(
                    MemoryRecord.source_message_id == candidate.source_message_id,
                    MemoryRecord.kind == candidate.kind, MemoryRecord.text == candidate.text,
                ))
                if existing is None:
                    session.add(MemoryRecord(
                        source_message_id=candidate.source_message_id, kind=candidate.kind,
                        text=candidate.text, evidence=candidate.evidence,
                    ))
                    session.flush()
        return self.get_memories(conversation_id)