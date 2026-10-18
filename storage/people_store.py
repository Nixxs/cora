"""Read and name who Cora is talking with; the app has one person for now."""

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from .models import PersonRecord


class PeopleStore:
    """Use a caller-owned engine; each operation owns and closes its session."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def current_person(self) -> PersonRecord | None:
        """Return the person Cora met first, or None before she has met anyone."""
        with Session(self.engine) as session:
            return session.scalars(
                select(PersonRecord).order_by(PersonRecord.first_met_at, PersonRecord.id).limit(1)
            ).first()

    def set_current_name(self, display_name: str) -> None:
        """Name the current person, meeting them now if Cora has met nobody yet."""
        with Session(self.engine) as session, session.begin():
            person = session.scalars(
                select(PersonRecord).order_by(PersonRecord.first_met_at, PersonRecord.id)
                .limit(1).with_for_update()
            ).first()
            if person is None:
                session.add(PersonRecord(display_name=display_name))
            else:
                person.display_name = display_name
