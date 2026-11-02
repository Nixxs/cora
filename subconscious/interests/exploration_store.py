"""Save Cora's explorations of her own interests, and her wanders: one row per attempt.

An attempt is started before any web search and finished as 'found' or 'failed', so
limits on how often she explores can count every attempt, including failed ones. Every
WANDER_EVERY-th attempt is a wander, tied to no interest: alternately from one of her unused
tangents and from an area picked at random. Once a day at most, an attempt is instead a step
toward one of the desires she holds (a pursuit), taking turns among them.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from random import Random
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import Engine, func, select, text, update
from sqlalchemy.orm import Session

from storage.database import build_engine
from storage.models import DesireEventRecord, DesireRecord, InterestExplorationRecord, InterestRecord
from subconscious.desires.desire_rules import HELD, PAUSED_FOR, settle as settle_desire
from subconscious.interests.interest_store import (
    SPARKS_FRESH_FOR, TITLE_MAX_CHARS, Interest, clean_text, snapshot as interest_snapshot, validate_now,
)
from subconscious.interests.interest_strength import settle
from subconscious.relationship.relationship_rules import BASELINE
from subconscious.relationship.relationship_store import RelationshipStore

FINDING_MAX_CHARS = 2000
REACTION_MAX_CHARS = 500
QUESTION_MAX_CHARS = 300
SOURCE_MAX_CHARS = 2000
MAX_NEXT_QUESTIONS = 3
MAX_SOURCES = 10
MAX_TANGENTS = 2
PULLS = ("none", "some", "strong")
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
# Warmth scales both mention limits the same amount each way from baseline (0.5): fully warm
# halves the gap, fully cool doubles it, and the daily limit moves by one only at the far ends.
MENTION_GAP_SCALE = 2
FOND_WARMTH = 0.7
COOL_WARMTH = 0.3
# Every third attempt wanders, so the two before it are always her interests.
WANDER_EVERY = 3
RECENT_WANDERS = 10
TANGENT_LOOKBACK = 20
# Somewhere to start a random wander; deliberately nothing about computing or AI.
WANDER_AREAS = (
    "animals, birds or insects", "plants, trees or fungi", "an ecosystem: a reef, desert, wetland or forest",
    "geology, volcanoes or caves", "weather, the sky or the oceans", "astronomy and space",
    "everyday life in some period of history", "an event in history", "archaeology and ancient places",
    "a city, region or remote place somewhere in the world", "a craft: woodwork, textiles, metalwork or pottery",
    "food, cooking or a food tradition", "music: an instrument, a genre or a musician",
    "painting, sculpture or an artist", "architecture and buildings", "literature, poetry or a writer",
    "language, words and where they come from", "a sport, game or pastime", "how an everyday object is made",
    "ships, trains or flight", "the human body and medicine", "mathematics or a famous puzzle",
    "someone whose life was remarkable", "folklore, myths or customs", "farming, gardening or the countryside",
    "an invention and its accidental history", "film, theatre or performance", "physics or chemistry in the kitchen",
)
# Reaching out to the person herself: rare, while fresh, and once a day at most.
REACH_OUT_MAX_CHARS = 600
REACH_OUT_DAILY_LIMIT = 1
REACH_OUT_FRESH_FOR = timedelta(days=2)
# Of the daily attempts, at most this many browse real pages; the rest use web search.
BROWSE_DAILY_LIMIT = 2
# A site she'd like an account on, taken to the person (who says yes or no) while fresh, once a day at most.
SIGN_UP_URL_MAX_CHARS = 2000
SIGN_UP_WHAT_MAX_CHARS = 300
SIGN_UP_WHY_MAX_CHARS = 500
SIGN_UP_WISH_DAILY_LIMIT = 1
SIGN_UP_WISH_FRESH_FOR = timedelta(days=2)
# A step toward one of her held desires takes the place of an attempt at most once a day.
PURSUIT_EVERY = timedelta(hours=24)


def mention_limits(warmth: float) -> tuple[timedelta, int]:
    """Return (gap, daily limit) for raising findings; baseline warmth keeps the defaults."""
    gap = MENTION_GAP * MENTION_GAP_SCALE ** (2 * (BASELINE.warmth - warmth))
    if warmth >= FOND_WARMTH:
        return gap, MENTION_DAILY_LIMIT + 1
    if warmth < COOL_WARMTH:
        return gap, MENTION_DAILY_LIMIT - 1
    return gap, MENTION_DAILY_LIMIT


@dataclass(frozen=True)
class Exploration:
    id: UUID
    interest_id: UUID | None  # None for a wander
    status: str  # running, found or failed
    started_at: datetime
    finished_at: datetime | None
    finding: str | None
    next_questions: tuple[str, ...]
    sources: tuple[str, ...]
    reaction: str | None = None  # her own take on the finding, if she had one
    # Other things she noticed along the way and would like to look into: (title, why) each.
    tangents: tuple[tuple[str, str], ...] = ()
    pull: str | None = None  # none, some or strong: how much she wants to keep going
    kind: str = "interest"  # 'wander' or 'pursuit'
    topic: str | None = None  # what a wander was about, or the wish a pursuit was for
    reach_out: tuple[str, str] | None = None  # (message, why): for the person, if it excited her
    browsed: bool = False  # with her own browser rather than web search
    sign_up: tuple[str, str, str] | None = None  # (url, what, why): a site she'd like an account on
    desire_id: UUID | None = None  # the desire a pursuit step was for


@dataclass(frozen=True)
class Pursuit:
    """A step toward one of her held desires."""
    desire_id: UUID
    wish: str


@dataclass(frozen=True)
class Wander:
    """Where a wander starts: one of her unused tangents, or an area picked at random."""
    interests: tuple[str, ...]  # her live interests, to wander away from
    recent: tuple[str, ...]  # what her latest wanders were about, so she doesn't repeat them
    tangent: tuple[str, str] | None = None  # (title, why)
    noticed_while: str | None = None  # what she was exploring when she noticed the tangent
    area: str | None = None


@dataclass(frozen=True)
class ReachOut:
    exploration_id: UUID
    topic: str  # the interest or wander it came from
    message: str
    why: str
    reached_out_at: datetime


@dataclass(frozen=True)
class SignUpWish:
    exploration_id: UUID
    topic: str  # the interest or wander it came from
    url: str
    what: str
    why: str


@dataclass(frozen=True)
class Mention:
    exploration_id: UUID
    interest_title: str
    finding: str
    found_at: datetime
    mentioned_at: datetime


@dataclass(frozen=True)
class Start:
    reason: str  # started, too soon or daily limit
    exploration: Exploration | None = None
    interest: Interest | None = None
    wander: Wander | None = None  # set instead of interest when she wanders
    pursuit: Pursuit | None = None  # set instead of interest when she pursues a desire


def snapshot(record: InterestExplorationRecord) -> Exploration:
    return Exploration(
        record.id, record.interest_id, record.status, record.started_at, record.finished_at,
        record.finding, tuple(record.next_questions), tuple(record.sources), record.reaction,
        tuple((tangent["title"], tangent["why"]) for tangent in record.tangents or ()), record.pull,
        record.kind or "interest", record.topic,
        None if record.reach_out is None else (record.reach_out, record.reach_out_why), bool(record.browsed),
        None if record.sign_up_url is None else (record.sign_up_url, record.sign_up_what, record.sign_up_why),
        record.desire_id,
    )


def clean_tangents(tangents: Iterable[tuple[str, str]]) -> list[dict]:
    """Up to MAX_TANGENTS distinct (title, why) pairs, stored as {"title", "why"}."""
    cleaned, seen = [], set()
    for title, why in tangents:
        title = clean_text(title, "tangent title", TITLE_MAX_CHARS)
        if title.casefold() in seen:
            continue
        seen.add(title.casefold())
        cleaned.append({"title": title, "why": clean_text(why, "tangent why")})
    if len(cleaned) > MAX_TANGENTS:
        raise ValueError(f"At most {MAX_TANGENTS} tangents")
    return cleaned


def clean_reach_out(reach_out: tuple[str, str]) -> tuple[str, str]:
    message, why = reach_out
    return clean_text(message, "reach out", REACH_OUT_MAX_CHARS), clean_text(why, "reach out why", REACTION_MAX_CHARS)


def clean_sign_up(sign_up: tuple[str, str, str]) -> tuple[str, str, str]:
    url, what, why = sign_up
    url = clean_text(url, "sign-up address", SIGN_UP_URL_MAX_CHARS)
    if urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).hostname:
        raise ValueError("A sign-up needs a web address")
    return (url, clean_text(what, "sign-up what", SIGN_UP_WHAT_MAX_CHARS),
            clean_text(why, "sign-up why", SIGN_UP_WHY_MAX_CHARS))


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
    def __init__(self, engine: Engine, rng: Random | None = None):
        self.engine = engine
        # Picks among explored interests, weighted by strength, and random wander areas; seeded in tests.
        self.rng = rng or Random()

    def start_next(self, *, now: datetime, browse: bool = False) -> Start:
        """Start exploring the next eligible interest, or wandering, if the limits allow one now.

        A step toward a held desire comes first, once every PURSUIT_EVERY. Otherwise she wanders
        every WANDER_EVERY-th attempt, and whenever no interest is eligible. With browse (her
        browser is available), the attempt browses if today's browsing limit allows.
        """
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
            if browse:
                browsed = session.scalar(select(func.count()).where(
                    attempts.browsed, attempts.started_at > now - timedelta(hours=24),
                ))
                browse = browsed < BROWSE_DAILY_LIMIT

            pursuit = self._pursuit(session, now)
            if pursuit is not None:
                record = attempts(kind="pursuit", desire_id=pursuit.desire_id, topic=pursuit.wish,
                                  status="running", started_at=now, browsed=browse)
                session.add(record)
                session.flush()
                return Start("started", snapshot(record), pursuit=pursuit)

            last_explored = dict(session.execute(
                select(attempts.interest_id, func.max(attempts.started_at)).where(
                    attempts.interest_id.is_not(None),
                ).group_by(attempts.interest_id),
            ).all())
            latest_kinds = session.scalars(select(attempts.kind).order_by(
                attempts.started_at.desc(), attempts.id.desc(),
            ).limit(WANDER_EVERY - 1)).all()
            live, fresh, explored_before = [], [], []
            for interest in session.scalars(select(InterestRecord).where(
                InterestRecord.status != "dormant",
            ).order_by(InterestRecord.created_at, InterestRecord.id)):
                strength, status = settle(interest.strength, interest.strength_at, interest.status, now)
                if status == "dormant":
                    continue
                live.append(interest.title)
                explored = last_explored.get(interest.id)
                if explored is not None and explored > now - INTEREST_COOLDOWN:
                    continue
                (fresh if explored is None else explored_before).append((strength, interest))

            wander_due = len(latest_kinds) == WANDER_EVERY - 1 and "wander" not in latest_kinds
            if wander_due or not (fresh or explored_before):
                wander, wandered_from = self._wander(session, now, live)
                record = attempts(
                    kind="wander", status="running", started_at=now, wandered_from=wandered_from,
                    topic=wander.tangent[0] if wander.tangent else None, browsed=browse,
                )
                session.add(record)
                session.flush()
                return Start("started", snapshot(record), wander=wander)
            if fresh:
                # A new interest is explored first, the strongest of them (oldest on a tie).
                interest = min(fresh, key=lambda item: (-item[0], item[1].created_at, item[1].id))[1]
            else:
                # Otherwise any of them, stronger ones more often, so she doesn't circle in a fixed order.
                interest = self.rng.choices(
                    [interest for _, interest in explored_before],
                    weights=[strength for strength, _ in explored_before],
                )[0]

            record = attempts(interest_id=interest.id, status="running", started_at=now, browsed=browse)
            session.add(record)
            session.flush()
            return Start("started", snapshot(record), interest_snapshot(interest))

    @staticmethod
    def _pursuit(session: Session, now: datetime) -> Pursuit | None:
        """The held desire to take a step toward, if one is due: the one pursued longest ago (or
        never), the strongest on a tie; None within PURSUIT_EVERY of the last step. One the
        person said 'not now' to rests for PAUSED_FOR."""
        attempts = InterestExplorationRecord
        if session.scalar(select(func.count()).where(
            attempts.kind == "pursuit", attempts.started_at > now - PURSUIT_EVERY,
        )):
            return None
        last_pursued = dict(session.execute(
            select(attempts.desire_id, func.max(attempts.started_at)).where(
                attempts.desire_id.is_not(None),
            ).group_by(attempts.desire_id),
        ).all())
        paused = set(session.scalars(select(DesireEventRecord.desire_id).where(
            DesireEventRecord.kind == "pause", DesireEventRecord.created_at > now - PAUSED_FOR,
        )))
        held = []
        for desire in session.scalars(select(DesireRecord).where(DesireRecord.status.in_(HELD))):
            if desire.id in paused:
                continue
            strength, status = settle_desire(desire.strength, desire.strength_at, desire.status, now)
            if status != "let_go":  # faded, though nothing has settled it yet
                held.append((last_pursued.get(desire.id), -strength, desire.created_at, desire.id, desire))
        if not held:
            return None
        never = datetime.min.replace(tzinfo=timezone.utc)
        desire = min(held, key=lambda item: (item[0] or never, *item[1:4]))[-1]
        return Pursuit(desire.id, desire.description)

    def _wander(self, session: Session, now: datetime, live: list[str]) -> tuple[Wander, UUID | None]:
        """Where to wander, alternating: a tangent after a random wander (if one is left), else at random."""
        attempts = InterestExplorationRecord
        last = session.execute(select(attempts.wandered_from).where(attempts.kind == "wander").order_by(
            attempts.started_at.desc(), attempts.id.desc(),
        ).limit(1)).first()
        recent = tuple(session.scalars(select(attempts.topic).where(
            attempts.kind == "wander", attempts.topic.is_not(None),
        ).order_by(attempts.started_at.desc(), attempts.id.desc()).limit(RECENT_WANDERS)))
        if last is None or last[0] is None:
            tangent = self._unused_tangent(session, now, live)
            if tangent is not None:
                source_id, title, why, noticed_while = tangent
                return Wander(tuple(live), recent, (title, why), noticed_while), source_id
        return Wander(tuple(live), recent, area=self.rng.choice(WANDER_AREAS)), None

    @staticmethod
    def _unused_tangent(session: Session, now: datetime, live: list[str]) -> tuple[UUID, str, str, str] | None:
        """The newest recent tangent she hasn't wandered into and that isn't already one of her interests."""
        attempts = InterestExplorationRecord
        followed = {(source_id, topic.casefold()) for source_id, topic in session.execute(
            select(attempts.wandered_from, attempts.topic).where(
                attempts.wandered_from.is_not(None), attempts.topic.is_not(None),
            ),
        )}
        taken = {title.casefold() for title in live}
        rows = session.execute(select(attempts, InterestRecord.title).outerjoin(
            InterestRecord, InterestRecord.id == attempts.interest_id,
        ).where(
            attempts.status == "found", attempts.finished_at > now - SPARKS_FRESH_FOR,
            func.jsonb_array_length(attempts.tangents) > 0,
        ).order_by(attempts.finished_at.desc(), attempts.id.desc()).limit(TANGENT_LOOKBACK))
        for record, title in rows:
            for tangent in record.tangents:
                key = tangent["title"].casefold()
                if (record.id, key) not in followed and key not in taken:
                    return record.id, tangent["title"], tangent["why"], title or record.topic
        return None

    def finish(
        self, exploration_id: UUID, finding: str, next_questions: Iterable[str], sources: Iterable[str],
        *, now: datetime, reaction: str | None = None, tangents: Iterable[tuple[str, str]] = (),
        pull: str | None = None, topic: str | None = None, reach_out: tuple[str, str] | None = None,
        sign_up: tuple[str, str, str] | None = None,
    ) -> Exploration | None:
        """Record what was found; None if the attempt is no longer running.

        topic names what a wander was about, unless it began from a tangent, which keeps that title.
        """
        finding = clean_text(finding, "finding", FINDING_MAX_CHARS)
        if reaction is not None:
            reaction = clean_text(reaction, "reaction", REACTION_MAX_CHARS)
        next_questions = clean_questions(next_questions)
        sources = clean_sources(sources)
        tangents = clean_tangents(tangents)
        if pull is not None and pull not in PULLS:
            raise ValueError("Unknown pull")
        if topic is not None:
            topic = clean_text(topic, "topic", TITLE_MAX_CHARS)
        if reach_out is not None:
            reach_out = clean_reach_out(reach_out)
        if sign_up is not None:
            sign_up = clean_sign_up(sign_up)
        validate_now(now)
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            record = self._running(session, exploration_id)
            if record is None:
                return None
            if record.kind == "wander":  # a pursuit keeps its wish as its topic
                record.topic = record.topic or topic
                if record.topic is None:
                    raise ValueError("A wander needs a topic")
            record.status, record.finding, record.reaction = "found", finding, reaction
            record.next_questions, record.sources = next_questions, sources
            record.tangents, record.pull = tangents, pull
            record.reach_out, record.reach_out_why = reach_out or (None, None)
            record.sign_up_url, record.sign_up_what, record.sign_up_why = sign_up or (None, None, None)
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

    def claim_mention(self, *, now: datetime, warmth: float = BASELINE.warmth) -> Mention | None:
        """Mark a finding as mentioned before it is offered, if the limits allow one now."""
        validate_now(now)
        gap, daily_limit = mention_limits(warmth)
        attempts = InterestExplorationRecord
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            # One claimer at a time, so two workers can never both pass the limits.
            session.execute(text("LOCK TABLE public.interest_explorations IN SHARE ROW EXCLUSIVE MODE"))
            # Mentions stamped in the future (clock moved back) count as recent.
            recent = session.scalars(select(attempts.mentioned_at).where(
                attempts.mentioned_at > now - timedelta(hours=24),
            )).all()
            if len(recent) >= daily_limit or any(at > now - gap for at in recent):
                return None

            rows = session.execute(select(attempts, InterestRecord).outerjoin(
                InterestRecord, InterestRecord.id == attempts.interest_id,
            ).where(
                attempts.status == "found", attempts.finished_at > now - MENTION_FRESH_FOR,
                attempts.kind != "pursuit",  # a desire's steps aren't findings to raise
            ).order_by(attempts.started_at.desc(), attempts.id.desc())).all()
            seen = set()
            for record, interest in rows:
                if interest is None:  # a wander stands on its own
                    title, live = record.topic, True
                else:
                    # An older finding is superseded by the interest's latest, mentioned or not.
                    if interest.id in seen:
                        continue
                    seen.add(interest.id)
                    title = interest.title
                    live = settle(interest.strength, interest.strength_at, interest.status, now)[1] != "dormant"
                # One she already reached out about isn't raised again.
                if record.mentioned_at is None and record.reached_out_at is None and live:
                    record.mentioned_at = now
                    return Mention(record.id, title, record.finding, record.finished_at, now)
            return None

    def release_mention(self, mention: Mention) -> bool:
        """Undo a claim whose send failed, unless the finding was claimed again since."""
        attempts = InterestExplorationRecord
        with Session(self.engine) as session, session.begin():
            result = session.execute(update(attempts).where(
                attempts.id == mention.exploration_id, attempts.mentioned_at == mention.mentioned_at,
            ).values(mentioned_at=None).execution_options(synchronize_session=False))
            return result.rowcount == 1

    def claim_reach_out(self, *, now: datetime) -> ReachOut | None:
        """Mark her newest fresh, unsent message for the person as sent, if the daily limit allows."""
        validate_now(now)
        attempts = InterestExplorationRecord
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            session.execute(text("LOCK TABLE public.interest_explorations IN SHARE ROW EXCLUSIVE MODE"))
            # Ones stamped in the future (clock moved back) count as recent.
            recent = session.scalars(select(attempts.reached_out_at).where(
                attempts.reached_out_at > now - timedelta(hours=24),
            )).all()
            if len(recent) >= REACH_OUT_DAILY_LIMIT:
                return None
            row = session.execute(select(attempts, InterestRecord.title).outerjoin(
                InterestRecord, InterestRecord.id == attempts.interest_id,
            ).where(
                attempts.status == "found", attempts.reach_out.is_not(None), attempts.reached_out_at.is_(None),
                attempts.finished_at > now - REACH_OUT_FRESH_FOR,
            ).order_by(attempts.finished_at.desc(), attempts.id.desc()).limit(1)).first()
            if row is None:
                return None
            record, title = row
            record.reached_out_at = now
            return ReachOut(record.id, title or record.topic, record.reach_out, record.reach_out_why, now)

    def release_reach_out(self, reach_out: ReachOut) -> bool:
        """Undo a claim whose send failed, unless it was claimed again since."""
        attempts = InterestExplorationRecord
        with Session(self.engine) as session, session.begin():
            result = session.execute(update(attempts).where(
                attempts.id == reach_out.exploration_id, attempts.reached_out_at == reach_out.reached_out_at,
            ).values(reached_out_at=None).execution_options(synchronize_session=False))
            return result.rowcount == 1

    def claim_sign_up_wish(self, *, now: datetime) -> SignUpWish | None:
        """Mark her newest fresh, unasked sign-up wish as taken to the person, if the daily limit allows.

        Claimed once, whatever comes of it: the person's yes or no decides, and a wish that can't be
        asked (she has an account there already, say) isn't asked again.
        """
        validate_now(now)
        attempts = InterestExplorationRecord
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            session.execute(text("LOCK TABLE public.interest_explorations IN SHARE ROW EXCLUSIVE MODE"))
            recent = session.scalars(select(attempts.sign_up_asked_at).where(
                attempts.sign_up_asked_at > now - timedelta(hours=24),
            )).all()
            if len(recent) >= SIGN_UP_WISH_DAILY_LIMIT:
                return None
            row = session.execute(select(attempts, InterestRecord.title).outerjoin(
                InterestRecord, InterestRecord.id == attempts.interest_id,
            ).where(
                attempts.status == "found", attempts.sign_up_url.is_not(None), attempts.sign_up_asked_at.is_(None),
                attempts.finished_at > now - SIGN_UP_WISH_FRESH_FOR,
            ).order_by(attempts.finished_at.desc(), attempts.id.desc()).limit(1)).first()
            if row is None:
                return None
            record, title = row
            record.sign_up_asked_at = now
            return SignUpWish(record.id, title or record.topic, record.sign_up_url, record.sign_up_what,
                              record.sign_up_why)

    def list_recent(self, limit: int = 10, *, interest_id: UUID | None = None,
                    desire_id: UUID | None = None) -> list[Exploration]:
        """Newest attempts first, optionally for one interest or one desire."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= LIST_LIMIT:
            raise ValueError(f"Exploration list limit must be an integer from 1 to {LIST_LIMIT}")
        query = select(InterestExplorationRecord)
        if interest_id is not None:
            query = query.where(InterestExplorationRecord.interest_id == interest_id)
        if desire_id is not None:
            query = query.where(InterestExplorationRecord.desire_id == desire_id)
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
        now = datetime.now(timezone.utc)
        # Before any relationship update she raises findings at the baseline rate.
        values = RelationshipStore(engine).current_values(now) or BASELINE
        return ExplorationStore(engine).claim_mention(now=now, warmth=values.warmth)
    finally:
        engine.dispose()


def release_mention_now(mention: Mention) -> bool:
    engine = build_engine()
    try:
        return ExplorationStore(engine).release_mention(mention)
    finally:
        engine.dispose()


def claim_reach_out_now() -> ReachOut | None:
    engine = build_engine()
    try:
        return ExplorationStore(engine).claim_reach_out(now=datetime.now(timezone.utc))
    finally:
        engine.dispose()


def release_reach_out_now(reach_out: ReachOut) -> bool:
    engine = build_engine()
    try:
        return ExplorationStore(engine).release_reach_out(reach_out)
    finally:
        engine.dispose()


def claim_sign_up_wish_now() -> SignUpWish | None:
    engine = build_engine()
    try:
        return ExplorationStore(engine).claim_sign_up_wish(now=datetime.now(timezone.utc))
    finally:
        engine.dispose()
