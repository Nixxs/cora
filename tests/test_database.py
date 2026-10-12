import os
import unittest
from unittest.mock import MagicMock, patch

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from storage.database import build_engine, main


class DatabaseEngineTests(unittest.TestCase):
    def test_engine_uses_psycopg_and_preserves_settings_without_connecting(self):
        settings = {
            "host": "127.0.0.1", "port": "55432", "dbname": "example",
            "user": "test_user", "password": "test@:/#only", "connect_timeout": 5,
        }
        with patch("storage.database.database_settings", return_value=settings), \
                patch("psycopg.connect") as connect:
            engine = build_engine()
            try:
                self.assertEqual(engine.dialect.driver, "psycopg")
                self.assertEqual(engine.url.host, settings["host"])
                self.assertEqual(engine.url.port, 55432)
                self.assertEqual(engine.url.database, settings["dbname"])
                self.assertEqual(engine.url.username, settings["user"])
                self.assertEqual(engine.url.password, settings["password"])
                connect.assert_not_called()
            finally:
                engine.dispose()

    def test_check_queries_database_and_disposes_engine(self):
        engine = MagicMock()
        connection = engine.connect.return_value.__enter__.return_value
        connection.execute.return_value.one.return_value = ("example", "test_user")
        with patch("storage.database.build_engine", return_value=engine), patch("builtins.print") as output:
            main()
        self.assertEqual(str(connection.execute.call_args.args[0]), "SELECT current_database(), current_user")
        output.assert_called_once_with("SQLAlchemy connected: database=example, user=test_user")
        engine.connect.return_value.__exit__.assert_called_once()
        engine.dispose.assert_called_once()

    def test_connection_failure_exits_and_disposes_engine(self):
        engine = MagicMock()
        engine.connect.side_effect = SQLAlchemyError("Connection unavailable")
        with patch("storage.database.build_engine", return_value=engine):
            with self.assertRaisesRegex(SystemExit, "Database connection failed"):
                main()
        engine.dispose.assert_called_once()

    def test_invalid_settings_exit_before_building_engine(self):
        with patch("storage.database.database_settings", side_effect=ValueError("Missing database settings")), \
                patch("storage.database.create_engine") as create:
            with self.assertRaisesRegex(SystemExit, "Missing database settings"):
                main()
        create.assert_not_called()


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class DatabaseConnectionTests(unittest.TestCase):
    def test_sqlalchemy_can_read_without_changing_tables_or_migration_history(self):
        engine = build_engine()
        self.addCleanup(engine.dispose)
        with engine.connect() as connection:
            tables = connection.execute(text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
            )).all()
            history = connection.execute(text("SELECT * FROM public.schema_migrations ORDER BY name")).all()
            self.assertEqual(connection.execute(text("SELECT 1")).scalar_one(), 1)
            with patch("builtins.print") as output:
                main()
            self.assertIn("SQLAlchemy connected:", output.call_args.args[0])
            self.assertEqual(connection.execute(text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
            )).all(), tables)
            self.assertEqual(connection.execute(text(
                "SELECT * FROM public.schema_migrations ORDER BY name"
            )).all(), history)


if __name__ == "__main__":
    unittest.main()