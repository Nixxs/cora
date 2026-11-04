"""Devices paired with the web interface. Only each token's SHA-256 is kept, never the token itself."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import Engine, delete, func, select, update
from sqlalchemy.orm import Session

from .models import WebDeviceRecord

NAME_MAX_CHARS = 60


@dataclass(frozen=True)
class WebDevice:
    id: UUID
    name: str
    paired_at: datetime
    last_seen_at: datetime


def device_name(name: str) -> str:
    """A tidy name for the list, from whatever the browser sent."""
    name = " ".join(str(name).split())[:NAME_MAX_CHARS]
    return name or "Browser"


def _device(row: WebDeviceRecord) -> WebDevice:
    return WebDevice(row.id, row.name, row.paired_at, row.last_seen_at)


class WebDeviceStore:
    """Use a caller-owned engine; each operation owns and closes its session."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def devices(self) -> list[WebDevice]:
        """Newest first."""
        with Session(self.engine) as session:
            rows = session.execute(select(WebDeviceRecord).order_by(WebDeviceRecord.paired_at.desc())).scalars()
            return [_device(row) for row in rows]

    def add(self, name: str, token_hash: str) -> WebDevice:
        with Session(self.engine) as session, session.begin():
            row = WebDeviceRecord(name=device_name(name), token_hash=token_hash)
            session.add(row)
            session.flush()
            session.refresh(row)
            return _device(row)

    def find(self, token_hash: str) -> WebDevice | None:
        """The device holding this token, marked as seen now; None if it isn't (or is no longer) paired."""
        with Session(self.engine) as session, session.begin():
            row = session.execute(
                update(WebDeviceRecord).where(WebDeviceRecord.token_hash == token_hash)
                .values(last_seen_at=func.now()).returning(WebDeviceRecord)
            ).scalar_one_or_none()
            return _device(row) if row is not None else None

    def remove(self, device_id: UUID) -> None:
        with Session(self.engine) as session, session.begin():
            session.execute(delete(WebDeviceRecord).where(WebDeviceRecord.id == device_id))
