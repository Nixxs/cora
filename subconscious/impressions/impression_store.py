"""Cora's impression of each person she talks with as a client: her view, and a one-line opinion.

The person she works with gets her view of everyone; each client gets her view of them and only
the names and one-line opinions of the others.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import Engine, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from storage.models import ConversationRecord, ImpressionRecord, ImpressionUpdateRecord, MessageRecord, PersonRecord

IMPRESSION_MAX_CHARS = 1200
OPINION_MAX_CHARS = 200
PEOPLE_CONTEXT_MAX_CHARS = 6000
CONDUCTS = ("fine", "rude", "abusive", "threatening")
CONDUCT_NOTE_MAX_CHARS = 500
# How long a concern about someone stays in the person's view of them.
RECENT_CONCERN = timedelta(days=30)


@dataclass(frozen=True)
class Impression:
    impression: str
    opinion: str


@dataclass(frozen=True)
class Conduct:
    """How a client treated her in one conversation; a note in her words for anything but fine."""
    kind: str
    note: str | None = None


@dataclass(frozen=True)
class HeadsUp:
    conversation_id: UUID
    name: str
    started_at: datetime
    conduct: str
    note: str


def clean_conduct(kind: str, note: str | None) -> Conduct:
    if kind not in CONDUCTS:
        raise ValueError("Unknown conduct")
    if kind == "fine":
        return Conduct("fine")
    note = " ".join((note or "").split())
    if not note or len(note) > CONDUCT_NOTE_MAX_CHARS:
        raise ValueError(f"A conduct note needs 1 to {CONDUCT_NOTE_MAX_CHARS} characters")
    return Conduct(kind, note)


def clean_impression(impression: str, opinion: str) -> Impression:
    impression, opinion = " ".join(impression.split()), " ".join(opinion.split())
    if not impression or len(impression) > IMPRESSION_MAX_CHARS:
        raise ValueError(f"An impression needs 1 to {IMPRESSION_MAX_CHARS} characters")
    if not opinion or len(opinion) > OPINION_MAX_CHARS:
        raise ValueError(f"An opinion needs 1 to {OPINION_MAX_CHARS} characters")
    return Impression(impression, opinion)


def bounded(items: list[dict]) -> str:
    """As many of the items as fit, as JSON; empty when there are none."""
    context, kept = "", []
    for item in items:
        candidate = json.dumps([*kept, item], ensure_ascii=False)
        if len(candidate) > PEOPLE_CONTEXT_MAX_CHARS:
            break
        kept.append(item)
        context = candidate
    return context


class ImpressionStore:
    """Use a caller-owned engine; each operation owns and closes its session."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def waiting_conversations(self) -> list[tuple[UUID, UUID, str]]:
        """Finished, nonempty client conversations not yet in an impression, oldest first."""
        with Session(self.engine) as session:
            return [tuple(row) for row in session.execute(
                select(ConversationRecord.id, PersonRecord.id, PersonRecord.display_name)
                .join(PersonRecord, PersonRecord.id == ConversationRecord.person_id)
                .where(
                    PersonRecord.kind == "client",
                    ConversationRecord.status.in_(("closed", "interrupted")),
                    select(MessageRecord.id).where(MessageRecord.conversation_id == ConversationRecord.id).exists(),
                    ~select(ImpressionUpdateRecord.conversation_id).where(
                        ImpressionUpdateRecord.conversation_id == ConversationRecord.id,
                    ).exists(),
                ).order_by(ConversationRecord.started_at, ConversationRecord.id)
            ).all()]

    def impression(self, person_id: UUID) -> Impression | None:
        with Session(self.engine) as session:
            record = session.get(ImpressionRecord, person_id)
            return None if record is None else Impression(record.impression, record.opinion)

    def save(self, conversation_id: UUID, person_id: UUID, impression: Impression, conduct: Conduct | None = None) -> None:
        """Her revised impression, and that this conversation is in it and how they behaved, together."""
        impression = clean_impression(impression.impression, impression.opinion)
        conduct = None if conduct is None else clean_conduct(conduct.kind, conduct.note)
        with Session(self.engine) as session, session.begin():
            session.execute(insert(ImpressionUpdateRecord).values(
                conversation_id=conversation_id, person_id=person_id,
                conduct=None if conduct is None else conduct.kind, conduct_note=None if conduct is None else conduct.note,
            ).on_conflict_do_nothing())
            session.execute(insert(ImpressionRecord).values(
                person_id=person_id, impression=impression.impression, opinion=impression.opinion,
            ).on_conflict_do_update(index_elements=[ImpressionRecord.person_id], set_={
                "impression": impression.impression, "opinion": impression.opinion,
                "updated_at": func.current_timestamp(),
            }))

    def unreported_heads_ups(self) -> list[HeadsUp]:
        """Conversations where someone treated her badly that the person hasn't been told about, oldest first."""
        with Session(self.engine) as session:
            rows = session.execute(
                select(ImpressionUpdateRecord.conversation_id, PersonRecord.display_name, ConversationRecord.started_at,
                       ImpressionUpdateRecord.conduct, ImpressionUpdateRecord.conduct_note)
                .join(PersonRecord, PersonRecord.id == ImpressionUpdateRecord.person_id)
                .join(ConversationRecord, ConversationRecord.id == ImpressionUpdateRecord.conversation_id)
                .where(ImpressionUpdateRecord.conduct_note.is_not(None), ImpressionUpdateRecord.reported_at.is_(None))
                .order_by(ConversationRecord.started_at, ConversationRecord.id)
            ).all()
        return [HeadsUp(*row) for row in rows]

    def mark_reported(self, conversation_id: UUID) -> None:
        with Session(self.engine) as session, session.begin():
            session.execute(update(ImpressionUpdateRecord).where(
                ImpressionUpdateRecord.conversation_id == conversation_id,
                ImpressionUpdateRecord.reported_at.is_(None),
            ).values(reported_at=func.current_timestamp()))

    def _recent_concerns(self, session: Session, now: datetime) -> dict[UUID, dict]:
        """Each person's latest concern within RECENT_CONCERN, for the person's view of them."""
        rows = session.execute(
            select(ImpressionUpdateRecord.person_id, ConversationRecord.started_at,
                   ImpressionUpdateRecord.conduct, ImpressionUpdateRecord.conduct_note)
            .join(ConversationRecord, ConversationRecord.id == ImpressionUpdateRecord.conversation_id)
            .where(ImpressionUpdateRecord.conduct_note.is_not(None), ConversationRecord.started_at >= now - RECENT_CONCERN)
            .order_by(ConversationRecord.started_at)
        ).all()
        return {person_id: {"when": started_at.date().isoformat(), "conduct": conduct, "note": note}
                for person_id, started_at, conduct, note in rows}

    def _contacts(self, session: Session):
        # Everyone she may currently talk with; removed people drop out of every context.
        return session.execute(
            select(PersonRecord.id, PersonRecord.display_name, ImpressionRecord.impression, ImpressionRecord.opinion)
            .outerjoin(ImpressionRecord, ImpressionRecord.person_id == PersonRecord.id)
            .where(PersonRecord.kind == "client", PersonRecord.signal_number.is_not(None))
            .order_by(PersonRecord.display_name, PersonRecord.id)
        ).all()

    def people_context(self, now: datetime) -> str:
        """For the person she works with: everyone she talks with, her view of each, and any recent concern."""
        with Session(self.engine) as session:
            rows = self._contacts(session)
            concerns = self._recent_concerns(session, now)
        return bounded([
            {"name": name, "impression": impression, **({"recent_concern": concerns[person_id]} if person_id in concerns else {})}
            for person_id, name, impression, _ in rows
        ])

    def client_context(self, person_id: UUID) -> str:
        """For one client's chat: her view of them, then only the others' names and one-line opinions."""
        with Session(self.engine) as session:
            rows = self._contacts(session)
            own = session.get(ImpressionRecord, person_id)
        others = [{"name": name, "opinion": opinion} for other_id, name, _, opinion in rows if other_id != person_id]
        if own is None and not others:
            return ""
        context = {"them": None if own is None else own.impression, "others": others}
        while len(json.dumps(context, ensure_ascii=False)) > PEOPLE_CONTEXT_MAX_CHARS and context["others"]:
            context["others"].pop()
        return json.dumps(context, ensure_ascii=False)
