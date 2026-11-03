"""Feeling store: saving reactions within the limits and reading back levels. Opt-in database tests."""

from datetime import datetime, timedelta, timezone
import os
import unittest
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from subconscious.emotions.feeling_rules import (
    HALF_LIFE, LOOKBACK, MAX_PER_CONVERSATION, REASON_MAX_CHARS, SAME_FEELING_GAP, Reaction,
)
from subconscious.emotions.feeling_store import FeelingLimit, FeelingStore
import test_conversation_summaries as summary_tests

NOW = datetime(2026, 11, 3, 10, 30, tzinfo=timezone.utc)


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class FeelingStoreDatabaseTests(unittest.TestCase):
    setUpClass = classmethod(summary_tests.SummaryDatabaseTests.setUpClass.__func__)

    def setUp(self):
        # Only in the disposable DB created above. Children before parents.
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM public.feeling_events"))
        summary_tests.SummaryDatabaseTests.setUp(self)
        self.feelings = FeelingStore(self.engine)

    def test_a_reaction_is_saved_and_counts_towards_how_she_feels(self):
        saved = self.feelings.record("hurt", "strong", "  told I   don't care ", now=NOW - HALF_LIFE,
                                     conversation_id=self.conversation_id)
        self.assertEqual((saved.reason, saved.conversation_id), ("told I don't care", self.conversation_id))
        self.feelings.record("curiosity", "small", "a new idea", now=NOW)
        self.assertEqual(self.feelings.reactions(NOW), [
            Reaction("hurt", "strong", NOW - HALF_LIFE, "told I don't care"),
            Reaction("curiosity", "small", NOW, "a new idea"),
        ])
        levels = self.feelings.levels(NOW)
        self.assertAlmostEqual(levels["hurt"], 0.15)
        self.assertAlmostEqual(levels["curiosity"], 0.1)
        self.assertTrue(self.feelings.feelings_now(NOW)[0].startswith("You feel hurt (about: told I don't care)."))

    def test_only_what_still_counts_and_nothing_after_now_is_read(self):
        self.feelings.record("excitement", "strong", "long ago", now=NOW - LOOKBACK)
        self.feelings.record("excitement", "small", "later", now=NOW + timedelta(minutes=1))
        self.assertEqual(self.feelings.reactions(NOW), [])
        self.assertEqual(len(self.feelings.reactions(NOW, since=NOW - LOOKBACK - timedelta(seconds=1))), 1)

    def test_limits_in_a_conversation_but_not_outside_one(self):
        record = lambda feeling, minutes, conversation=self.conversation_id: self.feelings.record(
            feeling, "small", "why", now=NOW + timedelta(minutes=minutes), conversation_id=conversation)
        record("hurt", 0)
        with self.assertRaisesRegex(FeelingLimit, "only a moment ago"):
            record("hurt", SAME_FEELING_GAP.total_seconds() / 60 - 1)
        record("hurt", SAME_FEELING_GAP.total_seconds() / 60)
        for minute in range(MAX_PER_CONVERSATION - 2):
            record("curiosity", 10 + minute * 10)
        with self.assertRaisesRegex(FeelingLimit, "enough feelings"):
            record("excitement", 200)
        # Outside a conversation, and in another one, the limits don't carry over.
        record("excitement", 200, None)
        record("excitement", 200, None)
        other = uuid4()
        self.store.create_conversation(other)
        record("excitement", 200, other)
        with self.assertRaises(LookupError):
            record("excitement", 200, uuid4())

    def test_the_app_notes_one_in_the_persons_conversation_creating_it_if_needed(self):
        from storage.conversation_store import ConversationStore
        from storage.keyboard_persistence import KeyboardPersistence
        persistence = KeyboardPersistence(ConversationStore(self.engine))
        persistence.record_feeling("excitement", "medium", "a new idea")
        self.assertIsNotNone(self.store.get_conversation(persistence.conversation_id))
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text(
                "SELECT feeling, conversation_id FROM public.feeling_events")).one(),
                ("excitement", persistence.conversation_id))
        with self.assertRaises(FeelingLimit):
            persistence.record_feeling("excitement", "small", "again")

    def test_bad_reactions_are_refused_by_the_store_and_the_database(self):
        for args in (("guilt", "small", "why"), ("hurt", "huge", "why"), ("hurt", "small", "   ")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.feelings.record(*args, now=NOW)
        self.assertEqual(len(self.feelings.record("hurt", "small", "x" * 500, now=NOW).reason), REASON_MAX_CHARS)
        for feeling, strength, reason in (("guilt", "small", "why"), ("hurt", "huge", "why"), ("hurt", "small", " "),
                                          ("hurt", "small", "x" * 201)):
            with self.subTest(feeling=feeling, strength=strength), self.assertRaises(IntegrityError), \
                    self.engine.begin() as connection:
                connection.execute(text(
                    "INSERT INTO public.feeling_events (feeling, strength, reason, felt_at) "
                    "VALUES (:feeling, :strength, :reason, now())"),
                    {"feeling": feeling, "strength": strength, "reason": reason})


if __name__ == "__main__":
    unittest.main()
