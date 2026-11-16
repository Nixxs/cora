"""The last Claude Code session Cora ran in each project folder, kept across restarts so "continue"
carries on her own session (see claude_code/runner.py).

A folder not run in for KEPT_DAYS is forgotten, as Claude Code clears its transcripts after 30
days by default, and a session without its transcript can't be continued.
"""

from datetime import timedelta
import re

from sqlalchemy import Engine, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .models import ClaudeCodeSessionRecord

KEPT_DAYS = 30
FOLDER_MAX_CHARS = 1000
SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


class ClaudeCodeSessionStore:
    """Use a caller-owned engine; each operation owns and closes its session."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def sessions(self) -> dict[str, str]:
        """{case-folded folder: session id}, without any gone unused too long."""
        with Session(self.engine) as session, session.begin():
            session.execute(delete(ClaudeCodeSessionRecord).where(
                ClaudeCodeSessionRecord.ran_at < func.now() - timedelta(days=KEPT_DAYS),
            ))
            rows = session.execute(select(ClaudeCodeSessionRecord.folder, ClaudeCodeSessionRecord.session_id))
            return {folder: session_id for folder, session_id in rows}

    def remember(self, folder: str, session_id: str) -> None:
        """This folder's session is now session_id (ValueError for one that can't be kept)."""
        if not folder.strip() or len(folder) > FOLDER_MAX_CHARS or not SESSION_ID.match(session_id):
            raise ValueError("That folder or session id can't be kept.")
        statement = insert(ClaudeCodeSessionRecord).values(folder=folder, session_id=session_id)
        statement = statement.on_conflict_do_update(
            index_elements=[ClaudeCodeSessionRecord.folder],
            set_={"session_id": statement.excluded.session_id, "ran_at": func.now()},
        )
        with Session(self.engine) as session, session.begin():
            session.execute(statement)
