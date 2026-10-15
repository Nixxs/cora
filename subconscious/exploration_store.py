"""Save Cora's explorations of her own interests: one row per attempt.

An attempt is started before any web search and finished as 'found' or 'failed', so
limits on how often she explores can count every attempt, including failed ones.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from storage.models import InterestExplorationRecord
from subconscious.interest_store import clean_text, validate_now

FINDING_MAX_CHARS = 2000
QUESTION_MAX_CHARS = 300
SOURCE_MAX_CHARS = 2000
MAX_NEXT_QUESTIONS = 3
MAX_SOURCES = 10
LIST_LIMIT = 50


@dataclass(frozen=True)
class Exploration:
    id: UUID
    interest_id: UUID
    status: str  # running, found or failed
    started_at: datetime
    finished_at: datetime | None
    finding: str | None
    next_questions: tuple[str, ...]
    sources: tuple[str, ...]


def snapshot(record: InterestExplorationRecord) -> Exploration:
    return Exploration(
        record.id, record.interest_id, record.status, record.started_at, record.finished_at,
        record.finding, tuple(record.next_questions), tuple(record.sources),
    )


def clean_questions(questions: Iterable[str]) -> list[str]:
    if isinstance(questions, str):
        raise ValueError("Next questions must be a list")
    cleaned = list(dict.fromkeys(clean_text(question, "question", QUESTION_MAX_CHARS) for question in questions))
    if len(cleaned) > MAX_NEXT_QUESTIONS:
        raise ValueError(f"At most {MAX_NEXT_QUESTIONS} next questions")
    return cleaned


def clean_sources(sources: Iterable[str]) -> list[str]:
    """Web addresses only; sources come from search results, so nothing else is trusted."""
    if isinstance(sources, str):
        raise ValueError("Sources must be a list")
    cleaned = []
    for source in sources:
        source = clean_text(source, "source", SOURCE_MAX_CHARS)
        parts = urlsplit(source)
        if parts.scheme not in ("http", "https") or not parts.hostname or any(char.isspace() for char in source):
            raise ValueError("Sources must be http or https addresses")
        cleaned.append(source)
    cleaned = list(dict.fromkeys(cleaned))
    if len(cleaned) > MAX_SOURCES:
        raise ValueError(f"At most {MAX_SOURCES} sources")
    return cleaned


class ExplorationStore:
    def __init__(self, engine: Engine):
        self.engine = engine

    def start(self, interest_id: UUID, *, now: datetime) -> Exploration:
        validate_now(now)
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            record = InterestExplorationRecord(interest_id=interest_id, status="running", started_at=now)
            session.add(record)
            session.flush()
            return snapshot(record)

    def finish(
        self, exploration_id: UUID, finding: str, next_questions: Iterable[str], sources: Iterable[str],
        *, now: datetime,
    ) -> Exploration | None:
        """Record what was found; None if the attempt is no longer running."""
        finding = clean_text(finding, "finding", FINDING_MAX_CHARS)
        next_questions = clean_questions(next_questions)
        sources = clean_sources(sources)
        validate_now(now)
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            record = self._running(session, exploration_id)
            if record is None:
                return None
            record.status, record.finding = "found", finding
            record.next_questions, record.sources = next_questions, sources
            record.finished_at = max(now, record.started_at)
            return snapshot(record)

    def fail(self, exploration_id: UUID, *, now: datetime) -> Exploration | None:
        """Close an attempt that produced nothing; None if it is no longer running."""
        validate_now(now)
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            record = self._running(session, exploration_id)
            if record is None:
                return None
            record.status = "failed"
            record.finished_at = max(now, record.started_at)
            return snapshot(record)

    def list_recent(self, limit: int = 10) -> list[Exploration]:
        """Newest attempts first."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= LIST_LIMIT:
            raise ValueError(f"Exploration list limit must be an integer from 1 to {LIST_LIMIT}")
        with Session(self.engine) as session:
            records = session.scalars(select(InterestExplorationRecord).order_by(
                InterestExplorationRecord.started_at.desc(), InterestExplorationRecord.id.desc(),
            ).limit(limit))
            return [snapshot(record) for record in records]

    @staticmethod
    def _running(session: Session, exploration_id: UUID) -> InterestExplorationRecord | None:
        record = session.scalar(select(InterestExplorationRecord).where(
            InterestExplorationRecord.id == exploration_id,
        ).with_for_update())
        if record is None:
            raise LookupError("Exploration does not exist")
        return record if record.status == "running" else None
