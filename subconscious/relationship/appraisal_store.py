"""Save appraisals of finished conversations; once saved, an appraisal is never replaced."""

from dataclasses import asdict
from uuid import UUID

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from storage.models import ConversationRecord, InteractionAppraisalRecord, MessageRecord, PersonRecord
from subconscious.relationship.appraisal import Appraisal


class AppraisalStore:
    """Use a caller-owned engine; each operation owns and closes its session."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def unappraised_conversation_ids(self) -> list[UUID]:
        """List finished, nonempty conversations without an appraisal, oldest first."""
        with Session(self.engine) as session:
            return list(session.scalars(
                select(ConversationRecord.id).where(
                    ConversationRecord.status.in_(("closed", "interrupted")),
                    select(MessageRecord.id).where(
                        MessageRecord.conversation_id == ConversationRecord.id
                    ).exists(),
                    ~select(InteractionAppraisalRecord.conversation_id).where(
                        InteractionAppraisalRecord.conversation_id == ConversationRecord.id
                    ).exists(),
                ).order_by(ConversationRecord.started_at, ConversationRecord.id)
            ).all())

    def save(self, conversation_id: UUID, appraisal: Appraisal) -> InteractionAppraisalRecord:
        """Save the appraisal for the current person; an existing one is kept and returned."""
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            conversation = session.scalar(
                select(ConversationRecord).where(ConversationRecord.id == conversation_id).with_for_update()
            )
            if conversation is None:
                raise LookupError("Conversation does not exist")
            if conversation.status not in ("closed", "interrupted"):
                raise ValueError("Conversation must be closed or interrupted before it is appraised")
            existing = session.get(InteractionAppraisalRecord, conversation_id)
            if existing is not None:
                return existing
            person = session.scalars(
                select(PersonRecord).order_by(PersonRecord.first_met_at, PersonRecord.id).limit(1)
            ).first()
            if person is None:
                # Cora met this unnamed person when their first conversation began.
                person = PersonRecord(first_met_at=session.scalar(select(func.min(ConversationRecord.started_at))))
                session.add(person)
                session.flush()
            record = InteractionAppraisalRecord(
                conversation_id=conversation_id, person_id=person.id, **asdict(appraisal),
            )
            session.add(record)
            session.flush()
        return record
