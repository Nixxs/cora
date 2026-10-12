"""Small synchronous persistence service, independent of Cora and its UI."""

from uuid import UUID

from sqlalchemy import Engine, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from models import ConversationRecord, MessageRecord


class ConversationStore:
    """Use a caller-owned engine; each operation owns and closes its session."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def create_conversation(self, conversation_id: UUID) -> ConversationRecord:
        """Commit a conversation, or return the existing one on a same-ID retry."""
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            session.execute(
                insert(ConversationRecord).values(id=conversation_id).on_conflict_do_nothing(
                    index_elements=[ConversationRecord.id]
                )
            )
            conversation = session.get(ConversationRecord, conversation_id)
            assert conversation is not None
        return conversation

    def get_conversation(self, conversation_id: UUID) -> ConversationRecord | None:
        """Return loaded scalar fields; use get_messages() for the history."""
        with Session(self.engine) as session:
            return session.get(ConversationRecord, conversation_id)

    def save_message(
        self,
        conversation_id: UUID,
        *,
        message_id: UUID,
        role: str,
        text: str,
        channel: str,
        status: str = "incomplete",
        delivery_status: str = "not_applicable",
    ) -> MessageRecord:
        """Commit one message; identical retries preserve its text/order/time."""
        if not text.strip():
            raise ValueError("Message text must not be blank")
        values = {
            "conversation_id": conversation_id,
            "role": role,
            "text": text,
            "channel": channel,
            "status": status,
            "delivery_status": delivery_status,
        }
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            # One lock protects both same-ID retries and sequence allocation.
            conversation = session.scalar(
                select(ConversationRecord).where(
                    ConversationRecord.id == conversation_id
                ).with_for_update()
            )
            if conversation is None:
                raise LookupError(f"Conversation {conversation_id} does not exist")
            message = session.get(MessageRecord, message_id)
            if message is not None:
                if any(getattr(message, key) != value for key, value in values.items()):
                    raise ValueError("Message ID already used with different content or metadata")
            else:
                last_sequence = session.scalar(
                    select(func.max(MessageRecord.sequence_number)).where(
                        MessageRecord.conversation_id == conversation_id
                    )
                ) or 0
                message = MessageRecord(
                    id=message_id, sequence_number=last_sequence + 1, **values
                )
                session.add(message)
                session.flush()
        return message

    def set_delivery_status(
        self, conversation_id: UUID, message_id: UUID, delivery_status: str
    ) -> MessageRecord:
        """Commit a delivery result without changing the saved text/order/time."""
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            message = session.scalar(
                update(MessageRecord).where(
                    MessageRecord.conversation_id == conversation_id,
                    MessageRecord.id == message_id,
                ).values(delivery_status=delivery_status).returning(MessageRecord)
            )
            if message is None:
                raise LookupError("Message does not exist in this conversation")
        return message

    def get_messages(self, conversation_id: UUID) -> list[MessageRecord]:
        """Return history in conversation order (an empty list if none exists)."""
        with Session(self.engine) as session:
            return list(session.scalars(
                select(MessageRecord).where(
                    MessageRecord.conversation_id == conversation_id
                ).order_by(MessageRecord.sequence_number)
            ).all())