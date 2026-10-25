"""Save which outside services are switched on; a service with no saved choice is off."""

from sqlalchemy import Engine, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from services.catalogue import SERVICES
from .models import ServiceSettingRecord


class ServiceSettingsStore:
    """Use a caller-owned engine; each operation owns and closes its session."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def enabled_services(self) -> set[str]:
        """Services switched on; a saved service no longer in the catalogue is ignored."""
        with Session(self.engine) as session:
            saved = session.execute(
                select(ServiceSettingRecord.service).where(ServiceSettingRecord.enabled)
            ).scalars().all()
        return {service for service in saved if service in SERVICES}

    def set_enabled(self, service: str, enabled: bool) -> None:
        if service not in SERVICES:
            raise ValueError(f"Unknown service: {service}")
        statement = insert(ServiceSettingRecord).values(service=service, enabled=enabled)
        statement = statement.on_conflict_do_update(
            index_elements=[ServiceSettingRecord.service],
            set_={"enabled": statement.excluded.enabled, "updated_at": func.now()},
        )
        with Session(self.engine) as session, session.begin():
            session.execute(statement)
