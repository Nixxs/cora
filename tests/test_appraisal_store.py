from datetime import datetime, timezone
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

import psycopg
from psycopg import sql
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from migrate import apply_migrations, database_settings
from storage.database import build_engine
from storage.models import ConversationRecord, InteractionAppraisalRecord, MessageRecord, PersonRecord
from subconscious.relationship.appraisal import Appraisal
from subconscious.relationship.appraisal_store import AppraisalStore

WARM = Appraisal("everyday", "warm", "none", False, "none", "A friendly chat")


def at(day: int) -> datetime:
    return datetime(2026, 10, day, tzinfo=timezone.utc)


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class AppraisalStoreDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        settings = database_settings()
        admin = psycopg.connect(**settings, autocommit=True)
        cls.addClassCleanup(admin.close)
        cls.database = "cora_test_appraisals_" + uuid4().hex
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
            connection.execute(text(
                "TRUNCATE public.interaction_appraisals, public.messages, public.conversations, public.people CASCADE"
            ))
        self.store = AppraisalStore(self.engine)

    def conversation(self, *, day=12, status="closed", messages=1):
        with Session(self.engine) as session, session.begin():
            conversation = ConversationRecord(started_at=at(day), status=status)
            session.add(conversation)
            session.flush()
            for number in range(1, messages + 1):
                session.add(MessageRecord(conversation_id=conversation.id, sequence_number=number,
                                          role="user", text="Hello", channel="keyboard"))
            return conversation.id

    def person(self, day, name=None):
        with Session(self.engine) as session, session.begin():
            person = PersonRecord(display_name=name, first_met_at=at(day))
            session.add(person)
            session.flush()
            return person.id

    def test_lists_only_finished_nonempty_unappraised_conversations_oldest_first(self):
        later = self.conversation(day=14)
        interrupted = self.conversation(day=13, status="interrupted")
        appraised = self.conversation(day=12)
        self.conversation(day=11, status="active")
        self.conversation(day=10, messages=0)
        self.store.save(appraised, WARM)
        self.assertEqual(self.store.unappraised_conversation_ids(), [interrupted, later])

    def test_first_appraisal_meets_an_unnamed_person_when_their_first_conversation_began(self):
        self.conversation(day=11)
        newer = self.conversation(day=13)
        record = self.store.save(newer, WARM)
        with Session(self.engine) as session:
            [person] = session.scalars(select(PersonRecord)).all()
        self.assertIsNone(person.display_name)
        self.assertEqual(person.first_met_at, at(11))
        self.assertEqual((record.person_id, record.tone, record.reason), (person.id, "warm", "A friendly chat"))

    def test_appraisal_belongs_to_the_person_met_first(self):
        self.person(5, "Later")
        first = self.person(1, "Nick")
        self.assertEqual(self.store.save(self.conversation(), WARM).person_id, first)

    def test_an_existing_appraisal_is_kept_not_replaced(self):
        conversation_id = self.conversation()
        self.store.save(conversation_id, WARM)
        kept = self.store.save(conversation_id, Appraisal("development", "curt", "at_situation", False, "none", "Tests"))
        self.assertEqual(kept.tone, "warm")
        with Session(self.engine) as session:
            self.assertEqual(session.scalars(select(InteractionAppraisalRecord.tone)).all(), ["warm"])

    def test_active_or_missing_conversations_are_not_appraised(self):
        with self.assertRaises(ValueError):
            self.store.save(self.conversation(status="active"), WARM)
        with self.assertRaises(LookupError):
            self.store.save(uuid4(), WARM)
        with Session(self.engine) as session:
            self.assertEqual(session.scalars(select(PersonRecord)).all(), [])


if __name__ == "__main__":
    unittest.main()
