from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
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
from subconscious.relationship import relationship_store

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


def fake_event(kind, trust, warmth, reason=None):
    return RelationshipEventRecord(
        id=uuid4(), person_id=uuid4(), kind=kind, conversation_id=None if reason else uuid4(),
        trust=trust, warmth=warmth, familiarity=0.3, reason=reason, created_at=at(2),
    )


class RelationshipCliTests(unittest.TestCase):
    def run_cli(self, store, *args, answer=""):
        output = io.StringIO()
        with patch("sys.argv", ["relationship_store", *args]), \
                patch.object(relationship_store, "load_dotenv"), \
                patch.object(relationship_store, "build_engine"), patch("sys.stderr", io.StringIO()), \
                patch.object(relationship_store, "RelationshipStore", return_value=store), \
                patch("builtins.input", return_value=answer) as prompt, redirect_stdout(output):
            relationship_store.main()
        self.prompted = prompt.called
        return output.getvalue()

    def test_show_values_and_their_wording(self):
        store = MagicMock()
        store.current_values.return_value = None
        self.assertEqual(self.run_cli(store), "No relationship yet.\n")
        store.current_values.return_value = RelationshipValues(0.72, 0.79, 0.68)
        self.assertEqual(self.run_cli(store, "show").splitlines(), [
            "trust=0.72 warmth=0.79 familiarity=0.68", describe_relationship(RelationshipValues(0.72, 0.79, 0.68)),
        ])
        self.assertIsNotNone(store.current_values.call_args.args[0].tzinfo)

    def test_history_lists_or_exports_every_snapshot(self):
        appraisal, reset = fake_event("appraisal", 0.6, 0.4), fake_event("reset", 0.5, 0.5, "Fresh start")
        store = MagicMock()
        store.history.return_value = []
        self.assertEqual(self.run_cli(store, "history"), "No relationship events yet.\n")
        store.history.return_value = [appraisal, reset]
        lines = self.run_cli(store, "history").splitlines()
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith(str(appraisal.id)))
        self.assertIn("appraisal trust=0.60 warmth=0.40 familiarity=0.30", lines[0])
        self.assertEqual(lines[2], "     Fresh start")
        exported = json.loads(self.run_cli(store, "history", "--json"))
        self.assertEqual(exported[1], {
            "id": str(reset.id), "kind": "reset", "created_at": at(2).isoformat(), "conversation_id": None,
            "reason": "Fresh start", "trust": 0.5, "warmth": 0.5, "familiarity": 0.3,
        })
        self.assertEqual(exported[0]["conversation_id"], str(appraisal.conversation_id))

    def test_reset_previews_then_needs_typed_confirmation(self):
        store = MagicMock()
        store.current_values.return_value = RelationshipValues(0.3, 0.2, 0.6)
        output = self.run_cli(store, "reset", "--reason", " A bad test ", "--dry-run")
        self.assertEqual(output.splitlines(), [
            "Now:   trust=0.30 warmth=0.20 familiarity=0.60",
            "After: trust=0.50 warmth=0.50 familiarity=0.60",
            "Reason: A bad test", "Dry run: nothing saved.",
        ])
        self.assertFalse(self.prompted)
        self.assertIn("Cancelled: nothing saved.", self.run_cli(store, "reset", "--reason", "x", answer="yes"))
        store.reset.assert_not_called()
        store.reset.return_value = fake_event("reset", 0.5, 0.5, "x")
        output = self.run_cli(store, "reset", "--reason", "x", answer=" reset ")
        self.assertIn(f"Reset saved as event {store.reset.return_value.id}.", output)
        self.assertIn("until she next connects", output)
        self.assertEqual(store.reset.call_args.kwargs["reason"], "x")
        self.assertIsNone(store.reset.call_args.kwargs["to_event_id"])

    def test_reset_to_an_event_and_refusals(self):
        good = fake_event("appraisal", 0.8, 0.7)
        store = MagicMock()
        store.current_values.return_value = None  # nothing applied yet reads as baseline
        store.history.return_value = [good]
        store.reset.return_value = fake_event("reset", 0.8, 0.7, "Back")
        output = self.run_cli(store, "reset", "--reason", "Back", "--to", str(good.id), answer="reset")
        self.assertIn("Now:   trust=0.50 warmth=0.50 familiarity=0.00", output)
        self.assertIn("After: trust=0.80 warmth=0.70 familiarity=0.00", output)
        self.assertEqual(store.reset.call_args.kwargs["to_event_id"], good.id)
        for args, message in (
            (("--reason", "Back", "--to", str(uuid4())), "No such relationship event for the current person."),
            (("--reason", "   "), "Reset reason must be 1 to 1000 characters."),
        ):
            store = MagicMock()
            store.history.return_value = [good]
            with self.subTest(args=args), self.assertRaises(SystemExit) as exit:
                self.run_cli(store, "reset", *args, answer="reset")
            self.assertEqual(exit.exception.code, message)
            store.reset.assert_not_called()
        for args in (("reset",), ("reset", "--reason", "x", "--to", "not-an-id")):
            with self.subTest(args=args), self.assertRaises(SystemExit):
                self.run_cli(MagicMock(), *args)

    def test_database_failure_hides_details(self):
        store = MagicMock()
        store.current_values.side_effect = RuntimeError("private SQL")
        with self.assertRaises(SystemExit) as exit:
            self.run_cli(store, "show")
        self.assertEqual(exit.exception.code,
                         "Relationship not loaded (RuntimeError). Check the database and migrations.")


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

    def test_reset_restores_baseline_keeps_familiarity_and_history_and_skips_older_appraisals(self):
        self.appraised(1, WARM)
        self.appraised(2, HOSTILE)
        self.store.apply_waiting_appraisals()
        hurt = values(self.relationship())
        unapplied = self.appraised(3, HOSTILE)  # the last bad conversation, not yet applied
        event = self.store.reset(reason="  Clearing a test that went wrong  ", now=at(4))
        self.assertEqual((event.kind, event.reason, event.created_at), ("reset", "Clearing a test that went wrong", at(4)))
        restored = RelationshipValues(BASELINE.trust, BASELINE.warmth, hurt.familiarity)
        self.assertEqual(values(event), restored)
        self.assertEqual(values(self.relationship()), restored)
        self.assertEqual(self.relationship().updated_at, at(4))
        self.assertEqual(self.store.apply_waiting_appraisals(), [])  # day 3 started before the reset
        after = self.appraised(5, WARM)
        [applied] = self.store.apply_waiting_appraisals()
        self.assertEqual(applied.conversation_id, after)
        self.assertEqual(values(applied), apply_appraisal(drift(restored, at(5) - at(4)), WARM))
        with Session(self.engine) as session:
            # Appraisal events are stamped by the database clock, the reset by the test's `now`.
            kinds = session.scalars(select(RelationshipEventRecord.kind)).all()
            self.assertEqual(sorted(kinds), ["appraisal", "appraisal", "appraisal", "reset"])
            self.assertIsNone(session.scalar(select(RelationshipEventRecord)
                                             .where(RelationshipEventRecord.conversation_id == unapplied)))

    def test_reset_to_an_earlier_event_and_its_refusals(self):
        with self.assertRaises(LookupError):
            self.store.reset(reason="No one yet", now=at(1))
        self.appraised(1, WARM)
        [good] = self.store.apply_waiting_appraisals()
        self.appraised(2, HOSTILE)
        self.store.apply_waiting_appraisals()
        familiarity = self.relationship().familiarity
        for reason in ("   ", "x" * 1001):
            with self.subTest(reason=len(reason)), self.assertRaises(ValueError):
                self.store.reset(reason=reason, now=at(3))
        with self.assertRaises(LookupError):
            self.store.reset(reason="Unknown snapshot", now=at(3), to_event_id=uuid4())
        event = self.store.reset(reason="Back to before the argument", now=at(3), to_event_id=good.id)
        self.assertEqual(values(event), RelationshipValues(good.trust, good.warmth, familiarity))
        self.assertEqual(sorted(item.kind for item in self.store.history()), ["appraisal", "appraisal", "reset"])
        with Session(self.engine) as session:
            self.assertEqual(len(session.scalars(select(RelationshipEventRecord)).all()), 3)

    def test_reset_before_any_update_starts_the_relationship_at_baseline(self):
        with Session(self.engine) as session, session.begin():
            session.add(PersonRecord(first_met_at=at(0)))
        event = self.store.reset(reason="Fresh start", now=at(1))
        self.assertEqual(values(event), BASELINE)
        self.assertEqual(self.relationship().updated_at, at(1))


if __name__ == "__main__":
    unittest.main()
