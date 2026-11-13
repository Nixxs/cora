"""General settings from Settings > General, such as her voice; one without a saved value uses its default."""

from pathlib import Path

from sqlalchemy import Engine, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from game.chat_line import CHANNELS as GAME_CHAT_CHANNELS, DEFAULT_CHANNEL
from game.moments import CHATTINESS, DEFAULT_CHATTINESS
from game.speech import DEFAULT_TRANSCRIPTION, MAX_VOCABULARY_CHARS, TRANSCRIPTION_CHOICES
from claude_code.routes import DEFAULT_PORT as CLAUDE_CODE_PORT
from claude_code.sessions import DEFAULT_TELL as DEFAULT_CLAUDE_CODE_TELL, TELL_CHOICES as CLAUDE_CODE_TELL
from session_config import VOICE, VOICE_CHOICES
from .models import AppSettingRecord
from .webhook_settings_store import PORTS

START_MODES = ("active", "standby")
# As phone.twilio.REGIONS.
TWILIO_REGIONS = ("au1", "us1", "ie1")
# Minutes of quiet before automatic standby; 0 turns it off.
MAX_IDLE_MINUTES = 720
# Minutes of quiet that end a conversation (in standby, and contacts' chats).
MAX_GAP_MINUTES = 1440
# Saved for "no second backup copy", which overrides .env; a folder is always an absolute path.
NO_BACKUP_COPY = "none"
BACKUP_COPY_MAX_CHARS = 200


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

    def conversation_gap(self) -> int | None:
        """Minutes of quiet that end a conversation; None when never set here."""
        saved = self._get("conversation_gap")
        return int(saved) if saved is not None and saved.isdigit() and 1 <= int(saved) <= MAX_GAP_MINUTES else None

    def set_conversation_gap(self, minutes: int) -> None:
        if not isinstance(minutes, int) or isinstance(minutes, bool) or not 1 <= minutes <= MAX_GAP_MINUTES:
            raise ValueError(f"Minutes must be 1 to {MAX_GAP_MINUTES}")
        self._set("conversation_gap", str(minutes))

    def backup_copy_dir(self) -> str | None:
        """The second backup folder, "" for none chosen here, or None when never set here (.env applies)."""
        saved = self._get("backup_copy_dir")
        if saved is None:
            return None
        return "" if saved == NO_BACKUP_COPY else saved

    def set_backup_copy_dir(self, folder: str) -> None:
        """Save the second backup folder (an absolute path), or "" for none."""
        folder = folder.strip()
        if not folder:
            self._set("backup_copy_dir", NO_BACKUP_COPY)
            return
        if len(folder) > BACKUP_COPY_MAX_CHARS or not Path(folder).is_absolute():
            raise ValueError("A backup folder needs its full path")
        self._set("backup_copy_dir", folder)

    def twilio_region(self) -> str | None:
        """The Twilio region for calls she places ('au1', 'us1' or 'ie1'); None when never set here."""
        saved = self._get("twilio_region")
        return saved if saved in TWILIO_REGIONS else None

    def set_twilio_region(self, code: str) -> None:
        if code not in TWILIO_REGIONS:
            raise ValueError(f"Unknown Twilio region: {code}")
        self._set("twilio_region", code)

    def game_push_to_talk(self) -> bool:
        """Whether holding the middle mouse button speaks into the game's chat; off unless switched on."""
        return self._switch("game_push_to_talk") is True

    def set_game_push_to_talk(self, on: bool) -> None:
        self._set("game_push_to_talk", "on" if on else "off")

    def game_chat_channel(self) -> str:
        """The game chat channel push-to-talk uses ('say' unless another was chosen)."""
        saved = self._get("game_chat_channel")
        return saved if saved in GAME_CHAT_CHANNELS else DEFAULT_CHANNEL

    def set_game_chat_channel(self, channel: str) -> None:
        if channel not in GAME_CHAT_CHANNELS:
            raise ValueError(f"Unknown game chat channel: {channel}")
        self._set("game_chat_channel", channel)

    def game_watch(self) -> bool:
        """Whether she watches the game through the Decisions API; off unless switched on."""
        return self._switch("game_watch") is True

    def set_game_watch(self, on: bool) -> None:
        self._set("game_watch", "on" if on else "off")

    def game_chattiness(self) -> str:
        """How often she reacts to the game: 'off', 'quiet', 'normal' (unless another was chosen) or 'chatty'."""
        saved = self._get("game_chattiness")
        return saved if saved in CHATTINESS else DEFAULT_CHATTINESS

    def set_game_chattiness(self, level: str) -> None:
        if level not in CHATTINESS:
            raise ValueError(f"Unknown chattiness: {level}")
        self._set("game_chattiness", level)

    def game_transcription(self) -> str:
        """How push-to-talk turns speech into text: 'openai' (unless another was chosen) or 'local'."""
        saved = self._get("game_transcription")
        return saved if saved in TRANSCRIPTION_CHOICES else DEFAULT_TRANSCRIPTION

    def set_game_transcription(self, choice: str) -> None:
        if choice not in TRANSCRIPTION_CHOICES:
            raise ValueError(f"Unknown transcription: {choice}")
        self._set("game_transcription", choice)

    def game_vocabulary(self) -> str:
        """The person's own names and words for push-to-talk's spelling ("" for none)."""
        return self._get("game_vocabulary") or ""

    def set_game_vocabulary(self, words: str) -> None:
        """Save them ("" removes them)."""
        words = " ".join(words.split())
        if len(words) > MAX_VOCABULARY_CHARS:
            raise ValueError(f"At most {MAX_VOCABULARY_CHARS} characters")
        if not words:
            with Session(self.engine) as session, session.begin():
                session.execute(delete(AppSettingRecord).where(AppSettingRecord.key == "game_vocabulary"))
            return
        self._set("game_vocabulary", words)

    def claude_code_listen(self) -> bool:
        """Whether Cora listens for Claude Code's hooks; off unless switched on."""
        return self._switch("claude_code_listen") is True

    def set_claude_code_listen(self, on: bool) -> None:
        self._set("claude_code_listen", "on" if on else "off")

    def claude_code_tell(self) -> str:
        """Which sessions left waiting she tells you about: 'all' (unless another was chosen), 'needs' or 'off'."""
        saved = self._get("claude_code_tell")
        return saved if saved in CLAUDE_CODE_TELL else DEFAULT_CLAUDE_CODE_TELL

    def set_claude_code_tell(self, choice: str) -> None:
        if choice not in CLAUDE_CODE_TELL:
            raise ValueError(f"Unknown choice: {choice}")
        self._set("claude_code_tell", choice)

    def claude_code_port(self) -> int:
        """The local port Claude Code's hooks post to (8001 unless another was chosen)."""
        saved = self._get("claude_code_port")
        return int(saved) if saved is not None and saved.isdigit() and int(saved) in PORTS else CLAUDE_CODE_PORT

    def set_claude_code_port(self, port: int) -> None:
        if isinstance(port, bool) or not isinstance(port, int) or port not in PORTS:
            raise ValueError(f"The local port must be between {PORTS.start} and {PORTS.stop - 1}.")
        self._set("claude_code_port", str(port))

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
