from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

import psycopg
from psycopg import sql
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from conversation_store import ConversationStore
from database import build_engine
from migrate import apply_migrations, database_settings


class ConversationStoreTests(unittest.TestCase):
    def test_construction_and_blank_text_validation_do_not_connect(self):
        engine = MagicMock()
        store = ConversationStore(engine)
        self.assertIs(store.engine, engine)
        for content in ("", " ", "\n\t"):
            with self.subTest(content=content), self.assertRaisesRegex(ValueError, "must not be blank"):
                store.save_message(uuid4(), message_id=uuid4(), role="user", text=content, channel="keyboard")
        engine.connect.assert_not_called()


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class ConversationStoreDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        settings = database_settings()
        admin = psycopg.connect(**settings, autocommit=True)
        cls.addClassCleanup(admin.close)
        cls.database = "cora_test_store_" + uuid4().hex
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.database)))
        cls.addClassCleanup(
            admin.execute, sql.SQL("DROP DATABASE {}").format(sql.Identifier(cls.database))
        )
        cls.settings = {**settings, "dbname": cls.database}
        with psycopg.connect(**cls.settings, autocommit=True) as connection:
            apply_migrations(connection)
        with patch("database.database_settings", return_value=cls.settings):
            cls.engine = build_engine()
        cls.addClassCleanup(cls.engine.dispose)

    def setUp(self):
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM public.messages"))
            connection.execute(text("DELETE FROM public.conversations"))
        self.store = ConversationStore(self.engine)
        self.conversation_id = uuid4()
        self.store.create_conversation(self.conversation_id)

    def save(self, **changes):
        values = {
            "message_id": uuid4(), "role": "user", "text": "Test message",
            "channel": "keyboard", "status": "completed", "delivery_status": "sent",
        }
        return self.store.save_message(self.conversation_id, **{**values, **changes})

    def test_conversation_creation_retries_preserve_existing_record(self):
        first = self.store.get_conversation(self.conversation_id)
        repeated = self.store.create_conversation(self.conversation_id)
        self.assertEqual(repeated.id, first.id)
        self.assertEqual(repeated.started_at, first.started_at)
        self.assertEqual(repeated.status, "active")
        self.assertIsNone(repeated.ended_at)
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT count(*) FROM public.conversations")).scalar_one(), 1)

    def test_messages_survive_engine_disposal_and_a_fresh_python_process(self):
        self.save(text="Voice message", channel="voice")
        self.save(text="Keyboard message")
        self.save(role="assistant", text="  Answer\n    code  ", channel="screen", delivery_status="not_applicable")
        expected = ["Voice message", "Keyboard message", "  Answer\n    code  "]
        self.engine.dispose()
        with patch("database.database_settings", return_value=self.settings):
            reopened = build_engine()
        try:
            records = ConversationStore(reopened).get_messages(self.conversation_id)
            self.assertEqual([message.sequence_number for message in records], [1, 2, 3])
            self.assertEqual([message.text for message in records], expected)
        finally:
            reopened.dispose()
        code = """
import json
import sys
from uuid import UUID
from database import build_engine
from conversation_store import ConversationStore
engine = build_engine()
try:
    records = ConversationStore(engine).get_messages(UUID(sys.argv[1]))
    print(json.dumps([message.text for message in records]))
finally:
    engine.dispose()
"""
        environment = {**os.environ, **{
            "CORA_DB_NAME": self.database, "CORA_DB_USER": self.settings["user"],
            "CORA_DB_PASSWORD": self.settings["password"], "CORA_DB_HOST": self.settings["host"],
            "CORA_DB_PORT": str(self.settings["port"]),
        }}
        result = subprocess.run(
            [sys.executable, "-c", code, str(self.conversation_id)],
            cwd=Path(__file__).resolve().parents[1], env=environment,
            capture_output=True, text=True, check=True, timeout=15,
        )
        self.assertEqual(json.loads(result.stdout), expected)

    def test_same_id_retry_preserves_one_message_and_next_ordering_position(self):
        message_id = uuid4()
        first = self.save(message_id=message_id)
        repeated = self.save(message_id=message_id)
        self.assertEqual(repeated.id, first.id)
        self.assertEqual(repeated.sequence_number, first.sequence_number)
        self.assertEqual(repeated.created_at, first.created_at)
        self.assertEqual(self.save().sequence_number, 2)
        self.assertEqual(len(self.store.get_messages(self.conversation_id)), 2)

    def test_same_id_with_different_payload_is_rejected_without_overwriting(self):
        message_id = uuid4()
        self.save(message_id=message_id)
        for changes in (
            {"text": "Different text"}, {"role": "assistant"}, {"channel": "voice"},
            {"status": "interrupted"}, {"delivery_status": "failed"},
        ):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "different content or metadata"):
                self.save(message_id=message_id, **changes)
        another_id = uuid4()
        self.store.create_conversation(another_id)
        with self.assertRaises(ValueError):
            self.store.save_message(
                another_id, message_id=message_id, role="user", text="Test message",
                channel="keyboard", status="completed", delivery_status="sent",
            )
        records = self.store.get_messages(self.conversation_id)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].text, "Test message")
        self.assertEqual(records[0].status, "completed")

    def test_invalid_save_rolls_back_without_consuming_sequence(self):
        with self.assertRaises(IntegrityError):
            self.save(role="invalid")
        self.assertEqual(self.store.get_messages(self.conversation_id), [])
        self.assertEqual(self.save().sequence_number, 1)

    def test_missing_conversation_does_not_create_orphan_message(self):
        unknown_id = uuid4()
        self.assertIsNone(self.store.get_conversation(unknown_id))
        self.assertEqual(self.store.get_messages(unknown_id), [])
        with self.assertRaises(LookupError):
            self.store.save_message(
                unknown_id, message_id=uuid4(), role="user", text="Hello", channel="keyboard"
            )

    def test_default_incomplete_and_interrupted_status_are_preserved(self):
        partial = self.store.save_message(
            self.conversation_id, message_id=uuid4(), role="assistant", text="Partial", channel="voice"
        )
        interrupted = self.save(role="assistant", status="interrupted", delivery_status="not_applicable")
        self.assertEqual(partial.status, "incomplete")
        self.assertEqual(partial.delivery_status, "not_applicable")
        self.assertEqual(interrupted.status, "interrupted")

    def test_delivery_update_preserves_payload_and_rejects_unknown_or_invalid_updates(self):
        saved = self.save(delivery_status="pending")
        updated = self.store.set_delivery_status(self.conversation_id, saved.id, "sent")
        self.assertEqual(updated.delivery_status, "sent")
        self.assertEqual((updated.text, updated.sequence_number, updated.created_at),
                         (saved.text, saved.sequence_number, saved.created_at))
        with self.assertRaises(LookupError):
            self.store.set_delivery_status(uuid4(), saved.id, "failed")
        with self.assertRaises(LookupError):
            self.store.set_delivery_status(self.conversation_id, uuid4(), "failed")
        with self.assertRaises(IntegrityError):
            self.store.set_delivery_status(self.conversation_id, saved.id, "unknown")
        self.assertEqual(self.store.get_messages(self.conversation_id)[0].delivery_status, "sent")

    def test_concurrent_distinct_saves_allocate_unique_sequence_numbers(self):
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda number: self.save(text=f"Message {number}"), range(8)))
        self.assertEqual(sorted(message.sequence_number for message in results), list(range(1, 9)))
        self.assertEqual(len(self.store.get_messages(self.conversation_id)), 8)

    def test_concurrent_same_id_retries_create_only_one_record(self):
        message_id = uuid4()
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: self.save(message_id=message_id), range(4)))
        self.assertTrue(all(message.sequence_number == 1 for message in results))
        self.assertEqual(len(self.store.get_messages(self.conversation_id)), 1)


if __name__ == "__main__":
    unittest.main()