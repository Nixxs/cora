from datetime import datetime, timezone
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

import psycopg
from psycopg import sql
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from migrate import apply_migrations, database_settings
from storage.database import build_engine
from storage.models import PersonRecord
from storage.people_store import PeopleStore


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class PeopleStoreDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        settings = database_settings()
        admin = psycopg.connect(**settings, autocommit=True)
        cls.addClassCleanup(admin.close)
        cls.database = "cora_test_people_" + uuid4().hex
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
            connection.execute(text("DELETE FROM public.people"))
        self.store = PeopleStore(self.engine)

    def add(self, **values):
        with Session(self.engine) as session, session.begin():
            session.add(PersonRecord(**values))

    def test_no_person_before_cora_has_met_anyone(self):
        self.assertIsNone(self.store.current_person())

    def test_current_person_is_the_one_met_first(self):
        self.add(display_name="Later", first_met_at=datetime(2026, 5, 1, tzinfo=timezone.utc))
        self.add(display_name=None, first_met_at=datetime(2026, 4, 1, tzinfo=timezone.utc))
        person = self.store.current_person()
        self.assertIsNone(person.display_name)
        self.assertEqual(person.first_met_at, datetime(2026, 4, 1, tzinfo=timezone.utc))

    def test_display_name_must_be_trimmed_non_blank_and_short(self):
        self.add(display_name="x" * 100)
        for name in ("", "   ", " Nick", "Nick ", "x" * 101):
            with self.subTest(name=name), self.assertRaises(IntegrityError):
                self.add(display_name=name)

    def test_naming_with_nobody_met_creates_the_person(self):
        self.store.set_current_name("Sam")
        person = self.store.current_person()
        self.assertEqual(person.display_name, "Sam")
        self.assertIsNotNone(person.first_met_at)

    def test_naming_renames_only_the_current_person(self):
        self.add(display_name="Later", first_met_at=datetime(2026, 5, 1, tzinfo=timezone.utc))
        self.add(display_name=None, first_met_at=datetime(2026, 4, 1, tzinfo=timezone.utc))
        self.store.set_current_name("Sam")
        with Session(self.engine) as session:
            names = session.scalars(select(PersonRecord.display_name).order_by(PersonRecord.first_met_at)).all()
        self.assertEqual(names, ["Sam", "Later"])

    def test_invalid_name_is_rejected_and_keeps_the_old_one(self):
        self.add(display_name="Nick")
        with self.assertRaises(IntegrityError):
            self.store.set_current_name(" Sam")
        self.assertEqual(self.store.current_person().display_name, "Nick")


if __name__ == "__main__":
    unittest.main()
