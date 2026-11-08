"""Key details: what the person told Cora to keep to hand, each a label and its value.

They're free-form ("Nick's email", "Wife (Sarah) mobile", "Car registration"), saved the moment
she's told by note_detail, and all of them reach her prompts in the person's own chats, never a
client's. A label is one entry, whatever its case, so saving it again replaces the value.

    python -m storage.key_details_store                       # show what's saved
    python -m storage.key_details_store set "Nick's email" nick@example.com
    python -m storage.key_details_store forget "Nick's email"
"""

import argparse
import json
import sys

from dotenv import load_dotenv
from sqlalchemy import Engine, delete, func, select
from sqlalchemy.orm import Session

from .database import build_engine
from .models import KeyDetailRecord

LABEL_MAX_CHARS = 100  # matches key_details_label_check
VALUE_MAX_CHARS = 500  # matches key_details_value_check
# Enough for a household's contact details and more; a full list asks her to replace or forget one.
DETAILS_LIMIT = 40
# Every saved detail fits: DETAILS_LIMIT entries of the longest label and value, as JSON.
CONTEXT_MAX_CHARS = DETAILS_LIMIT * (LABEL_MAX_CHARS + VALUE_MAX_CHARS + 30)


class FullError(ValueError):
    """There's no room for a new label; one has to be forgotten first."""


def tidy(text: str, limit: int, what: str) -> str:
    text = " ".join(str(text).split())
    if not text or len(text) > limit:
        raise ValueError(f"A {what} needs 1 to {limit} characters")
    return text


class KeyDetailsStore:
    """Use a caller-owned engine; each operation owns and closes its session."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def save(self, label: str, value: str) -> bool:
        """Save or replace one detail; True when it replaced an earlier value of that label."""
        label = tidy(label, LABEL_MAX_CHARS, "label")
        value = tidy(value, VALUE_MAX_CHARS, "value")
        with Session(self.engine) as session, session.begin():
            existing = session.scalars(
                select(KeyDetailRecord).where(func.lower(KeyDetailRecord.label) == label.lower()).with_for_update()
            ).first()
            if existing is not None:
                existing.label, existing.value, existing.updated_at = label, value, func.now()
                return True
            if session.scalar(select(func.count()).select_from(KeyDetailRecord)) >= DETAILS_LIMIT:
                raise FullError(f"There are already {DETAILS_LIMIT} key details")
            session.add(KeyDetailRecord(label=label, value=value))
            return False

    def forget(self, label: str) -> bool:
        """Remove one detail by its label, whatever its case; False when there was none."""
        label = tidy(label, LABEL_MAX_CHARS, "label")
        with Session(self.engine) as session, session.begin():
            return bool(session.execute(
                delete(KeyDetailRecord).where(func.lower(KeyDetailRecord.label) == label.lower())
            ).rowcount)

    def all(self) -> list[tuple[str, str]]:
        """Every detail as (label, value), by label."""
        with Session(self.engine) as session:
            return list(session.execute(
                select(KeyDetailRecord.label, KeyDetailRecord.value).order_by(func.lower(KeyDetailRecord.label))
            ).all())

    def get_startup_context(self) -> str:
        """Every detail as a JSON object of label to value, for her prompts; empty when there are none."""
        details = self.all()
        return json.dumps(dict(details), ensure_ascii=False) if details else ""


def main() -> None:
    parser = argparse.ArgumentParser(description="Show, set or forget Cora's key details about the person.")
    sub = parser.add_subparsers(dest="command")
    setter = sub.add_parser("set", help="save or replace a detail")
    setter.add_argument("label")
    setter.add_argument("value")
    forgetter = sub.add_parser("forget", help="remove a detail")
    forgetter.add_argument("label")
    args = parser.parse_args()
    load_dotenv()
    engine = build_engine()
    try:
        store = KeyDetailsStore(engine)
        if args.command == "set":
            print("Replaced." if store.save(args.label, args.value) else "Saved.")
        elif args.command == "forget":
            print("Forgotten." if store.forget(args.label) else "There was no detail with that label.")
        else:
            details = store.all()
            for label, value in details:
                print(f"{label}: {value}")
            if not details:
                print("No key details saved yet.")
    except ValueError as error:
        print(error, file=sys.stderr)
        sys.exit(1)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
