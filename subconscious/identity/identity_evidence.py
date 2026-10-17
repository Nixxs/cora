"""Read what Cora has experienced since she last reflected: the evidence a reflection may cite.

Three sources, matching identity_rules.EVIDENCE_SOURCES: changes to her interests,
findings from exploring them (with her own reaction), and conversation summaries.
Raw memories are never read; they are the person's facts, not her experience.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from storage.models import ConversationSummaryRecord, InterestEventRecord, InterestExplorationRecord, InterestRecord
from subconscious.identity.identity_rules import Evidence
from subconscious.interests.interest_store import validate_now

MAX_EXPERIENCES = 30
# Rows are stamped when their transaction starts, so a slow save can commit a row
# older than one already read. Reading only up to a little before now lets it land.
COMMIT_MARGIN = timedelta(minutes=5)


@dataclass(frozen=True)
class Experience:
    source: str  # one of EVIDENCE_SOURCES
    id: UUID
    at: datetime
    details: Mapping[str, str]  # what the reflection sees

    @property
    def evidence(self) -> Evidence:
        return Evidence(self.source, self.id)


@dataclass(frozen=True)
class ExperienceBatch:
    experiences: tuple[Experience, ...]  # oldest first
    until: datetime  # read through here; the reflection stamps this as last_reflected_at
    more: bool  # stopped at the limit, so more experiences wait for the next reflection


def read_experiences(engine: Engine, *, after: datetime | None, now: datetime) -> ExperienceBatch:
    """Experiences after `after` (all if None) up to a little before `now`, oldest first."""
    validate_now(now)
    if after is not None:
        validate_now(after)
    until = now - COMMIT_MARGIN
    if after is not None and after >= until:
        return ExperienceBatch((), after, False)
    with Session(engine) as session:
        found = [*interest_changes(session, after, until), *findings(session, after, until),
                 *conversations(session, after, until)]
    found.sort(key=lambda item: (item.at, item.source, item.id))
    if len(found) <= MAX_EXPERIENCES:
        return ExperienceBatch(tuple(found), until, False)
    # Stop between timestamps, so experiences sharing one are never split across reflections.
    cut = found[MAX_EXPERIENCES].at
    kept = [item for item in found[:MAX_EXPERIENCES] if item.at < cut] or [
        item for item in found if item.at == cut]
    return ExperienceBatch(tuple(kept), kept[-1].at, True)


def window(query, column, after: datetime | None, until: datetime):
    query = query.where(column <= until)
    if after is not None:
        query = query.where(column > after)
    # One more than the limit from each source is enough to know whether the merge was cut.
    return query.order_by(column).limit(MAX_EXPERIENCES + 1)


def interest_changes(session: Session, after: datetime | None, until: datetime) -> list[Experience]:
    event = InterestEventRecord
    rows = session.execute(window(select(event, InterestRecord.title).join(
        InterestRecord, InterestRecord.id == event.interest_id), event.created_at, after, until))
    return [Experience("interest_event", record.id, record.created_at, {
        "interest": title, "change": record.kind, "reason": record.reason,
    }) for record, title in rows]


def findings(session: Session, after: datetime | None, until: datetime) -> list[Experience]:
    exploration = InterestExplorationRecord
    rows = session.execute(window(select(exploration, InterestRecord.title).join(
        InterestRecord, InterestRecord.id == exploration.interest_id,
    ).where(exploration.status == "found"), exploration.finished_at, after, until))
    experiences = []
    for record, title in rows:
        details = {"interest": title, "finding": record.finding}
        if record.reaction is not None:
            details["your_reaction"] = record.reaction
        experiences.append(Experience("exploration", record.id, record.finished_at, details))
    return experiences


def conversations(session: Session, after: datetime | None, until: datetime) -> list[Experience]:
    summary = ConversationSummaryRecord
    records = session.scalars(window(select(summary), summary.created_at, after, until))
    return [Experience("conversation", record.conversation_id, record.created_at, {"summary": record.text})
            for record in records]
