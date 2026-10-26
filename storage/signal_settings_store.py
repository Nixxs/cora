"""Save Cora's Signal setup: whether it is on, her linked number, and who she answers."""

from dataclasses import dataclass
import re

from sqlalchemy import Engine, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .models import SignalSettingRecord

# International format, as Signal uses: a plus and 8 to 15 digits.
NUMBER_PATTERN = re.compile(r"^\+[1-9]\d{7,14}$")


@dataclass(frozen=True)
class SignalSettings:
    enabled: bool = False
    account: str | None = None  # Cora's own number, once linked
    allowed_number: str | None = None  # the person's number


def normalise_number(number: str) -> str:
    """'+61 400 000 000' -> '+61400000000'; raises for anything that isn't a full number."""
    compact = re.sub(r"[\s\-()]", "", number)
    if not NUMBER_PATTERN.match(compact):
        raise ValueError("Use the full international number, such as +61400000000")
    return compact


class SignalSettingsStore:
    """Use a caller-owned engine; each operation owns and closes its session."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def settings(self) -> SignalSettings:
        with Session(self.engine) as session:
            row = session.execute(select(SignalSettingRecord)).scalar_one_or_none()
        if row is None:
            return SignalSettings()
        return SignalSettings(row.enabled, row.account, row.allowed_number)

    def _save(self, **values) -> None:
        statement = insert(SignalSettingRecord).values(id=1, **values)
        statement = statement.on_conflict_do_update(
            index_elements=[SignalSettingRecord.id], set_={**values, "updated_at": func.now()},
        )
        with Session(self.engine) as session, session.begin():
            session.execute(statement)

    def set_enabled(self, enabled: bool) -> None:
        self._save(enabled=enabled)

    def set_allowed_number(self, number: str) -> None:
        number = normalise_number(number)
        if number == self.settings().account:
            raise ValueError("That is Cora's own number, not yours")
        self._save(allowed_number=number)

    def set_account(self, account: str | None) -> None:
        if account is not None:
            account = normalise_number(account)
            if account == self.settings().allowed_number:
                raise ValueError("That is your number, not Cora's")
        self._save(account=account)
