"""Save each sign-up Cora asked for: the person's answer, and how signing up went.

A site holds at most one account: a sign-up under way, waiting for its confirmation email or
done. She may ask at most DAILY_LIMIT times a day, answered or not. Mail from a site settles its
sign-up: one waiting for confirmation is done once the link is opened, and a blocked one (which
the person may have finished for her) is done once the site writes to her.
"""

from dataclasses import dataclass
import json
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import Engine, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from storage.models import SignUpRecord

DECLINED, TIMED_OUT, SIGNING_UP, CONFIRM_EMAIL, DONE, BLOCKED, FAILED = (
    "declined", "timed_out", "signing_up", "confirm_email", "done", "blocked", "failed",
)
HOLDS_SITE = (SIGNING_UP, CONFIRM_EMAIL, DONE)
BLOCKERS = ("captcha", "phone", "payment", "personal_details", "other")
DAILY_LIMIT = 3
WHAT_MAX_CHARS = 300
WHY_MAX_CHARS = 500
NOTE_MAX_CHARS = 1000
# A sign-up still under way after this was cut short (the app closed mid-way).
STALE_AFTER = timedelta(minutes=15)
LIST_LIMIT = 8
# Her sign-ups as they reach her conversations.
CONTEXT_MAX_CHARS = 2000
# Mail from a site settles a blocked or unconfirmed sign-up this long after she asked.
MAIL_WAIT = timedelta(days=30)
# A confirmation email this late has probably gone astray; she tells the person once.
STALLED_AFTER = timedelta(days=2)


@dataclass(frozen=True)
class SignUp:
    id: UUID
    site: str
    url: str
    what: str
    why: str
    status: str
    blocked_by: str | None
    note: str | None
    asked_at: datetime
    finished_at: datetime | None


def sign_up_view(record: SignUpRecord) -> SignUp:
    return SignUp(record.id, record.site, record.url, record.what, record.why, record.status, record.blocked_by,
                  record.note, record.asked_at, record.finished_at)


