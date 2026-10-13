"""Save evidence-linked memories without changing conversations or summaries."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from math import isfinite
from uuid import UUID

from pgvector import Vector
from sqlalchemy import Engine, and_, case, func, or_, select
from sqlalchemy.orm import Session

from storage.models import (
    MEMORY_EMBEDDING_DIMENSIONS, MEMORY_EMBEDDING_MODEL,
    ConversationRecord, MemoryRecord, MessageRecord,
)


MEMORY_KINDS = ("fact", "preference", "decision", "task")
STARTUP_MEMORY_LIMIT = 10
STARTUP_MEMORY_MAX_CHARS = 3000
MEMORY_SEARCH_MAX_RESULTS = 20


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
        """Return nearest embedded user memories, not vectors or full chat histories."""
        validate_search_limit(limit)
        vector = validate_memory_embedding(query_embedding)
        distance = MemoryRecord.embedding.cosine_distance(vector).label("distance")
        with Session(self.engine) as session:
            rows = session.execute(select(
                MemoryRecord.id, MemoryRecord.kind, MemoryRecord.text,
                MemoryRecord.source_message_id, MessageRecord.conversation_id,
                MessageRecord.created_at, distance,
            ).join(MessageRecord, MemoryRecord.source_message_id == MessageRecord.id).where(
                MemoryRecord.embedding.is_not(None),
                MemoryRecord.embedding_model == MEMORY_EMBEDDING_MODEL,
                MemoryRecord.duplicate_of.is_(None),
                MessageRecord.role == "user",
            ).order_by(distance, MemoryRecord.id).limit(limit)).all()
            return [MemoryMatch(*row) for row in rows]

    def get_memory(self, memory_id: UUID) -> MemoryRecord | None:
        with Session(self.engine) as session:
            return session.get(MemoryRecord, memory_id)

    def get_pending_embedding_ids(self) -> list[UUID]:
        with Session(self.engine) as session:
            return list(session.scalars(select(MemoryRecord.id).where(
                MemoryRecord.embedding.is_(None), MemoryRecord.duplicate_of.is_(None),
            ).join(MessageRecord, MemoryRecord.source_message_id == MessageRecord.id).where(
                MessageRecord.role == "user",
            ).order_by(MemoryRecord.created_at, MemoryRecord.id).limit(100)))

    def get_pending_deduplication_ids(self) -> list[UUID]:
        with Session(self.engine) as session:
            return list(session.scalars(select(MemoryRecord.id).where(
                MemoryRecord.embedding.is_not(None), MemoryRecord.duplicate_of.is_(None),
                MemoryRecord.dedup_checked_at.is_(None),
            ).join(MessageRecord, MemoryRecord.source_message_id == MessageRecord.id).where(
                MessageRecord.role == "user", MemoryRecord.embedding_model == MEMORY_EMBEDDING_MODEL,
            ).order_by(MemoryRecord.created_at, MemoryRecord.id).limit(100)))

    def get_source_created_at(self, source_message_id: UUID) -> datetime:
        with Session(self.engine) as session:
            return session.execute(select(MessageRecord.created_at).where(
                MessageRecord.id == source_message_id,
            )).scalar_one()

    def get_duplicate_candidates(self, memory: MemoryRecord) -> list[DuplicateCandidate]:
        vector = validate_memory_embedding(memory.embedding)
        with Session(self.engine) as session:
            rows = session.execute(select(
                MemoryRecord.id, MemoryRecord.text, MemoryRecord.evidence, MessageRecord.created_at,
            ).join(MessageRecord, MemoryRecord.source_message_id == MessageRecord.id).where(
                MemoryRecord.kind == memory.kind, MemoryRecord.duplicate_of.is_(None),
                MemoryRecord.embedding_model == MEMORY_EMBEDDING_MODEL,
                MemoryRecord.embedding.is_not(None), MessageRecord.role == "user",
                or_(MemoryRecord.created_at < memory.created_at, and_(
                    MemoryRecord.created_at == memory.created_at, MemoryRecord.id < memory.id,
                )),
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

    def get_startup_context(self) -> str:
        """Read at most ten ranked memories; return bounded, whole JSON records."""
        with Session(self.engine) as session:
            rows = session.execute(
                select(MemoryRecord.kind, MemoryRecord.text, MessageRecord.created_at).join(
                    MessageRecord, MemoryRecord.source_message_id == MessageRecord.id,
                ).where(MessageRecord.role == "user", MemoryRecord.duplicate_of.is_(None)).order_by(
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