from datetime import datetime, timezone
import os
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from sqlalchemy import CheckConstraint, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, configure_mappers

from storage.database import build_engine
from migrate import apply_migrations, database_settings
from storage.models import (
    Base, ConversationRecord, ConversationSummaryRecord, IdentityEventEvidenceRecord, IdentityEventRecord,
    IdentityEventTraitRecord, IdentityTraitRecord, IdentityWorkerStateRecord, InterestEventMemoryRecord,
    InterestEventRecord, InterestExplorationRecord, InterestRecord, InterestWorkerStateRecord,
    MemoryRecord, MessageRecord,
)


class ModelDefinitionTests(unittest.TestCase):
    def test_mappings_only_describe_the_tables_without_connecting(self):
        with patch("psycopg.connect") as connect:
            configure_mappers()
            conversation = ConversationRecord()
            conversation.messages.append(MessageRecord(
                sequence_number=1, role="user", text="Hello", channel="keyboard"
            ))
            self.assertIs(conversation.messages[0].conversation, conversation)
            summary = ConversationSummaryRecord(conversation_id=uuid4(), text="A brief summary")
            self.assertEqual(summary.text, "A brief summary")
            self.assertEqual(set(Base.metadata.tables), {
                "public.conversations", "public.messages", "public.conversation_summaries",
                "public.memories", "public.interests", "public.interest_events",
                "public.interest_event_memories", "public.interest_worker_state",
                "public.interest_explorations", "public.people", "public.identity_traits",
                "public.identity_events", "public.identity_event_traits", "public.identity_event_evidence",
                "public.identity_worker_state",
            })
            connect.assert_not_called()


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class ModelDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        settings = database_settings()
        admin = psycopg.connect(**settings, autocommit=True)
        cls.addClassCleanup(admin.close)
        cls.database = "cora_test_models_" + uuid4().hex
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.database)))
        cls.addClassCleanup(
            admin.execute, sql.SQL("DROP DATABASE {}").format(sql.Identifier(cls.database))
        )
        cls.settings = {**settings, "dbname": cls.database}
        with psycopg.connect(**cls.settings, autocommit=True) as connection:
            apply_migrations(connection)
        with patch("storage.database.database_settings", return_value=cls.settings):
            cls.engine = build_engine()
        cls.addClassCleanup(cls.engine.dispose)

    def setUp(self):
        # Only this disposable test database is cleared, never Cora's database.
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM public.conversation_summaries"))
            connection.execute(text("DELETE FROM public.messages"))
            connection.execute(text("DELETE FROM public.conversations"))
        with Session(self.engine) as session, session.begin():
            conversation = ConversationRecord()
            session.add(conversation)
            session.flush()
            self.conversation_id = conversation.id

    def message(self, **changes):
        values = {
            "conversation_id": self.conversation_id, "sequence_number": 1,
            "role": "user", "text": "Test message", "channel": "keyboard",
        }
        return MessageRecord(**{**values, **changes})

    def test_models_match_migrated_columns_and_constraints(self):
        inspector = inspect(self.engine)
        for table in Base.metadata.sorted_tables:
            columns = {column["name"]: column for column in inspector.get_columns(table.name, schema="public")}
            self.assertEqual(set(columns), set(table.columns.keys()))
            for column in table.columns:
                reflected = columns[column.name]
                self.assertEqual(reflected["nullable"], column.nullable)
                self.assertEqual(
                    str(reflected["type"].compile(dialect=self.engine.dialect)),
                    str(column.type.compile(dialect=self.engine.dialect)),
                )
                expected_default = str(column.server_default.arg) if column.server_default is not None else None
                actual_default = reflected["default"]
                if expected_default in ("active", "incomplete", "not_applicable"):
                    self.assertEqual(actual_default, f"'{expected_default}'::text")
                else:
                    self.assertEqual(actual_default, expected_default)
            checks = {row["name"] for row in inspector.get_check_constraints(table.name, schema="public")}
            self.assertEqual(checks, {constraint.name for constraint in table.constraints if isinstance(constraint, CheckConstraint)})
        unique = inspector.get_unique_constraints("messages", schema="public")
        self.assertEqual(unique[0]["column_names"], ["conversation_id", "sequence_number"])
        foreign_key = inspector.get_foreign_keys("messages", schema="public")[0]
        self.assertEqual(foreign_key["referred_table"], "conversations")
        self.assertEqual(foreign_key["options"]["ondelete"], "RESTRICT")
        self.assertEqual(inspector.get_pk_constraint("conversation_summaries", schema="public")["constrained_columns"],
                 ["conversation_id"])
        summary_key = inspector.get_foreign_keys("conversation_summaries", schema="public")[0]
        self.assertEqual(summary_key["referred_table"], "conversations")
        self.assertEqual(summary_key["options"]["ondelete"], "RESTRICT")

    def test_voice_keyboard_and_screen_share_ordered_history_after_commit(self):
        with Session(self.engine) as session, session.begin():
            session.add_all([
                self.message(sequence_number=3, role="assistant", channel="screen", status="completed"),
                self.message(sequence_number=1, channel="voice", status="completed", delivery_status="sent"),
                self.message(sequence_number=2, channel="keyboard", status="completed", delivery_status="sent"),
            ])
        with Session(self.engine) as session:
            conversation = session.get(ConversationRecord, self.conversation_id)
            self.assertIsNotNone(conversation)
            self.assertEqual([message.sequence_number for message in conversation.messages], [1, 2, 3])
            self.assertEqual([message.channel for message in conversation.messages], ["voice", "keyboard", "screen"])
            self.assertEqual(conversation.status, "active")
            self.assertIsNone(conversation.ended_at)
            self.assertIsNotNone(conversation.started_at.tzinfo)
            for message in conversation.messages:
                self.assertIsInstance(message.id, UUID)
                self.assertIsNotNone(message.created_at.tzinfo)

    def test_defaults_do_not_mark_partial_responses_completed(self):
        with Session(self.engine) as session, session.begin():
            message = self.message(role="assistant", channel="voice", text="Partial answer")
            session.add(message)
            session.flush()
            self.assertEqual(message.status, "incomplete")
            self.assertEqual(message.delivery_status, "not_applicable")

    def test_invalid_messages_are_rejected_by_database(self):
        cases = [
            ({"conversation_id": uuid4()}, "messages_conversation_id_fkey"),
            ({"sequence_number": 0}, "messages_sequence_check"),
            ({"role": "system"}, "messages_role_check"),
            ({"channel": "email"}, "messages_channel_check"),
            ({"status": "unknown"}, "messages_status_check"),
            ({"delivery_status": "unknown"}, "messages_delivery_status_check"),
            ({"text": " "}, "messages_text_check"),
        ]
        for changes, constraint in cases:
            with self.subTest(changes=changes), Session(self.engine) as session:
                with self.assertRaises(IntegrityError) as error:
                    with session.begin():
                        session.add(self.message(**changes))
                self.assertEqual(error.exception.orig.diag.constraint_name, constraint)

    def test_duplicate_ordering_and_ids_are_rejected_without_overwriting(self):
        message_id = uuid4()
        with Session(self.engine) as session, session.begin():
            session.add(self.message(id=message_id))
        for changes in ({"sequence_number": 1}, {"id": message_id, "sequence_number": 2}):
            with self.subTest(changes=changes), Session(self.engine) as session:
                with self.assertRaises(IntegrityError):
                    with session.begin():
                        session.add(self.message(**changes))
        with Session(self.engine) as session:
            self.assertEqual(session.get(MessageRecord, message_id).text, "Test message")

    def test_conversation_status_times_and_deletion_are_guarded(self):
        with self.engine.connect() as connection:
            for statement in (
                "UPDATE public.conversations SET status = 'unknown'",
                "UPDATE public.conversations SET ended_at = started_at - INTERVAL '1 second'",
            ):
                with self.assertRaises(IntegrityError):
                    connection.execute(text(statement))
                connection.rollback()
        with Session(self.engine) as session, session.begin():
            session.add(self.message())
        with Session(self.engine) as session:
            conversation = session.get(ConversationRecord, self.conversation_id)
            self.assertEqual(len(conversation.messages), 1)
            session.delete(conversation)
            with self.assertRaises(IntegrityError):
                session.commit()

    def test_migration_rerun_preserves_saved_rows(self):
        with Session(self.engine) as session, session.begin():
            session.add(self.message())
        with psycopg.connect(**self.settings, autocommit=True) as connection:
            self.assertEqual(apply_migrations(connection), [])
            self.assertEqual(connection.execute("SELECT count(*) FROM public.messages").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM public.schema_migrations").fetchone()[0], 12)

    def interest(self, **changes):
        values = {
            "title": "Older mechanical designs", "description": "Why old machines were built as they were",
            "why_emerged": "Nick is restoring a car", "strength": 0.2,
            "strength_at": datetime(2026, 10, 1, tzinfo=timezone.utc), "status": "emerging",
        }
        return InterestRecord(**{**values, **changes})

    def test_interest_rows_lineage_events_and_memory_links(self):
        self.addCleanup(self.delete_interests)
        with Session(self.engine) as session, session.begin():
            parent = self.interest()
            session.add(parent)
            session.flush()
            child = self.interest(title="Industrial design", parent_interest_id=parent.id)
            session.add(child)
            session.flush()
            event = InterestEventRecord(
                interest_id=child.id, kind="spawn", strength_after=0.2, reason="Followed a thread",
            )
            session.add(event)
            session.flush()
            self.assertIsNotNone(child.created_at.tzinfo)
            self.assertIsNone(child.last_reinforced_at)
            self.assertIsNotNone(event.created_at.tzinfo)
            self.assertIsNone(event.strength_before)
            message = self.message(text="I have a wedding on 20 November.")
            session.add(message)
            session.flush()
            memory = MemoryRecord(
                source_message_id=message.id, kind="fact", text="Has a wedding on 20 November",
                evidence=message.text,
            )
            session.add(memory)
            session.flush()
            session.add(InterestEventMemoryRecord(event_id=event.id, memory_id=memory.id))
            event_id, memory_id = event.id, memory.id
        # Forgetting a memory removes its link but keeps the event history.
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM public.memories WHERE id = :id"), {"id": memory_id})
        with Session(self.engine) as session:
            self.assertIsNotNone(session.get(InterestEventRecord, event_id))
            self.assertIsNone(session.get(InterestEventMemoryRecord, (event_id, memory_id)))

    def test_invalid_interests_are_rejected_by_database(self):
        cases = [
            ({"title": " "}, "interests_title_check"),
            ({"title": "x" * 201}, "interests_title_check"),
            ({"description": ""}, "interests_description_check"),
            ({"why_emerged": " "}, "interests_why_emerged_check"),
            ({"strength": -0.01}, "interests_strength_check"),
            ({"strength": 1.01}, "interests_strength_check"),
            ({"strength": float("nan")}, "interests_strength_check"),
            ({"status": "bored"}, "interests_status_check"),
            ({"parent_interest_id": uuid4()}, "interests_parent_interest_id_fkey"),
            ({"id": UUID(int=5), "parent_interest_id": UUID(int=5)}, "interests_parent_check"),
        ]
        for changes, constraint in cases:
            with self.subTest(changes=changes), Session(self.engine) as session:
                with self.assertRaises(IntegrityError) as error:
                    with session.begin():
                        session.add(self.interest(**changes))
                self.assertEqual(error.exception.orig.diag.constraint_name, constraint)

    def test_invalid_interest_events_and_worker_state_are_rejected(self):
        with Session(self.engine) as session, session.begin():
            interest = self.interest(title="Event checks")
            session.add(interest)
            session.flush()
            interest_id = interest.id
        self.addCleanup(self.delete_interests)
        cases = [
            ({"kind": "explore", "strength_before": 0.1}, "interest_events_kind_check"),
            ({"kind": "reinforce", "strength_before": 0.1, "strength_after": 1.5}, "interest_events_strength_check"),
            ({"kind": "reinforce", "strength_before": None}, "interest_events_before_check"),
            ({"kind": "spawn", "strength_before": 0.1}, "interest_events_before_check"),
            ({"kind": "reinforce", "strength_before": -0.1}, "interest_events_before_check"),
            ({"kind": "reinforce", "strength_before": 0.1, "reason": " "}, "interest_events_reason_check"),
            ({"kind": "reinforce", "strength_before": 0.1, "interest_id": uuid4()}, "interest_events_interest_id_fkey"),
        ]
        for changes, constraint in cases:
            values = {"interest_id": interest_id, "strength_after": 0.3, "reason": "Because", **changes}
            with self.subTest(changes=changes), Session(self.engine) as session:
                with self.assertRaises(IntegrityError) as error:
                    with session.begin():
                        session.add(InterestEventRecord(**values))
                self.assertEqual(error.exception.orig.diag.constraint_name, constraint)
        for changes, constraint in (
            ({"id": 2}, "interest_worker_state_singleton_check"),
            ({"memory_cursor_id": uuid4()}, "interest_worker_state_cursor_check"),
            ({"memory_cursor_created_at": datetime(2026, 10, 1, tzinfo=timezone.utc)},
             "interest_worker_state_cursor_check"),
        ):
            with self.subTest(changes=changes), Session(self.engine) as session:
                with self.assertRaises(IntegrityError) as error:
                    with session.begin():
                        session.add(InterestWorkerStateRecord(**changes))
                self.assertEqual(error.exception.orig.diag.constraint_name, constraint)

    def test_invalid_explorations_are_rejected_by_database(self):
        with Session(self.engine) as session, session.begin():
            interest = self.interest(title="Exploration checks")
            session.add(interest)
            session.flush()
            interest_id = interest.id
        self.addCleanup(self.delete_interests)
        start = datetime(2026, 10, 15, tzinfo=timezone.utc)
        found = {"status": "found", "finished_at": start, "finding": "Something new"}
        cases = [
            ({"status": "paused", "finished_at": start}, "interest_explorations_status_check"),
            ({"finished_at": start}, "interest_explorations_finished_check"),
            ({"status": "failed"}, "interest_explorations_finished_check"),
            ({**found, "finished_at": start.replace(day=14)}, "interest_explorations_finished_check"),
            ({"finding": "Too early"}, "interest_explorations_finding_check"),
            ({**found, "finding": None}, "interest_explorations_finding_check"),
            ({**found, "finding": " "}, "interest_explorations_finding_check"),
            ({**found, "finding": "x" * 2001}, "interest_explorations_finding_check"),
            ({**found, "next_questions": ["a", "b", "c", "d"]}, "interest_explorations_next_questions_check"),
            ({"next_questions": ["Not yet"]}, "interest_explorations_next_questions_check"),
            ({**found, "sources": ["https://example.com"] * 11}, "interest_explorations_sources_check"),
            ({"status": "failed", "finished_at": start, "sources": ["https://example.com"]},
             "interest_explorations_sources_check"),
            ({"interest_id": uuid4()}, "interest_explorations_interest_id_fkey"),
            ({"mentioned_at": start}, "interest_explorations_mentioned_check"),
            ({"status": "failed", "finished_at": start, "mentioned_at": start},
             "interest_explorations_mentioned_check"),
            ({"reaction": "Too early"}, "interest_explorations_reaction_check"),
            ({"status": "failed", "finished_at": start, "reaction": "Dull"}, "interest_explorations_reaction_check"),
            ({**found, "reaction": " "}, "interest_explorations_reaction_check"),
            ({**found, "reaction": "x" * 501}, "interest_explorations_reaction_check"),
        ]
        for changes, constraint in cases:
            values = {"interest_id": interest_id, "status": "running", "started_at": start, **changes}
            with self.subTest(changes=changes), Session(self.engine) as session:
                with self.assertRaises(IntegrityError) as error:
                    with session.begin():
                        session.add(InterestExplorationRecord(**values))
                self.assertEqual(error.exception.orig.diag.constraint_name, constraint)
        with Session(self.engine) as session, session.begin():
            session.add(InterestExplorationRecord(interest_id=interest_id, status="running", started_at=start))
        with self.engine.connect() as connection, self.assertRaises(IntegrityError):
            connection.execute(text("DELETE FROM public.interests WHERE id = :id"), {"id": interest_id})

    def test_worker_state_is_a_single_row_with_cursor_pair(self):
        self.addCleanup(self.delete_worker_state)
        cursor = datetime(2026, 10, 1, tzinfo=timezone.utc)
        with Session(self.engine) as session, session.begin():
            state = InterestWorkerStateRecord(memory_cursor_created_at=cursor, memory_cursor_id=uuid4())
            session.add(state)
            session.flush()
            self.assertEqual(state.id, 1)
        with Session(self.engine) as session:
            with self.assertRaises(IntegrityError):
                with session.begin():
                    session.add(InterestWorkerStateRecord())

    def delete_interests(self):
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM public.interest_explorations"))
            connection.execute(text("DELETE FROM public.interest_event_memories"))
            connection.execute(text("DELETE FROM public.interest_events"))
            # RESTRICT is checked per row, so remove child interests before parents.
            connection.execute(text("DELETE FROM public.interests WHERE parent_interest_id IS NOT NULL"))
            connection.execute(text("DELETE FROM public.interests"))

    def delete_worker_state(self):
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM public.interest_worker_state"))

    def trait(self, **changes):
        values = {"kind": "perspective", "statement": "Old machines show their reasoning.", "confidence": 0.6}
        return IdentityTraitRecord(**{**values, **changes})

    def delete_identity(self):
        with self.engine.begin() as connection:
            for table in ("identity_event_evidence", "identity_event_traits", "identity_events",
                          "identity_traits", "identity_worker_state"):
                connection.execute(text(f"DELETE FROM public.{table}"))

    def test_identity_traits_events_cited_traits_and_evidence(self):
        self.addCleanup(self.delete_interests)
        self.addCleanup(self.delete_identity)  # runs first: evidence references interests
        start = datetime(2026, 10, 15, tzinfo=timezone.utc)
        with Session(self.engine) as session, session.begin():
            interest = self.interest()
            session.add(interest)
            session.flush()
            interest_event = InterestEventRecord(
                interest_id=interest.id, kind="spawn", strength_after=0.2, reason="Followed a thread")
            found = InterestExplorationRecord(
                interest_id=interest.id, status="found", started_at=start, finished_at=start, finding="New")
            summary = ConversationSummaryRecord(conversation_id=self.conversation_id, text="We talked engines")
            liked = self.trait(kind="preference", statement="I like engines.", polarity="like", subject="Engines")
            seen = self.trait()
            value = self.trait(kind="value", statement="I value clarity.")
            session.add_all([interest_event, found, summary, liked, seen, value])
            session.flush()
            self.assertEqual((value.status, value.retired_at), ("active", None))
            self.assertIsNotNone(value.created_at.tzinfo)
            event = IdentityEventRecord(
                trait_id=value.id, kind="form", statement=value.statement, confidence_after=0.6,
                reason="Both traits point the same way")
            session.add(event)
            session.flush()
            session.add_all([
                IdentityEventTraitRecord(event_id=event.id, trait_id=liked.id),
                IdentityEventTraitRecord(event_id=event.id, trait_id=seen.id),
                IdentityEventEvidenceRecord(event_id=event.id, interest_event_id=interest_event.id),
                IdentityEventEvidenceRecord(event_id=event.id, exploration_id=found.id),
                IdentityEventEvidenceRecord(event_id=event.id, conversation_id=self.conversation_id),
            ])
            event_id, trait_id = event.id, value.id
        # History is kept: cited experience and traits cannot be deleted from under it.
        for statement in ("DELETE FROM public.identity_traits WHERE id = :id",
                          "DELETE FROM public.interest_explorations WHERE status = 'found'",
                          "DELETE FROM public.conversation_summaries"):
            with self.subTest(statement=statement), self.engine.connect() as connection:
                with self.assertRaises(IntegrityError):
                    connection.execute(text(statement), {"id": trait_id})
        with Session(self.engine) as session:
            self.assertEqual(len(session.scalars(select(IdentityEventEvidenceRecord).where(
                IdentityEventEvidenceRecord.event_id == event_id)).all()), 3)

    def test_invalid_identity_rows_are_rejected_by_database(self):
        self.addCleanup(self.delete_identity)
        start = datetime(2026, 10, 15, tzinfo=timezone.utc)
        for changes, constraint in (
            ({"kind": "belief"}, "identity_traits_kind_check"),
            ({"statement": " "}, "identity_traits_statement_check"),
            ({"statement": "x" * 301}, "identity_traits_statement_check"),
            ({"kind": "preference"}, "identity_traits_preference_check"),
            ({"kind": "preference", "polarity": "like"}, "identity_traits_preference_check"),
            ({"kind": "preference", "polarity": "love", "subject": "Engines"}, "identity_traits_preference_check"),
            ({"kind": "preference", "polarity": "like", "subject": " "}, "identity_traits_preference_check"),
            ({"kind": "preference", "polarity": "like", "subject": "x" * 201},
             "identity_traits_preference_check"),
            ({"polarity": "like"}, "identity_traits_preference_check"),
            ({"subject": "Engines"}, "identity_traits_preference_check"),
            ({"confidence": 1.01}, "identity_traits_confidence_check"),
            ({"confidence": float("nan")}, "identity_traits_confidence_check"),
            ({"status": "held"}, "identity_traits_status_check"),
            ({"status": "retired"}, "identity_traits_retired_check"),
            ({"retired_at": start}, "identity_traits_retired_check"),
            ({"created_at": start, "status": "retired", "retired_at": start.replace(day=14)},
             "identity_traits_retired_check"),
        ):
            with self.subTest(changes=changes), Session(self.engine) as session:
                with self.assertRaises(IntegrityError) as error:
                    with session.begin():
                        session.add(self.trait(**changes))
                self.assertEqual(error.exception.orig.diag.constraint_name, constraint)
        with Session(self.engine) as session, session.begin():
            trait = self.trait()
            session.add(trait)
            session.flush()
            event = IdentityEventRecord(
                trait_id=trait.id, kind="form", statement=trait.statement, confidence_after=0.6, reason="Because")
            session.add(event)
            session.flush()
            trait_id, event_id = trait.id, event.id
        revise = {"kind": "revise", "confidence_before": 0.6}
        for changes, constraint in (
            ({"kind": "grow", "confidence_before": 0.6}, "identity_events_kind_check"),
            ({**revise, "statement": ""}, "identity_events_statement_check"),
            ({**revise, "statement": "x" * 301}, "identity_events_statement_check"),
            ({**revise, "confidence_after": -0.1}, "identity_events_confidence_check"),
            ({"kind": "revise"}, "identity_events_before_check"),
            ({"kind": "form", "confidence_before": 0.6}, "identity_events_before_check"),
            ({**revise, "confidence_before": 1.5}, "identity_events_before_check"),
            ({**revise, "reason": " "}, "identity_events_reason_check"),
            ({**revise, "reason": "x" * 1001}, "identity_events_reason_check"),
            ({**revise, "trait_id": uuid4()}, "identity_events_trait_id_fkey"),
        ):
            values = {"trait_id": trait_id, "kind": "form", "statement": "Revised wording.",
                      "confidence_after": 0.7, "reason": "Because", **changes}
            with self.subTest(changes=changes), Session(self.engine) as session:
                with self.assertRaises(IntegrityError) as error:
                    with session.begin():
                        session.add(IdentityEventRecord(**values))
                self.assertEqual(error.exception.orig.diag.constraint_name, constraint)
        for record, constraint in (
            (IdentityEventTraitRecord(event_id=event_id, trait_id=uuid4()), "identity_event_traits_trait_id_fkey"),
            (IdentityEventTraitRecord(event_id=uuid4(), trait_id=trait_id), "identity_event_traits_event_id_fkey"),
            (IdentityEventEvidenceRecord(event_id=event_id), "identity_event_evidence_source_check"),
            (IdentityEventEvidenceRecord(event_id=event_id, exploration_id=uuid4(), conversation_id=uuid4()),
             "identity_event_evidence_source_check"),
            (IdentityEventEvidenceRecord(event_id=event_id, exploration_id=uuid4()),
             "identity_event_evidence_exploration_id_fkey"),
            (IdentityEventEvidenceRecord(event_id=event_id, interest_event_id=uuid4()),
             "identity_event_evidence_interest_event_id_fkey"),
            # Only summarized conversations count as evidence.
            (IdentityEventEvidenceRecord(event_id=event_id, conversation_id=self.conversation_id),
             "identity_event_evidence_conversation_id_fkey"),
            (IdentityWorkerStateRecord(id=2), "identity_worker_state_singleton_check"),
        ):
            with self.subTest(record=record), Session(self.engine) as session:
                with self.assertRaises(IntegrityError) as error:
                    with session.begin():
                        session.add(record)
                self.assertEqual(error.exception.orig.diag.constraint_name, constraint)
        with Session(self.engine) as session, session.begin():
            state = IdentityWorkerStateRecord()
            session.add(state)
            session.flush()
            self.assertEqual((state.id, state.last_reflected_at), (1, None))


if __name__ == "__main__":
    unittest.main()