"""Save the reactions Cora reports, within the limits, and read back how she feels now."""

import argparse
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from storage.database import build_engine
from storage.models import ConversationRecord, FeelingEventRecord
from subconscious.emotions.feeling_rules import (
    LOOKBACK, MAX_PER_CONVERSATION, REASON_MAX_CHARS, SAME_FEELING_GAP, FeelingNow, Reaction, feelings_now,
    feelings_state, levels_at, validate_reaction,
)


class FeelingLimit(Exception):
    """A reaction the limits turned away; the message says why, for her."""


class FeelingStore:
    """Use a caller-owned engine; each operation owns and closes its session."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def record(self, feeling: str, strength: str, reason: str, now: datetime,
               conversation_id: UUID | None = None) -> FeelingEventRecord:
        """Save one reaction. In a conversation, at most MAX_PER_CONVERSATION, and the same feeling
        not again within SAME_FEELING_GAP; raises FeelingLimit otherwise."""
        validate_reaction(feeling, strength)
        reason = " ".join(reason.split())
        if not reason:
            raise ValueError("A feeling needs a reason")
        reason = reason[:REASON_MAX_CHARS]
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            if conversation_id is not None:
                # Locking the conversation keeps two reports at once from both slipping under the limits.
                if session.scalar(select(ConversationRecord.id).where(
                        ConversationRecord.id == conversation_id).with_for_update()) is None:
                    raise LookupError("Conversation does not exist")
                in_conversation = FeelingEventRecord.conversation_id == conversation_id
                if session.scalar(select(func.count()).where(in_conversation)) >= MAX_PER_CONVERSATION:
                    raise FeelingLimit("You've noted enough feelings for this conversation; just let them be.")
                if session.scalar(select(FeelingEventRecord.id).where(
                        in_conversation, FeelingEventRecord.feeling == feeling,
                        FeelingEventRecord.felt_at > now - SAME_FEELING_GAP).limit(1)) is not None:
                    raise FeelingLimit(f"You noted {feeling} only a moment ago; it's still there.")
            record = FeelingEventRecord(feeling=feeling, strength=strength, reason=reason, felt_at=now,
                                        conversation_id=conversation_id)
            session.add(record)
        return record

    def reactions(self, now: datetime, since: datetime | None = None) -> list[Reaction]:
        """Reactions from `since` (by default as far back as still counts) up to `now`, oldest first."""
        with Session(self.engine) as session:
            rows = session.execute(select(
                FeelingEventRecord.feeling, FeelingEventRecord.strength, FeelingEventRecord.felt_at,
                FeelingEventRecord.reason,
            ).where(
                FeelingEventRecord.felt_at > (now - LOOKBACK if since is None else since),
                FeelingEventRecord.felt_at <= now,
            ).order_by(FeelingEventRecord.felt_at, FeelingEventRecord.id)).all()
        return [Reaction(feeling, strength, felt_at, reason) for feeling, strength, felt_at, reason in rows]

    def levels(self, now: datetime) -> dict[str, float]:
        return levels_at(self.reactions(now), now)

    def feelings_now(self, now: datetime) -> list[str]:
        return feelings_now(self.reactions(now), now)

    def state(self, now: datetime) -> list[FeelingNow]:
        return feelings_state(self.reactions(now), now)


def main() -> None:
    parser = argparse.ArgumentParser(description="Cora's feelings now, or note one by hand to try it.")
    parser.add_argument("--add", nargs=3, metavar=("FEELING", "STRENGTH", "REASON"),
                        help="note a reaction outside any conversation, e.g. --add excitement medium 'a new idea'")
    args = parser.parse_args()
    engine = build_engine()
    try:
        store = FeelingStore(engine)
        now = datetime.now(timezone.utc)
        if args.add:
            store.record(*args.add, now=now)
        for reaction in store.reactions(now):
            print(f"{reaction.at.astimezone():%H:%M} {reaction.feeling} ({reaction.strength}): {reaction.reason}")
        print()
        for feeling, level in store.levels(now).items():
            print(f"{feeling:<12} {level:.2f}")
        print()
        print("\n".join(store.feelings_now(now)) or "No feeling is noticeable now.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
