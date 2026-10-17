from datetime import datetime, timedelta, timezone
import os
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

import psycopg
from psycopg import sql
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from migrate import apply_migrations, database_settings
from storage.database import build_engine
from storage.models import (
    ConversationRecord, MessageRecord, PersonRecord, RelationshipEventRecord, RelationshipRecord,
)
from subconscious.relationship.appraisal import Appraisal
from subconscious.relationship.appraisal_store import AppraisalStore
from subconscious.relationship.relationship_context import describe_relationship
from subconscious.relationship.relationship_rules import (
    BASELINE, DRIFT_HALF_LIFE, RelationshipValues, apply_appraisal, drift,
)
from subconscious.relationship.relationship_store import RelationshipStore, update_relationships

WARM = Appraisal("development", "warm", "none", False, "none", "Building Cora together")
HOSTILE = Appraisal("everyday", "hostile", "at_cora", False, "none", "Insults aimed at Cora")


def at(day: int) -> datetime:
    return datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(days=day)


def values(row) -> RelationshipValues:
    return RelationshipValues(row.trust, row.warmth, row.familiarity)


class UpdateRelationshipsTests(unittest.TestCase):
    def test_logs_latest_values_once_per_pass(self):
        write_activity = MagicMock()
        events = [MagicMock(trust=0.6, warmth=0.5, familiarity=0.02),
                  MagicMock(trust=0.612, warmth=0.517, familiarity=0.04)]
        with patch("subconscious.relationship.relationship_store.RelationshipStore") as store:
            store.return_value.apply_waiting_appraisals.return_value = events
            update_relationships("engine", write_activity=write_activity)
        store.assert_called_once_with("engine")
        write_activity.assert_called_once_with(
            "Relationship updated from 2 conversation(s): trust=0.61, warmth=0.52, familiarity=0.04",
            "Relationship updated",
        )

    def test_nothing_waiting_logs_nothing_and_failure_logs_only_type(self):
        write_activity = MagicMock()
        with patch("subconscious.relationship.relationship_store.RelationshipStore") as store:
            store.return_value.apply_waiting_appraisals.return_value = []
            update_relationships("engine", write_activity=write_activity)
            write_activity.assert_not_called()
            store.return_value.apply_waiting_appraisals.side_effect = RuntimeError("private details")
            update_relationships("engine", write_activity=write_activity)
        write_activity.assert_called_once_with(
            "Relationship update stopped (RuntimeError).", "Relationship update failed",
        )


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class RelationshipStoreDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        settings = database_settings()
        admin = psycopg.connect(**settings, autocommit=True)
        cls.addClassCleanup(admin.close)
        cls.database = "cora_test_relationships_" + uuid4().hex
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
                "TRUNCATE public.relationship_events, public.relationships, public.interaction_appraisals, "
                "public.messages, public.conversations, public.people CASCADE"
            ))
        self.appraisals = AppraisalStore(self.engine)
        self.store = RelationshipStore(self.engine)

    def appraised(self, day, appraisal):
        with Session(self.engine) as session, session.begin():
            conversation = ConversationRecord(started_at=at(day), status="closed")
            session.add(conversation)
            session.flush()
            session.add(MessageRecord(conversation_id=conversation.id, sequence_number=1,
                                      role="user", text="Hello", channel="keyboard"))
            conversation_id = conversation.id
        self.appraisals.save(conversation_id, appraisal)
        return conversation_id

    def relationship(self):
        with Session(self.engine) as session:
            return session.scalars(select(RelationshipRecord)).one()

    def test_applies_each_appraisal_once_in_conversation_order_with_drift_between(self):
        later = self.appraised(31, HOSTILE)
        first = self.appraised(1, WARM)
        events = self.store.apply_waiting_appraisals()
        self.assertEqual([event.conversation_id for event in events], [first, later])
        after_first = apply_appraisal(BASELINE, WARM)
        expected = apply_appraisal(drift(after_first, timedelta(days=30)), HOSTILE)
        self.assertEqual([values(event) for event in events], [after_first, expected])
        relationship = self.relationship()
        self.assertEqual(values(relationship), expected)
        self.assertEqual(relationship.updated_at, at(31))
        self.assertEqual(relationship.person_id, events[0].person_id)
        self.assertEqual({event.kind for event in events}, {"appraisal"})
        self.assertEqual(self.store.apply_waiting_appraisals(), [])
        with Session(self.engine) as session:
            self.assertEqual(len(session.scalars(select(RelationshipEventRecord)).all()), 2)

    def test_a_late_appraisal_of_an_older_conversation_never_turns_the_clock_back(self):
        self.appraised(14, WARM)
        self.store.apply_waiting_appraisals()
        before = values(self.relationship())
        self.appraised(12, HOSTILE)
        [event] = self.store.apply_waiting_appraisals()
        self.assertEqual(values(event), apply_appraisal(before, HOSTILE))
        self.assertEqual(self.relationship().updated_at, at(14))

    def test_startup_context_describes_the_current_person_after_drift_to_now(self):
        self.assertEqual(self.store.get_startup_context(at(5)), "")
        self.appraised(1, HOSTILE)
        self.assertEqual(self.store.get_startup_context(at(5)), "")
        self.assertIsNone(self.store.current_values(at(5)))
        self.store.apply_waiting_appraisals()
        hurt = values(self.relationship())
        self.assertEqual(self.store.current_values(at(1)), hurt)
        self.assertEqual(self.store.get_startup_context(at(1)), describe_relationship(hurt))
        self.assertIn("cooler toward them", self.store.get_startup_context(at(1)))
        # Months later the hurt has eased back toward baseline; familiarity stays.
        later = at(1) + DRIFT_HALF_LIFE * 6
        self.assertEqual(self.store.get_startup_context(later), describe_relationship(drift(hurt, later - at(1))))
        self.assertIn("friendly toward them", self.store.get_startup_context(later))
        # A person met later never borrows the first person's relationship.
        with Session(self.engine) as session, session.begin():
            session.add(PersonRecord(first_met_at=at(0)))
        self.assertEqual(self.store.get_startup_context(later), "")


if __name__ == "__main__":
    unittest.main()
