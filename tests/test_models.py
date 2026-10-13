import os
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from sqlalchemy import CheckConstraint, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, configure_mappers

from storage.database import build_engine
from migrate import apply_migrations, database_settings
from storage.models import Base, ConversationRecord, ConversationSummaryRecord, MessageRecord


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
                "public.memories",
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
            self.assertEqual(connection.execute("SELECT count(*) FROM public.schema_migrations").fetchone()[0], 6)


if __name__ == "__main__":
    unittest.main()