"""Save which outside services, and which of their tools, are switched on; anything not saved is off."""

from sqlalchemy import Engine, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from services.catalogue import SERVICES
from .models import ServiceSettingRecord, ServiceToolRecord


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

    def enabled_tools(self) -> dict[str, set[str]]:
        """Each service's switched-on tools; services no longer in the catalogue are ignored."""
        with Session(self.engine) as session:
            rows = session.execute(select(ServiceToolRecord.service, ServiceToolRecord.tool)).all()
        tools: dict[str, set[str]] = {}
        for service, tool in rows:
            if service in SERVICES:
                tools.setdefault(service, set()).add(tool)
        return tools

    def set_tool_enabled(self, service: str, tool: str, enabled: bool) -> None:
        if service not in SERVICES:
            raise ValueError(f"Unknown service: {service}")
        if not tool.strip():
            raise ValueError("A tool needs a name")
        with Session(self.engine) as session, session.begin():
            if enabled:
                session.execute(
                    insert(ServiceToolRecord).values(service=service, tool=tool).on_conflict_do_nothing()
                )
            else:
                session.execute(delete(ServiceToolRecord).where(
                    ServiceToolRecord.service == service, ServiceToolRecord.tool == tool,
                ))
