"""General settings from Settings > General, such as her voice; one without a saved value uses its default."""

from sqlalchemy import Engine, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from session_config import VOICE, VOICE_CHOICES
from .models import AppSettingRecord

START_MODES = ("active", "standby")
# Minutes of quiet before automatic standby; 0 turns it off.
MAX_IDLE_MINUTES = 720


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

    def start_mode(self) -> str:
        """How she starts when the app opens: 'active' (the default) or 'standby'."""
        saved = self._get("start_mode")
        return saved if saved in START_MODES else "active"

    def set_start_mode(self, mode: str) -> None:
        if mode not in START_MODES:
            raise ValueError(f"Unknown start mode: {mode}")
        self._set("start_mode", mode)

    def idle_minutes(self) -> int | None:
        """Minutes of quiet before automatic standby (0 is off); None when never set here."""
        saved = self._get("idle_minutes")
        return int(saved) if saved is not None and saved.isdigit() and int(saved) <= MAX_IDLE_MINUTES else None

    def set_idle_minutes(self, minutes: int) -> None:
        if not isinstance(minutes, int) or isinstance(minutes, bool) or not 0 <= minutes <= MAX_IDLE_MINUTES:
            raise ValueError(f"Minutes must be 0 to {MAX_IDLE_MINUTES}")
        self._set("idle_minutes", str(minutes))

    def _switch(self, key: str) -> bool | None:
        saved = self._get(key)
        return {"on": True, "off": False}.get(saved)

    def standby_farewell(self) -> bool | None:
        """Whether she gives a heads-up before automatic standby; None when never set here."""
        return self._switch("standby_farewell")

    def set_standby_farewell(self, on: bool) -> None:
        self._set("standby_farewell", "on" if on else "off")

    def wake_word(self) -> bool | None:
        """Whether her wake word is on (the Wake button's last state); None when never set here."""
        return self._switch("wake_word")

    def set_wake_word(self, on: bool) -> None:
        self._set("wake_word", "on" if on else "off")

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
