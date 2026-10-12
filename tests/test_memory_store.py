"""Validation plus opt-in disposable PostgreSQL tests; never reset personal data."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import os
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

import psycopg
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from migrate import apply_migrations
from storage.conversation_store import ConversationStore
from storage.database import build_engine
from storage.memory_store import MEMORY_KINDS, MemoryCandidate, MemoryStore
from storage.models import MemoryRecord
import test_conversation_summaries as summary_tests


class MemoryStoreValidationTests(unittest.TestCase):
    def test_candidate_is_frozen_and_kinds_are_the_four_supported_categories(self):
        self.assertEqual(MEMORY_KINDS, ("fact", "preference", "decision", "task"))
        candidate = MemoryCandidate(uuid4(), "fact", "Lives in Perth", "I live in Perth")
        with self.assertRaises(FrozenInstanceError):
            candidate.text = "Replacement"

    def test_construction_and_empty_batch_do_not_open_a_session(self):
        engine = MagicMock()
        with patch("storage.memory_store.Session") as session:
            store = MemoryStore(engine)
            self.assertIs(store.engine, engine)
            self.assertEqual(store.save_memories(uuid4(), []), [])
            session.assert_not_called()
        engine.connect.assert_not_called()

    def test_invalid_fields_in_entire_batch_are_checked_before_connecting(self):
        engine = MagicMock()
        store = MemoryStore(engine)
        valid = MemoryCandidate(uuid4(), "fact", "Lives in Perth", "I live in Perth")
        cases = [
            ("unknown", "Valid text", "quote"),
            ("fact", "", "quote"), ("fact", " \n\t", "quote"),
            ("fact", "x" * 501, "quote"),
            ("fact", "Valid text", ""), ("fact", "Valid text", " \n\t"),
        ]
        with patch("storage.memory_store.Session") as session:
            for kind, content, evidence in cases:
                with self.subTest(kind=kind, content=content, evidence=evidence):
                    invalid = MemoryCandidate(uuid4(), kind, content, evidence)
                    with self.assertRaises(ValueError):
                        store.save_memories(uuid4(), [valid, invalid])
            session.assert_not_called()
        engine.connect.assert_not_called()


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class MemoryStoreDatabaseTests(unittest.TestCase):
    # This fixture creates a uniquely named disposable DB and registers cleanup.
    # Import its MODULE, not its TestCase into this module's discovery namespace.
    setUpClass = classmethod(summary_tests.SummaryDatabaseTests.setUpClass.__func__)

    def setUp(self):
        # Memories RESTRICT source deletion: remove these FIRST, only in the
        # disposable DB created above, before reusing the source-row fixture.
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM public.memories"))
        summary_tests.SummaryDatabaseTests.setUp(self)
        self.memories = MemoryStore(self.engine)

    def save_source(self, *, conversation_id=None, role="user", content="I live in Perth. I prefer small steps."):
        return self.store.save_message(
            self.conversation_id if conversation_id is None else conversation_id,
            message_id=uuid4(), role=role, text=content, channel="voice", status="incomplete",
        )

    def candidate(self, source, **changes):
        return MemoryCandidate(**{
            "source_message_id": source.id, "kind": "fact", "text": "Lives in Perth",
            "evidence": "I live in Perth", **changes,
        })

    def test_closed_and_interrupted_are_eligible_but_active_and_missing_are_not(self):
        source = self.save_source()
        candidate = self.candidate(source)
        with self.assertRaisesRegex(ValueError, "closed or interrupted"):
            self.memories.save_memories(self.conversation_id, [candidate])
        unknown = uuid4()
        with self.assertRaises(LookupError):
            self.memories.save_memories(unknown, [candidate])
        self.assertIsNone(self.store.get_conversation(unknown))
        self.assertEqual(self.memories.get_memories(self.conversation_id), [])
        self.store.close_conversation(self.conversation_id)
        self.assertEqual(len(self.memories.save_memories(self.conversation_id, [candidate])), 1)

        interrupted_id = uuid4()
        self.store.create_conversation(interrupted_id)
        interrupted_source = self.save_source(conversation_id=interrupted_id)
        self.store.interrupt_active_conversations(uuid4())
        saved = self.memories.save_memories(interrupted_id, [self.candidate(interrupted_source)])
        self.assertEqual(len(saved), 1)
        conversation = self.store.get_conversation(interrupted_id)
        self.assertEqual(conversation.status, "interrupted")
        self.assertIsNone(conversation.ended_at)

    def test_bad_source_or_nonverbatim_evidence_rolls_back_the_whole_batch(self):
        source = self.save_source()
        assistant = self.save_source(role="assistant")
        other_id = uuid4()
        self.store.create_conversation(other_id)
        other_source = self.save_source(conversation_id=other_id)
        self.store.close_conversation(self.conversation_id)
        valid = self.candidate(source)
        cases = [
            self.candidate(assistant), self.candidate(other_source),
            MemoryCandidate(uuid4(), "fact", "Unknown source", "I live in Perth"),
            self.candidate(source, text="Nonverbatim", evidence="I LIVE IN PERTH"),
        ]
        for invalid in cases:
            with self.subTest(source=invalid.source_message_id, evidence=invalid.evidence):
                with self.assertRaises(ValueError):
                    self.memories.save_memories(self.conversation_id, [valid, invalid])
                self.assertEqual(self.memories.get_memories(self.conversation_id), [])
        self.assertEqual(len(self.store.get_messages(self.conversation_id)), 2)
        self.assertEqual(self.memories.get_memories(other_id), [])

    def test_retries_preserve_id_timestamp_and_evidence_and_concurrent_batch_is_one_row(self):
        source = self.save_source()
        self.store.close_conversation(self.conversation_id)
        candidate = self.candidate(source)
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(
                lambda _: self.memories.save_memories(self.conversation_id, [candidate]), range(4),
            ))
        first = results[0][0]
        self.assertIsNotNone(first.created_at.tzinfo)
        for rows in results:
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0].id, rows[0].created_at), (first.id, first.created_at))
        again = self.memories.save_memories(self.conversation_id, [candidate, candidate])[0]
        alternate_quote = self.candidate(source, evidence="live in Perth")
        reused = self.memories.save_memories(self.conversation_id, [alternate_quote])[0]
        for row in (again, reused):
            self.assertEqual((row.id, row.created_at, row.evidence), (first.id, first.created_at, first.evidence))
        self.assertEqual(len(self.memories.get_memories(self.conversation_id)), 1)

    def test_reads_are_conversation_scoped_sorted_and_saves_return_all_memories(self):
        first = self.save_source()
        second = self.save_source()
        other_id = uuid4()
        self.store.create_conversation(other_id)
        other = self.save_source(conversation_id=other_id)
        self.store.close_conversation(self.conversation_id)
        self.store.close_conversation(other_id)
        self.memories.save_memories(other_id, [self.candidate(other)])
        self.memories.save_memories(self.conversation_id, [self.candidate(second, text="Second source")])
        rows = self.memories.save_memories(self.conversation_id, [
            self.candidate(first, kind="preference", text="Small steps", evidence="I prefer small steps"),
            self.candidate(first, text="Z fact"), self.candidate(first, text="A fact"),
        ])
        expected = [
            (first.id, "fact", "A fact"), (first.id, "fact", "Z fact"),
            (first.id, "preference", "Small steps"), (second.id, "fact", "Second source"),
        ]
        self.assertEqual([(row.source_message_id, row.kind, row.text) for row in rows], expected)
        self.assertEqual([(row.source_message_id, row.kind, row.text)
                          for row in self.memories.get_memories(self.conversation_id)], expected)
        self.assertEqual(self.memories.get_memories(other_id)[0].source_message_id, other.id)
        self.assertEqual(self.memories.get_memories(uuid4()), [])

    def test_reopened_readback_and_migration_rerun_preserve_sources_and_optional_summary(self):
        source = self.save_source()
        closed = self.store.close_conversation(self.conversation_id)
        self.assertIsNone(self.store.get_summary(self.conversation_id))
        saved = self.memories.save_memories(self.conversation_id, [self.candidate(source)])[0]
        self.assertIsNone(self.store.get_summary(self.conversation_id))
        summary = self.store.save_summary(self.conversation_id, "An independently saved summary.")
        self.memories.save_memories(self.conversation_id, [self.candidate(source)])
        with psycopg.connect(**self.settings, autocommit=True) as connection:
            self.assertEqual(apply_migrations(connection), [])
        self.engine.dispose()
        with patch("storage.database.database_settings", return_value=self.settings):
            reopened = build_engine()
        try:
            result = MemoryStore(reopened).get_memories(self.conversation_id)[0]
            self.assertEqual((result.id, result.source_message_id, result.kind, result.text,
                              result.evidence, result.created_at),
                             (saved.id, source.id, saved.kind, saved.text, saved.evidence, saved.created_at))
            store = ConversationStore(reopened)
            message = store.get_messages(self.conversation_id)[0]
            fields = ("id", "conversation_id", "sequence_number", "role", "text", "channel",
                      "created_at", "status", "delivery_status")
            self.assertEqual(tuple(getattr(message, field) for field in fields),
                             tuple(getattr(source, field) for field in fields))
            read_summary = store.get_summary(self.conversation_id)
            self.assertEqual((read_summary.text, read_summary.created_at), (summary.text, summary.created_at))
            conversation = store.get_conversation(self.conversation_id)
            self.assertEqual((conversation.status, conversation.started_at, conversation.ended_at),
                             (closed.status, closed.started_at, closed.ended_at))
        finally:
            reopened.dispose()

    def test_database_constraints_and_source_deletion_restriction(self):
        source = self.save_source()
        values = {"source_message_id": source.id, "kind": "fact", "text": "Lives in Perth",
                  "evidence": "I live in Perth"}
        cases = [
            ({"kind": "unknown"}, "memories_kind_check"),
            ({"text": " "}, "memories_text_check"),
            ({"text": "x" * 501}, "memories_text_check"),
            ({"evidence": " "}, "memories_evidence_check"),
            ({"source_message_id": uuid4()}, "memories_source_message_id_fkey"),
        ]
        for changes, constraint in cases:
            with self.subTest(constraint=constraint), Session(self.engine) as session:
                with self.assertRaises(IntegrityError) as error:
                    with session.begin():
                        session.add(MemoryRecord(**{**values, **changes}))
                self.assertEqual(error.exception.orig.diag.constraint_name, constraint)
        self.store.close_conversation(self.conversation_id)
        # The boundary is inclusive; database and application both accept 500.
        boundary = self.memories.save_memories(self.conversation_id, [self.candidate(source, text="x" * 500)])
        self.assertEqual(len(boundary[0].text), 500)
        saved = self.memories.save_memories(self.conversation_id, [self.candidate(source)])
        with Session(self.engine) as session:
            with self.assertRaises(IntegrityError) as error:
                with session.begin():
                    session.add(MemoryRecord(**values))
            self.assertEqual(error.exception.orig.diag.constraint_name, "memories_source_kind_text_key")
        with self.assertRaises(IntegrityError) as error:
            with self.engine.begin() as connection:
                connection.execute(text("DELETE FROM public.messages WHERE id = :id"), {"id": source.id})
        self.assertEqual(error.exception.orig.diag.constraint_name, "memories_source_message_id_fkey")
        self.assertEqual(len(self.memories.get_memories(self.conversation_id)), len(saved))
        self.assertEqual(self.store.get_messages(self.conversation_id)[0].id, source.id)


if __name__ == "__main__":
    unittest.main()