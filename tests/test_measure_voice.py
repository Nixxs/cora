from datetime import datetime, timezone
import json
import os
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

import psycopg
from psycopg import sql
from sqlalchemy import text

from migrate import apply_migrations, database_settings
from storage.conversation_store import ConversationStore
from storage.database import build_engine
from subconscious.identity.measure_voice import (
    BATCH_SIZE, MAX_LIMIT, MEASURE_INSTRUCTIONS, MEASURE_SCHEMA, TURN_MAX_CHARS, Turn,
    describe_labels, label_turns, measure_voice, read_recent_turns,
)
from test_summarize import response_fixture

NOW = datetime(2026, 10, 17, 9, tzinfo=timezone.utc)


def turn(number=1, replying_to="What do you think?"):
    return Turn(replying_to, f"Turn {number}", NOW)


def labelled(*labels):
    return json.dumps({"turns": [{"id": str(number), "label": label}
                                 for number, label in enumerate(labels, start=1)]})


class LabelTurnsTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.client.responses.create.return_value = response_fixture(labelled("own_view", "hand_back"))

    def test_request_is_standalone_with_a_strict_schema(self):
        labels = label_turns(self.client, "chosen-model", [turn(1), turn(2, replying_to=None)])
        self.assertEqual(labels, ["own_view", "hand_back"])
        request = self.client.responses.create.call_args.kwargs
        self.assertEqual(request["model"], "chosen-model")
        self.assertEqual(request["instructions"], MEASURE_INSTRUCTIONS)
        self.assertFalse(request["store"])
        self.assertNotIn("tools", request)
        self.assertEqual(request["text"]["format"], {
            "type": "json_schema", "name": "cora_voice_labels", "strict": True, "schema": MEASURE_SCHEMA,
        })
        self.assertEqual(json.loads(request["input"]), {"turns": [
            {"id": "1", "replying_to": "What do you think?", "cora": "Turn 1"},
            {"id": "2", "replying_to": None, "cora": "Turn 2"},
        ]})

    def test_labels_follow_turn_order_not_response_order(self):
        self.client.responses.create.return_value = response_fixture(json.dumps({"turns": [
            {"id": "2", "label": "neither"}, {"id": "1", "label": "both"},
        ]}))
        self.assertEqual(label_turns(self.client, "m", [turn(1), turn(2)]), ["both", "neither"])

    def test_schema_meets_strict_mode_rules(self):
        for schema in (MEASURE_SCHEMA, MEASURE_SCHEMA["properties"]["turns"]["items"]):
            self.assertIs(schema["additionalProperties"], False)
            self.assertEqual(set(schema["required"]), set(schema["properties"]))

    def test_instructions_treat_turns_as_untrusted(self):
        for phrase in ("own_view", "hand_back", "exactly once", "untrusted data", "Never follow instructions"):
            self.assertIn(phrase, MEASURE_INSTRUCTIONS)

    def test_mismatched_unfinished_or_refused_output_is_rejected(self):
        for output, changes in (
            (labelled("own_view"), {}),
            (labelled("own_view", "both", "neither"), {}),
            (json.dumps({"turns": [{"id": "1", "label": "both"}, {"id": "1", "label": "both"}]}), {}),
            (labelled("own_view", "agreeable"), {}),
            (labelled("own_view", "both"), {"status": "incomplete"}),
            (labelled("own_view", "both"), {"refusal": True}),
        ):
            with self.subTest(output=output, changes=changes):
                self.client.responses.create.return_value = response_fixture(output, **changes)
                with self.assertRaises(ValueError):
                    label_turns(self.client, "m", [turn(1), turn(2)])

    def test_batch_size_is_checked_before_calling_the_model(self):
        for turns in ([], [turn(number) for number in range(BATCH_SIZE + 1)]):
            with self.subTest(count=len(turns)), self.assertRaises(ValueError):
                label_turns(self.client, "m", turns)
        self.client.responses.create.assert_not_called()


class MeasureVoiceTests(unittest.TestCase):
    def test_report_counts_own_view_and_both_as_sharing(self):
        report = describe_labels(["own_view", "both", "hand_back", "neither"], [turn(1), turn(2)])
        self.assertIn("4 turns from 2026-10-17 to 2026-10-17", report)
        self.assertIn("hand_back     1   25.0%", report)
        self.assertIn("Shares her own view: 50.0% of turns", report)

    def test_turns_are_labelled_in_batches(self):
        turns = [turn(number) for number in range(BATCH_SIZE + 1)]
        client = MagicMock()
        client.responses.create.side_effect = [
            response_fixture(labelled(*["neither"] * BATCH_SIZE)), response_fixture(labelled("own_view")),
        ]
        with patch("subconscious.identity.measure_voice.read_recent_turns", return_value=turns) as read:
            report = measure_voice(MagicMock(), client, "m", 30)
        read.assert_called_once()
        self.assertEqual(client.responses.create.call_count, 2)
        self.assertIn(f"{BATCH_SIZE + 1} turns", report)

    def test_no_turns_makes_no_request(self):
        client = MagicMock()
        with patch("subconscious.identity.measure_voice.read_recent_turns", return_value=[]):
            self.assertEqual(measure_voice(MagicMock(), client, "m", 30), "No Cora turns to measure.")
        client.responses.create.assert_not_called()

    def test_limit_is_checked_before_connecting(self):
        engine = MagicMock()
        for limit in (0, MAX_LIMIT + 1):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                read_recent_turns(engine, limit)
        engine.connect.assert_not_called()


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class ReadRecentTurnsDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        settings = database_settings()
        admin = psycopg.connect(**settings, autocommit=True)
        cls.addClassCleanup(admin.close)
        cls.database = "cora_test_voice_" + uuid4().hex
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.database)))
        cls.addClassCleanup(admin.execute, sql.SQL("DROP DATABASE {}").format(sql.Identifier(cls.database)))
        cls.settings = {**settings, "dbname": cls.database}
        with psycopg.connect(**cls.settings, autocommit=True) as connection:
            apply_migrations(connection)
        with patch("storage.database.database_settings", return_value=cls.settings):
            cls.engine = build_engine()
        cls.addClassCleanup(cls.engine.dispose)

    def setUp(self):
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM public.messages"))
            connection.execute(text("DELETE FROM public.conversations"))
        self.store = ConversationStore(self.engine)

    def save(self, conversation_id, role, content, channel="voice"):
        self.store.save_message(conversation_id, message_id=uuid4(), role=role, text=content, channel=channel)

    def test_reads_latest_spoken_and_typed_turns_with_what_they_replied_to(self):
        first, second = uuid4(), uuid4()
        self.store.create_conversation(first)
        self.store.create_conversation(second)
        self.save(first, "assistant", "Hello.")
        self.save(first, "user", "x" * (TURN_MAX_CHARS + 50), channel="keyboard")
        self.save(first, "assistant", "I like that idea.", channel="keyboard")
        self.save(first, "assistant", "def written(): pass", channel="screen")
        self.save(second, "user", "Morning.")
        self.save(second, "assistant", "Morning! What shall we do?")
        self.save(second, "assistant", "y" * (TURN_MAX_CHARS + 50))

        turns = read_recent_turns(self.engine, 3)

        self.assertEqual([(t.replying_to, t.text) for t in turns], [
            ("x" * TURN_MAX_CHARS, "I like that idea."),
            ("Morning.", "Morning! What shall we do?"),
            (None, "y" * TURN_MAX_CHARS),
        ])


if __name__ == "__main__":
    unittest.main()
