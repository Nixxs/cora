"""The last Claude Code session Cora ran in each project folder, kept across restarts."""

import os
import unittest
from unittest.mock import patch
from uuid import uuid4

import psycopg
from psycopg import sql
from sqlalchemy import text

from migrate import apply_migrations, database_settings
from storage.claude_code_session_store import ClaudeCodeSessionStore
from storage.database import build_engine


class SessionStoreCheckTests(unittest.TestCase):
    def test_only_a_real_folder_and_session_id_are_kept(self):
        store = ClaudeCodeSessionStore(engine=None)
        for folder, session_id in ((" ", "s1"), ("d:\\x" * 300, "s1"), ("d:\\site", "a b"), ("d:\\site", "")):
            with self.assertRaises(ValueError):
                store.remember(folder, session_id)


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class SessionStoreDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        settings = database_settings()
        admin = psycopg.connect(**settings, autocommit=True)
        cls.addClassCleanup(admin.close)
        cls.database = "cora_test_claude_code_sessions_" + uuid4().hex
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.database)))
        cls.addClassCleanup(admin.execute, sql.SQL("DROP DATABASE {}").format(sql.Identifier(cls.database)))
        with psycopg.connect(**{**settings, "dbname": cls.database}, autocommit=True) as connection:
            apply_migrations(connection)
        with patch("storage.database.database_settings", return_value={**settings, "dbname": cls.database}):
            cls.engine = build_engine()
        cls.addClassCleanup(cls.engine.dispose)

    def test_each_folder_keeps_its_latest_session(self):
        store = ClaudeCodeSessionStore(self.engine)
        store.remember("d:\\development\\site", "first-1")
        store.remember("d:\\development\\site", "second-2")
        store.remember("d:\\addons\\combodots", "dots-3")
        saved = store.sessions()
        self.assertEqual(saved["d:\\development\\site"], "second-2")
        self.assertEqual(saved["d:\\addons\\combodots"], "dots-3")

    def test_a_folder_not_run_in_for_30_days_is_forgotten(self):
        store = ClaudeCodeSessionStore(self.engine)
        store.remember("d:\\old", "old-1")
        store.remember("d:\\recent", "recent-1")
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE public.claude_code_sessions SET ran_at = now() - interval '31 days' "
                                    "WHERE folder = 'd:\\old'"))
            connection.execute(text("UPDATE public.claude_code_sessions SET ran_at = now() - interval '29 days' "
                                    "WHERE folder = 'd:\\recent'"))
        saved = store.sessions()
        self.assertNotIn("d:\\old", saved)
        self.assertEqual(saved["d:\\recent"], "recent-1")


if __name__ == "__main__":
    unittest.main()
