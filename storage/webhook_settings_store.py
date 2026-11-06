"""Save Cora's listener setup: webhooks, the web interface and phone calls on or off, the public address that reaches
her, and her local port."""

from dataclasses import dataclass
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
        return WebhookSettings(row.enabled, row.public_url, row.port, row.web_enabled, row.phone_enabled)

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

    def set_address(self, public_url: str, port: int) -> None:
        self._save(public_url=normalise_public_url(public_url), port=check_port(port))
