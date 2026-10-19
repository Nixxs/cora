import os
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

import psycopg
from psycopg import sql
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from migrate import apply_migrations, database_settings
from session_config import BACKEND_MODEL
from storage.database import build_engine
from storage.model_settings_store import ModelSettingsStore


class ModelSettingsValidationTests(unittest.TestCase):
    def test_unknown_area_or_model_is_rejected_before_touching_the_database(self):
        engine = MagicMock()
        store = ModelSettingsStore(engine)
        with self.assertRaises(ValueError):
            store.set_model("voice", "gpt-6-luna")
        with self.assertRaises(ValueError):
            store.set_model("memory", "gpt-unknown")
        engine.connect.assert_not_called()


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class ModelSettingsStoreDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        settings = database_settings()
        admin = psycopg.connect(**settings, autocommit=True)
        cls.addClassCleanup(admin.close)
        cls.database = "cora_test_model_settings_" + uuid4().hex
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.database)))
        cls.addClassCleanup(
            admin.execute, sql.SQL("DROP DATABASE {}").format(sql.Identifier(cls.database))
        )
        with psycopg.connect(**{**settings, "dbname": cls.database}, autocommit=True) as connection:
            apply_migrations(connection)
        with patch("storage.database.database_settings", return_value={**settings, "dbname": cls.database}):
            cls.engine = build_engine()
        cls.addClassCleanup(cls.engine.dispose)

    def setUp(self):
        # Only this disposable test database is cleared, never Cora's database.
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM public.model_settings"))
        self.store = ModelSettingsStore(self.engine)

    def test_every_area_uses_the_default_until_chosen(self):
        self.assertEqual(self.store.models(), {
            "delegation": BACKEND_MODEL, "memory": BACKEND_MODEL,
            "subconscious": BACKEND_MODEL, "utility": BACKEND_MODEL,
        })

    def test_choice_is_saved_per_area_and_can_be_replaced(self):
        self.store.set_model("memory", "gpt-6-luna")
        self.store.set_model("memory", "gpt-5.6-terra")
        self.assertEqual(self.store.model_for("memory"), "gpt-5.6-terra")
        self.assertEqual(self.store.model_for("utility"), BACKEND_MODEL)

    def test_saved_model_no_longer_offered_falls_back_to_the_default(self):
        with self.engine.begin() as connection:
            connection.execute(text("INSERT INTO public.model_settings (area, model) VALUES ('utility', 'gpt-old')"))
        self.assertEqual(self.store.model_for("utility"), BACKEND_MODEL)

    def test_database_rejects_unknown_areas_and_blank_models(self):
        for area, model in (("voice", "gpt-6-luna"), ("memory", " ")):
            with self.subTest(area=area, model=model), self.assertRaises(IntegrityError):
                with self.engine.begin() as connection:
                    connection.execute(
                        text("INSERT INTO public.model_settings (area, model) VALUES (:area, :model)"),
                        {"area": area, "model": model},
                    )


if __name__ == "__main__":
    unittest.main()
