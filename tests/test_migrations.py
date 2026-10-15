import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

import psycopg
from psycopg import sql

from migrate import apply_migrations, database_settings, read_migrations


class MigrationFileTests(unittest.TestCase):
    def test_files_are_ordered_and_windows_newlines_have_same_checksum(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "0002_second.sql").write_text("SELECT 2;\n", encoding="utf-8")
            first = root / "0001_first.sql"
            first.write_bytes(b"SELECT 1;\r\n")
            migrations = read_migrations(root)
            self.assertEqual([row[0] for row in migrations], ["0001_first.sql", "0002_second.sql"])
            first.write_bytes(b"SELECT 1;\n")
            self.assertEqual(migrations[0][2], read_migrations(root)[0][2])

    def test_empty_invalid_and_duplicate_files_are_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "No SQL migrations"):
                read_migrations(root)
            invalid = root / "first.sql"
            invalid.write_text("SELECT 1;", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Invalid migration filename"):
                read_migrations(root)
            invalid.unlink()
            for name in ("0001_first.sql", "0001_second.sql"):
                (root / name).write_text("SELECT 1;", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Duplicate migration number"):
                read_migrations(root)

    def test_settings_use_database_file_and_environment_overrides(self):
        values = {"CORA_DB_NAME": "example", "CORA_DB_USER": "example", "CORA_DB_PASSWORD": "test-only"}
        with patch("migrate.dotenv_values", return_value=values), \
                patch.dict(os.environ, {"CORA_DB_PORT": "55432"}, clear=True):
            settings = database_settings()
            self.assertEqual(settings["dbname"], "example")
            self.assertEqual(settings["host"], "127.0.0.1")
            self.assertEqual(settings["port"], "55432")
        with patch("migrate.dotenv_values", return_value={}), patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "Missing database settings"):
                database_settings()


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class MigrationDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Never reset Cora's real database: create/drop a uniquely named test DB.
        cls.settings = database_settings()
        admin = psycopg.connect(**cls.settings, autocommit=True)
        cls.addClassCleanup(admin.close)
        cls.database = "cora_test_migrations_" + uuid4().hex
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.database)))
        cls.addClassCleanup(
            admin.execute, sql.SQL("DROP DATABASE {}").format(sql.Identifier(cls.database))
        )

    def setUp(self):
        self.connection = psycopg.connect(
            **{**self.settings, "dbname": self.database}, autocommit=True
        )
        self.addCleanup(self.connection.close)
        self.connection.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def write_migration(self, name, script):
        (self.directory / name).write_text(script, encoding="utf-8")

    def test_vector_applies_once_and_history_has_timestamp(self):
        expected = [
            "0001_enable_vector.sql", "0002_create_conversations_and_messages.sql",
            "0003_create_conversation_summaries.sql",
            "0004_create_memories.sql",
            "0005_add_memory_embeddings.sql",
            "0006_mark_duplicate_memories.sql",
            "0007_create_interests.sql",
        ]
        self.assertEqual(apply_migrations(self.connection), expected)
        self.assertEqual(apply_migrations(self.connection), [])
        version = self.connection.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()
        self.assertIsNotNone(version)
        history = self.connection.execute("SELECT name, applied_at FROM public.schema_migrations ORDER BY name").fetchall()
        self.assertEqual([row[0] for row in history], expected)
        self.assertTrue(all(row[1].tzinfo is not None for row in history))

    def test_failed_batch_rolls_back_schema_and_history_then_can_retry(self):
        self.write_migration("0001_first.sql", "CREATE TABLE public.rollback_marker (id integer);")
        self.write_migration("0002_second.sql", "INSERT INTO missing_table VALUES (1);")
        with self.assertRaises(psycopg.Error):
            apply_migrations(self.connection, self.directory)
        for name in ("public.rollback_marker", "public.schema_migrations"):
            self.assertIsNone(self.connection.execute("SELECT to_regclass(%s)", (name,)).fetchone()[0])
        self.write_migration("0002_second.sql", "INSERT INTO public.rollback_marker VALUES (1);")
        self.assertEqual(len(apply_migrations(self.connection, self.directory)), 2)
        self.assertEqual(self.connection.execute("SELECT id FROM public.rollback_marker").fetchone(), (1,))

    def test_changed_applied_file_is_rejected_without_running_pending_sql(self):
        self.write_migration("0001_first.sql", "SELECT 1;")
        apply_migrations(self.connection, self.directory)
        self.write_migration("0001_first.sql", "SELECT 2;")
        self.write_migration("0002_second.sql", "CREATE TABLE public.should_not_exist (id integer);")
        with self.assertRaisesRegex(ValueError, "Applied migration was changed"):
            apply_migrations(self.connection, self.directory)
        self.assertIsNone(self.connection.execute("SELECT to_regclass('public.should_not_exist')").fetchone()[0])
        self.assertEqual(self.connection.execute("SELECT count(*) FROM public.schema_migrations").fetchone()[0], 1)

    def test_removed_applied_file_is_rejected(self):
        self.write_migration("0001_first.sql", "SELECT 1;")
        apply_migrations(self.connection, self.directory)
        (self.directory / "0001_first.sql").unlink()
        self.write_migration("0002_second.sql", "SELECT 2;")
        with self.assertRaisesRegex(ValueError, "Migration history differs"):
            apply_migrations(self.connection, self.directory)


if __name__ == "__main__":
    unittest.main()