class SignUpStore:
    """Use a caller-owned engine; each operation owns and closes its session."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def holding(self, site: str, now: datetime) -> SignUp | None:
        """The account (or sign-up under way) she has on this site, if any; a stale one is marked failed first."""
        with Session(self.engine) as session, session.begin():
            session.execute(
                update(SignUpRecord)
                .where(SignUpRecord.status == SIGNING_UP, SignUpRecord.updated_at < now - STALE_AFTER)
                .values(status=FAILED, note="Cut short: the app closed while signing up.", finished_at=now,
                        updated_at=now)
            )
            record = session.scalars(
                select(SignUpRecord).where(SignUpRecord.site == site, SignUpRecord.status.in_(HOLDS_SITE))
            ).first()
            return None if record is None else sign_up_view(record)

    def asked_today(self, now: datetime) -> int:
        with Session(self.engine) as session:
            return session.scalar(
                select(func.count()).select_from(SignUpRecord).where(SignUpRecord.asked_at > now - timedelta(days=1))
            )

    def record(self, site: str, url: str, what: str, why: str, status: str, now: datetime) -> SignUp:
        """The person's answer: declined, timed out, or signing up (which fails if the site is already held)."""
        if status not in (DECLINED, TIMED_OUT, SIGNING_UP):
            raise ValueError("A new sign-up is declined, timed out or signing up")
        record = SignUpRecord(
            site=site, url=url[:2000], what=what.strip()[:WHAT_MAX_CHARS], why=why.strip()[:WHY_MAX_CHARS],
            status=status, asked_at=now, updated_at=now, finished_at=None if status == SIGNING_UP else now,
        )
        with Session(self.engine) as session, session.begin():
            session.add(record)
            session.flush()
            return sign_up_view(record)

    def finish(self, sign_up_id: UUID, status: str, now: datetime, blocked_by: str | None = None,
               note: str | None = None) -> None:
        """How signing up went: done, waiting for the confirmation email, blocked or failed."""
        if status not in (CONFIRM_EMAIL, DONE, BLOCKED, FAILED):
            raise ValueError("Signing up ends done, waiting for confirmation, blocked or failed")
        if (status == BLOCKED) != (blocked_by in BLOCKERS):
            raise ValueError("A blocked sign-up says what blocked it, and only a blocked one")
        note = " ".join((note or "").split())[:NOTE_MAX_CHARS] or None
        with Session(self.engine) as session, session.begin():
            session.execute(
                update(SignUpRecord).where(SignUpRecord.id == sign_up_id, SignUpRecord.status == SIGNING_UP)
                .values(status=status, blocked_by=blocked_by, note=note, updated_at=now,
                        finished_at=None if status == CONFIRM_EMAIL else now)
            )

    def recent(self, limit: int = LIST_LIMIT) -> list[SignUp]:
        """Her latest sign-ups, newest first, leaving out ones the person declined or didn't answer."""
        with Session(self.engine) as session:
            records = session.scalars(
                select(SignUpRecord).where(SignUpRecord.status.not_in((DECLINED, TIMED_OUT)))
                .order_by(SignUpRecord.asked_at.desc()).limit(limit)
            ).all()
            return [sign_up_view(record) for record in records]

    def context(self) -> str:
        """For the person's conversations: her latest sign-ups and how each stands, as JSON; empty when none."""
        context, kept = "", []
        for sign_up in self.recent():
            item = {"site": sign_up.site, "what": sign_up.what, "status": sign_up.status,
                    "asked": sign_up.asked_at.date().isoformat()}
            if sign_up.blocked_by:
                item["blocked_by"] = sign_up.blocked_by
            if sign_up.note:
                item["note"] = sign_up.note
            candidate = json.dumps([*kept, item], ensure_ascii=False)
            if len(candidate) > CONTEXT_MAX_CHARS:
                break
            kept.append(item)
            context = candidate
        return context

    def awaiting_mail(self, now: datetime) -> list[SignUp]:
        """Sign-ups that mail from their site would settle: waiting for confirmation, or blocked, lately."""
        with Session(self.engine) as session:
            records = session.scalars(
                select(SignUpRecord).where(SignUpRecord.status.in_((CONFIRM_EMAIL, BLOCKED)),
                                           SignUpRecord.asked_at > now - MAIL_WAIT)
                .order_by(SignUpRecord.asked_at.desc())
            ).all()
            return [sign_up_view(record) for record in records]

    def settled_by_mail(self, sign_up_id: UUID, now: datetime, note: str) -> bool:
        """Done, from the site's own mail; False if it was settled already or another sign-up holds the site."""
        note = " ".join(note.split())[:NOTE_MAX_CHARS]
        try:
            with Session(self.engine) as session, session.begin():
                result = session.execute(
                    update(SignUpRecord)
                    .where(SignUpRecord.id == sign_up_id, SignUpRecord.status.in_((CONFIRM_EMAIL, BLOCKED)))
                    .values(status=DONE, blocked_by=None, note=note, updated_at=now, finished_at=now)
                    .execution_options(synchronize_session=False)
                )
                return result.rowcount == 1
        except IntegrityError:
            return False  # she already has an account there

    def claim_stalled(self, now: datetime) -> list[SignUp]:
        """Unconfirmed sign-ups whose email hasn't come for STALLED_AFTER, marked as told so each is told once."""
        with Session(self.engine) as session, session.begin():
            session.execute(text("LOCK TABLE public.sign_ups IN SHARE ROW EXCLUSIVE MODE"))
            records = session.scalars(
                select(SignUpRecord).where(
                    SignUpRecord.status == CONFIRM_EMAIL, SignUpRecord.stalled_told_at.is_(None),
                    SignUpRecord.updated_at < now - STALLED_AFTER,
                )
            ).all()
            for record in records:
                record.stalled_told_at = now
            return [sign_up_view(record) for record in records]
