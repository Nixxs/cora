"""The person's most recent messages with Cora, across their conversations, for the web page."""

from datetime import datetime

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from .models import WITH_PRIMARY_PERSON, ConversationRecord, MessageRecord


def recent_person_messages(engine: Engine, limit: int) -> list[tuple[str, str, str, datetime, str | None]]:
    """(role, channel, text, created_at, device), oldest first: the person's own conversations
    only, never a client's, and not desk messages that failed to send. device is the paired device
    a web message came from, else None."""
    with Session(engine) as session:
        rows = session.execute(
            select(MessageRecord.role, MessageRecord.channel, MessageRecord.text, MessageRecord.created_at,
                   MessageRecord.device)
            .join(ConversationRecord, MessageRecord.conversation_id == ConversationRecord.id)
            .where(WITH_PRIMARY_PERSON, MessageRecord.delivery_status != "failed")
            .order_by(MessageRecord.created_at.desc(), MessageRecord.sequence_number.desc())
            .limit(limit)
        ).all()
    return [tuple(row) for row in reversed(rows)]
