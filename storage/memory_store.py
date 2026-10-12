"""Save evidence-linked memories without changing conversations or summaries."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from .models import ConversationRecord, MemoryRecord, MessageRecord


MEMORY_KINDS = ("fact", "preference", "decision", "task")


@dataclass(frozen=True)
class MemoryCandidate:
    source_message_id: UUID
    kind: str
    text: str
    evidence: str


class MemoryStore:
    def __init__(self, engine: Engine):
        self.engine = engine

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