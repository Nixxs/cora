"""Save Cora's listener setup: webhooks, the web interface and phone calls on or off, the public address that reaches
her, and her local port."""

from dataclasses import dataclass
import re
from urllib.parse import urlsplit

from sqlalchemy import Engine, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .models import WebhookSettingRecord

DEFAULT_PORT = 8000
PORTS = range(1024, 65536)


@dataclass(frozen=True)
class WebhookSettings:
    enabled: bool = False
    # https://host[:port], without a path: Cora adds her own secret paths to it.
    public_url: str | None = None
    port: int = DEFAULT_PORT
    # The web interface, served by the same listener at the same address.
    web_enabled: bool = False
    # Phone calls to her number: Twilio asks the same listener what to do with each one.
    phone_enabled: bool = False
    # Calling out for tasks: the Twilio account (AC...) and her own number (+61...) to call from.
    twilio_account_sid: str | None = None
    phone_number: str | None = None

    @property
    def listening(self) -> bool:
        """Whether the listener runs: something is on, and there's an address to reach it."""
        return bool(self.public_url) and (self.enabled or self.web_enabled or self.phone_enabled)


def normalise_public_url(value: str) -> str:
    """'https://Cora.Example.com/' -> 'https://cora.example.com'; ValueError for anything else."""
    parts = urlsplit(value.strip())
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise ValueError("The public address must start with https:// (Microsoft only calls HTTPS).")
    if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username or parts.password:
        raise ValueError("Give only the address, such as https://cora.example.com, without a path.")
    host = parts.hostname.lower()
    return f"https://{host}" + (f":{parts.port}" if parts.port else "")


ACCOUNT_SID = re.compile(r"AC[0-9a-fA-F]{32}")
PHONE_NUMBER = re.compile(r"\+[1-9][0-9]{7,14}")


def check_account_sid(value: str) -> str:
    value = value.strip()
    if value and not ACCOUNT_SID.fullmatch(value):
        raise ValueError("That isn't a Twilio Account SID (AC and 32 letters and digits, from the Twilio console).")
    return value


def check_phone_number(value: str) -> str:
    """'+61 485 008 128' -> '+61485008128'; ValueError for anything not a full international number."""
    value = "".join(value.split())
    if value and not PHONE_NUMBER.fullmatch(value):
        raise ValueError("Give the number in full international form, such as +61485008128.")
    return value


def check_port(port: int) -> int:
    if port not in PORTS:
        raise ValueError(f"The local port must be between {PORTS.start} and {PORTS.stop - 1}.")
    return port


class WebhookSettingsStore:
    """Use a caller-owned engine; each operation owns and closes its session."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def settings(self) -> WebhookSettings:
        with Session(self.engine) as session:
            row = session.execute(select(WebhookSettingRecord)).scalar_one_or_none()
        if row is None:
            return WebhookSettings()
        return WebhookSettings(row.enabled, row.public_url, row.port, row.web_enabled, row.phone_enabled,
                               row.twilio_account_sid, row.phone_number)

    def _save(self, **values) -> None:
        statement = insert(WebhookSettingRecord).values(id=1, **values)
        statement = statement.on_conflict_do_update(
            index_elements=[WebhookSettingRecord.id], set_={**values, "updated_at": func.now()},
        )
        with Session(self.engine) as session, session.begin():
            session.execute(statement)

    def set_enabled(self, enabled: bool) -> None:
        self._save(enabled=enabled)

    def set_web_enabled(self, enabled: bool) -> None:
        self._save(web_enabled=enabled)

    def set_phone_enabled(self, enabled: bool) -> None:
        self._save(phone_enabled=enabled)

    def set_twilio_account(self, account_sid: str, phone_number: str) -> None:
        """Both checked; empty clears them (she can't call out then)."""
        self._save(twilio_account_sid=check_account_sid(account_sid) or None,
                   phone_number=check_phone_number(phone_number) or None)

    def set_address(self, public_url: str, port: int) -> None:
        self._save(public_url=normalise_public_url(public_url), port=check_port(port))
