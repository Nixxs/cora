from concurrent.futures import ThreadPoolExecutor
import os
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

import psycopg
from psycopg import sql
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from storage.conversation_store import ConversationStore
from storage.database import build_engine
from migrate import apply_migrations, database_settings
from storage.models import ConversationSummaryRecord


class SummaryValidationTests(unittest.TestCase):
    def test_blank_summary_is_rejected_without_connecting(self):
        engine = MagicMock()
        store = ConversationStore(engine)
        for value in ("", " ", "\n\t"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "must not be blank"):
                store.save_summary(uuid4(), value)
        engine.connect.assert_not_called()


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class SummaryDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        settings = database_settings()
        admin = psycopg.connect(**settings, autocommit=True)
        cls.addClassCleanup(admin.close)
        database = "cora_test_summaries_" + uuid4().hex
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
        cls.addClassCleanup(admin.execute, sql.SQL("DROP DATABASE {}").format(sql.Identifier(database)))
        cls.settings = {**settings, "dbname": database}
        with psycopg.connect(**cls.settings, autocommit=True) as connection:
            apply_migrations(connection)
        with patch("storage.database.database_settings", return_value=cls.settings):
            cls.engine = build_engine()
        cls.addClassCleanup(cls.engine.dispose)

    def setUp(self):
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM public.conversation_summaries"))
            connection.execute(text("DELETE FROM public.messages"))
            connection.execute(text("DELETE FROM public.conversations"))
        self.store = ConversationStore(self.engine)
        self.conversation_id = uuid4()
        self.store.create_conversation(self.conversation_id)

    def test_closed_summary_commits_and_survives_engine_reopen_without_changing_messages(self):
        message = self.store.save_message(
            self.conversation_id, message_id=uuid4(), role="user", text="Discussed database memory",
            channel="voice", status="incomplete",
        )
        self.assertIsNone(self.store.get_summary(self.conversation_id))
        conversation = self.store.close_conversation(self.conversation_id)
        content = "  Discussed Cora's database memory.\nNext: add summarisation.  "
        saved = self.store.save_summary(self.conversation_id, content)
        self.assertEqual(saved.conversation_id, self.conversation_id)
        self.assertIsNotNone(saved.created_at.tzinfo)
        self.engine.dispose()
        with patch("storage.database.database_settings", return_value=self.settings):
            reopened = build_engine()
        try:
            store = ConversationStore(reopened)
            result = store.get_summary(self.conversation_id)
            self.assertEqual((result.text, result.created_at), (content, saved.created_at))
            row = store.get_messages(self.conversation_id)[0]
            self.assertEqual((row.id, row.text, row.status), (message.id, message.text, "incomplete"))
            self.assertEqual(store.get_conversation(self.conversation_id).ended_at, conversation.ended_at)
        finally:
            reopened.dispose()

    def test_interrupted_conversation_can_have_summary_without_an_end_timestamp(self):
        self.store.interrupt_active_conversations(uuid4())
        saved = self.store.save_summary(self.conversation_id, "Saved portion of an interrupted discussion.")
        self.assertEqual(self.store.get_summary(self.conversation_id).text, saved.text)
        conversation = self.store.get_conversation(self.conversation_id)
        self.assertEqual(conversation.status, "interrupted")
        self.assertIsNone(conversation.ended_at)

    def test_identical_retries_preserve_one_summary_and_original_timestamp(self):
        self.store.close_conversation(self.conversation_id)
        first = self.store.save_summary(self.conversation_id, "A short summary")
        again = self.store.save_summary(self.conversation_id, "A short summary")
        self.assertEqual(again.created_at, first.created_at)
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT count(*) FROM public.conversation_summaries")).scalar_one(), 1)

    def test_different_summary_is_rejected_without_overwriting(self):
        self.store.close_conversation(self.conversation_id)
        first = self.store.save_summary(self.conversation_id, "Original summary")
        with self.assertRaisesRegex(ValueError, "different summary"):
            self.store.save_summary(self.conversation_id, "Replacement summary")
        result = self.store.get_summary(self.conversation_id)
        self.assertEqual((result.text, result.created_at), (first.text, first.created_at))

    def test_active_or_missing_conversation_is_rejected_without_creating_rows(self):
        with self.assertRaisesRegex(ValueError, "closed or interrupted"):
            self.store.save_summary(self.conversation_id, "Too early")
        unknown = uuid4()
        with self.assertRaises(LookupError):
            self.store.save_summary(unknown, "No source conversation")
        self.assertIsNone(self.store.get_conversation(unknown))
        self.assertIsNone(self.store.get_summary(unknown))
        self.assertIsNone(self.store.get_summary(self.conversation_id))

    def test_database_rejects_orphans_blank_text_duplicates_and_source_deletion(self):
        cases = [
            (uuid4(), "Orphan summary", "conversation_summaries_conversation_id_fkey"),
            (self.conversation_id, " ", "conversation_summaries_text_check"),
        ]
        for conversation_id, content, constraint in cases:
            with self.subTest(constraint=constraint), Session(self.engine) as session:
                with self.assertRaises(IntegrityError) as error:
                    with session.begin():
                        session.add(ConversationSummaryRecord(conversation_id=conversation_id, text=content))
                self.assertEqual(error.exception.orig.diag.constraint_name, constraint)
        self.store.close_conversation(self.conversation_id)
        self.store.save_summary(self.conversation_id, "Keep the source")
        with Session(self.engine) as session, self.assertRaises(IntegrityError):
            with session.begin():
                session.add(ConversationSummaryRecord(conversation_id=self.conversation_id, text="Duplicate"))
        with self.engine.begin() as connection, self.assertRaises(IntegrityError):
            connection.execute(text("DELETE FROM public.conversations WHERE id = :id"), {"id": self.conversation_id})
        self.assertIsNotNone(self.store.get_conversation(self.conversation_id))

    def test_concurrent_identical_saves_create_only_one_summary(self):
        self.store.close_conversation(self.conversation_id)
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(
                lambda _: self.store.save_summary(self.conversation_id, "One summary"), range(4)
            ))
        self.assertTrue(all(result.created_at == results[0].created_at for result in results))
        self.assertEqual(self.store.get_summary(self.conversation_id).text, "One summary")

    def test_migration_rerun_preserves_summary(self):
        self.store.close_conversation(self.conversation_id)
        saved = self.store.save_summary(self.conversation_id, "Retain summary on rerun")
        with psycopg.connect(**self.settings, autocommit=True) as connection:
            self.assertEqual(apply_migrations(connection), [])
        result = self.store.get_summary(self.conversation_id)
        self.assertEqual((result.text, result.created_at), (saved.text, saved.created_at))


if __name__ == "__main__":
    unittest.main()