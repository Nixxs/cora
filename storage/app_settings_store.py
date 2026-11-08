"""General settings from Settings > General, such as her voice; one without a saved value uses its default."""

from sqlalchemy import Engine, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from session_config import VOICE, VOICE_CHOICES
from .models import AppSettingRecord


class AppSettingsStore:
    """Use a caller-owned engine; each operation owns and closes its session."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def _get(self, key: str) -> str | None:
        with Session(self.engine) as session:
            return session.scalar(select(AppSettingRecord.value).where(AppSettingRecord.key == key))

    def _set(self, key: str, value: str) -> None:
        statement = insert(AppSettingRecord).values(key=key, value=value)
        statement = statement.on_conflict_do_update(
            index_elements=[AppSettingRecord.key],
            set_={"value": statement.excluded.value, "updated_at": func.now()},
        )
        with Session(self.engine) as session, session.begin():
            session.execute(statement)

    def voice(self) -> str:
        """Her voice; one saved but no longer offered falls back to the default."""
        saved = self._get("voice")
        return saved if saved in VOICE_CHOICES else VOICE

    def set_voice(self, voice: str) -> None:
        if voice not in VOICE_CHOICES:
            raise ValueError(f"Unknown voice: {voice}")
        self._set("voice", voice)

    def spend_project(self) -> str | None:
        """Whose OpenAI spend to show: a project's id or 'all', or None for automatic."""
        return self._get("spend_project")

    def set_spend_project(self, project: str | None) -> None:
        """Save whose spend to show; None goes back to automatic."""
        if project is None:
            with Session(self.engine) as session, session.begin():
                session.execute(delete(AppSettingRecord).where(AppSettingRecord.key == "spend_project"))
            return
        project = project.strip()
        if not project or len(project) > 200 or any(character.isspace() for character in project):
            raise ValueError("A project needs its id")
        self._set("spend_project", project)
