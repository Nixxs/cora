"""Save Cora's explorations of her own interests: one row per attempt.

An attempt is started before any web search and finished as 'found' or 'failed', so
limits on how often she explores can count every attempt, including failed ones.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import Engine, func, select, text, update
from sqlalchemy.orm import Session

from storage.database import build_engine
from storage.models import InterestExplorationRecord, InterestRecord
from subconscious.interest_store import Interest, clean_text, snapshot as interest_snapshot, validate_now
from subconscious.interest_strength import settle

FINDING_MAX_CHARS = 2000
QUESTION_MAX_CHARS = 300
SOURCE_MAX_CHARS = 2000
MAX_NEXT_QUESTIONS = 3
MAX_SOURCES = 10
LIST_LIMIT = 50
# Every attempt counts, including failed and running ones, so a broken API is not retried often.
MIN_GAP = timedelta(hours=6)
DAILY_LIMIT = 3
INTEREST_COOLDOWN = timedelta(hours=24)
# A running attempt this old was abandoned (e.g. the app closed mid-search).
STALE_AFTER = timedelta(minutes=30)
# Raising a finding herself: only each interest's latest, while fresh, and rarely.
MENTION_GAP = timedelta(hours=4)
MENTION_DAILY_LIMIT = 2
MENTION_FRESH_FOR = timedelta(days=3)


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


@dataclass(frozen=True)
class Mention:
    exploration_id: UUID
    interest_title: str
    finding: str
    found_at: datetime
    mentioned_at: datetime


@dataclass(frozen=True)
class Start:
    reason: str  # started, too soon, daily limit or no eligible interest
    exploration: Exploration | None = None
    interest: Interest | None = None


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

    def start_next(self, *, now: datetime) -> Start:
        """Start exploring the next eligible interest, if the limits allow one now."""
        validate_now(now)
        attempts = InterestExplorationRecord
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            # One starter at a time, so two workers can never both pass the limits.
            session.execute(text("LOCK TABLE public.interest_explorations IN SHARE ROW EXCLUSIVE MODE"))
            session.execute(update(attempts).where(
                attempts.status == "running", attempts.started_at <= now - STALE_AFTER,
            ).values(status="failed", finished_at=now).execution_options(synchronize_session=False))

            # Attempts stamped in the future (clock moved back) count as recent.
            recent = session.scalars(select(attempts.started_at).where(
                attempts.started_at > now - timedelta(hours=24),
            )).all()
            if any(started_at > now - MIN_GAP for started_at in recent):
                return Start("too soon")
            if len(recent) >= DAILY_LIMIT:
                return Start("daily limit")

            last_explored = dict(session.execute(
                select(attempts.interest_id, func.max(attempts.started_at)).group_by(attempts.interest_id),
            ).all())
            candidates = []
            for interest in session.scalars(select(InterestRecord).where(InterestRecord.status != "dormant")):
                strength, status = settle(interest.strength, interest.strength_at, interest.status, now)
                explored = last_explored.get(interest.id)
                if status == "dormant" or (explored is not None and explored > now - INTEREST_COOLDOWN):
                    continue
                # Never explored first, then least recently explored, then strongest.
                candidates.append((explored is not None, explored or now, -strength,
                                   interest.created_at, interest.id, interest))
            if not candidates:
                return Start("no eligible interest")
            interest = min(candidates, key=lambda candidate: candidate[:5])[5]

            record = attempts(interest_id=interest.id, status="running", started_at=now)
            session.add(record)
            session.flush()
            return Start("started", snapshot(record), interest_snapshot(interest))

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

    def claim_mention(self, *, now: datetime) -> Mention | None:
        """Mark a finding as mentioned before it is offered, if the limits allow one now."""
        validate_now(now)
        attempts = InterestExplorationRecord
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            # One claimer at a time, so two workers can never both pass the limits.
            session.execute(text("LOCK TABLE public.interest_explorations IN SHARE ROW EXCLUSIVE MODE"))
            # Mentions stamped in the future (clock moved back) count as recent.
            recent = session.scalars(select(attempts.mentioned_at).where(
                attempts.mentioned_at > now - timedelta(hours=24),
            )).all()
            if len(recent) >= MENTION_DAILY_LIMIT or any(at > now - MENTION_GAP for at in recent):
                return None

            rows = session.execute(select(attempts, InterestRecord).join(
                InterestRecord, InterestRecord.id == attempts.interest_id,
            ).where(
                attempts.status == "found", attempts.finished_at > now - MENTION_FRESH_FOR,
            ).order_by(attempts.started_at.desc(), attempts.id.desc())).all()
            seen = set()
            for record, interest in rows:
                # An older finding is superseded by the interest's latest, mentioned or not.
                if interest.id in seen:
                    continue
                seen.add(interest.id)
                status = settle(interest.strength, interest.strength_at, interest.status, now)[1]
                if record.mentioned_at is None and status != "dormant":
                    record.mentioned_at = now
                    return Mention(record.id, interest.title, record.finding, record.finished_at, now)
            return None

    def release_mention(self, mention: Mention) -> bool:
        """Undo a claim whose send failed, unless the finding was claimed again since."""
        attempts = InterestExplorationRecord
        with Session(self.engine) as session, session.begin():
            result = session.execute(update(attempts).where(
                attempts.id == mention.exploration_id, attempts.mentioned_at == mention.mentioned_at,
            ).values(mentioned_at=None).execution_options(synchronize_session=False))
            return result.rowcount == 1

    def list_recent(self, limit: int = 10, *, interest_id: UUID | None = None) -> list[Exploration]:
        """Newest attempts first, optionally for one interest."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= LIST_LIMIT:
            raise ValueError(f"Exploration list limit must be an integer from 1 to {LIST_LIMIT}")
        query = select(InterestExplorationRecord)
        if interest_id is not None:
            query = query.where(InterestExplorationRecord.interest_id == interest_id)
        with Session(self.engine) as session:
            records = session.scalars(query.order_by(
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


# Thin wrappers for the app, run off its event loop; each uses and disposes its own engine.
def claim_mention_now() -> Mention | None:
    engine = build_engine()
    try:
        return ExplorationStore(engine).claim_mention(now=datetime.now(timezone.utc))
    finally:
        engine.dispose()


def release_mention_now(mention: Mention) -> bool:
    engine = build_engine()
    try:
        return ExplorationStore(engine).release_mention(mention)
    finally:
        engine.dispose()
