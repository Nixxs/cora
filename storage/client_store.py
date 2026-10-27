"""People Cora may talk with on Signal as clients, and their own conversations with her.

Clients are people rows of kind 'client'. Their chats are conversations with their
person_id, never mixed into the conversation with the primary person.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import Engine, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .conversation_store import ConversationStore
from .models import ConversationRecord, MessageRecord, PersonRecord
from .signal_settings_store import normalise_number

NAME_MAX_CHARS = 100
# A quieter gap than this starts a new conversation, as a long standby does with the primary person.
CONVERSATION_GAP = timedelta(hours=3)
# History each reply carries, as with the primary person's chat.
HISTORY_MESSAGES = 40
HISTORY_BYTES = 6000


@dataclass(frozen=True)
class Client:
    id: UUID
    name: str
    number: str
    # Their Signal ID once learned; messages go to it, so a hidden number doesn't matter.
    signal_id: str | None = None

    @property
    def recipient(self) -> str:
        return self.signal_id or self.number


def clean_name(name: str) -> str:
    name = " ".join(name.split())
    if not name or len(name) > NAME_MAX_CHARS:
        raise ValueError(f"A name needs 1 to {NAME_MAX_CHARS} characters")
    return name


class ClientStore:
    """Use a caller-owned engine; each operation owns and closes its session."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def clients(self) -> list[Client]:
        """Everyone she may currently talk with, by name."""
        with Session(self.engine) as session:
            rows = session.execute(select(
                PersonRecord.id, PersonRecord.display_name, PersonRecord.signal_number, PersonRecord.signal_id,
            ).where(
                PersonRecord.kind == "client", PersonRecord.signal_number.is_not(None),
            ).order_by(PersonRecord.display_name, PersonRecord.id)).all()
        return [Client(*row) for row in rows]

    def add(self, name: str, number: str) -> Client:
        name, number = clean_name(name), normalise_number(number)
        try:
            with Session(self.engine) as session, session.begin():
                person = PersonRecord(display_name=name, kind="client", signal_number=number)
                session.add(person)
                session.flush()
                return Client(person.id, name, number)
        except IntegrityError:
            raise ValueError("Someone with that number is already added") from None

    def remove(self, person_id: UUID) -> None:
        """She stops answering them; their past conversations are kept."""
        with Session(self.engine) as session, session.begin():
            # The ID goes too, so adding them again later is a fresh entry.
            session.execute(update(PersonRecord).where(
                PersonRecord.id == person_id, PersonRecord.kind == "client",
            ).values(signal_number=None, signal_id=None))

    def set_signal_id(self, person_id: UUID, signal_id: str) -> None:
        with Session(self.engine) as session, session.begin():
            session.execute(update(PersonRecord).where(
                PersonRecord.id == person_id, PersonRecord.kind == "client",
            ).values(signal_id=signal_id))

    def conversation_for(self, person_id: UUID, now: datetime) -> UUID:
        """Their current conversation, or a new one after a long quiet (closing the old one)."""
        with Session(self.engine) as session, session.begin():
            current = session.execute(
                select(ConversationRecord.id, func.max(MessageRecord.created_at))
                .outerjoin(MessageRecord, MessageRecord.conversation_id == ConversationRecord.id)
                .where(ConversationRecord.person_id == person_id, ConversationRecord.status == "active")
                .group_by(ConversationRecord.id, ConversationRecord.started_at)
                .order_by(ConversationRecord.started_at.desc()).limit(1)
            ).first()
            if current is not None and current[1] is not None and now - current[1] < CONVERSATION_GAP:
                return current[0]
            session.execute(update(ConversationRecord).where(
                ConversationRecord.person_id == person_id, ConversationRecord.status == "active",
            ).values(status="closed", ended_at=func.current_timestamp()))
            conversation = ConversationRecord(id=uuid4(), person_id=person_id)
            session.add(conversation)
            session.flush()
            return conversation.id

    def save(self, conversation_id: UUID, role: str, text: str) -> None:
        ConversationStore(self.engine).save_message(
            conversation_id, message_id=uuid4(), role=role, text=text, channel="signal", status="completed",
        )

    def history(self, conversation_id: UUID) -> list[dict]:
        """Recent messages of this conversation as backend input, oldest first, within the usual bounds."""
        with Session(self.engine) as session:
            rows = session.execute(select(MessageRecord.role, MessageRecord.text).where(
                MessageRecord.conversation_id == conversation_id,
            ).order_by(MessageRecord.sequence_number.desc()).limit(HISTORY_MESSAGES)).all()
        history, remaining = [], HISTORY_BYTES
        for role, text in rows:
            text = text.encode("utf-8")[-remaining:].decode("utf-8", errors="ignore")
            if not text:
                break
            history.append({
                "type": "message", "role": role,
                "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}],
            })
            remaining -= len(text.encode("utf-8"))
            if remaining <= 0:
                break
        return list(reversed(history))
