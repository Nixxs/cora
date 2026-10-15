"""Exploration attempts: validation plus opt-in disposable PostgreSQL tests."""

from datetime import datetime, timedelta, timezone
import os
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from subconscious.exploration_store import LIST_LIMIT, ExplorationStore
import test_interest_store as interest_tests

NOW = interest_tests.NOW
FINDING = ("Carburettors were tuned by ear", ["How did fuel injection replace them?"],
           ["https://example.com/carburettors"])


class ExplorationValidationTests(unittest.TestCase):
    def test_invalid_inputs_are_rejected_before_connecting(self):
        engine = MagicMock()
        store = ExplorationStore(engine)
        finding, questions, sources = FINDING
        with patch("subconscious.exploration_store.Session") as session:
            for args in (
                ("", questions, sources), ("x" * 2001, questions, sources), (None, questions, sources),
                (finding, "One question", sources), (finding, ["a", "b", "c", "d"], sources),
                (finding, [" "], sources), (finding, ["x" * 301], sources),
                (finding, questions, "https://example.com"),
                (finding, questions, [f"https://example.com/{n}" for n in range(11)]),
                (finding, questions, ["javascript:alert(1)"]), (finding, questions, ["file:///etc/passwd"]),
                (finding, questions, ["https://"]), (finding, questions, ["https://exa mple.com"]),
                (finding, questions, ["example.com"]),
            ):
                with self.subTest(args=args), self.assertRaises(ValueError):
                    store.finish(uuid4(), *args, now=NOW)
            naive = datetime(2026, 10, 15)
            for call in (lambda: store.start(uuid4(), now=naive), lambda: store.fail(uuid4(), now=naive),
                         lambda: store.finish(uuid4(), *FINDING, now=naive)):
                with self.assertRaises(ValueError):
                    call()
            for limit in (0, LIST_LIMIT + 1, True, 1.0):
                with self.subTest(limit=limit), self.assertRaises(ValueError):
                    store.list_recent(limit)
            session.assert_not_called()
        engine.connect.assert_not_called()


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class ExplorationStoreDatabaseTests(unittest.TestCase):
    setUpClass = classmethod(interest_tests.summary_tests.SummaryDatabaseTests.setUpClass.__func__)

    def setUp(self):
        interest_tests.InterestStoreDatabaseTests.setUp(self)
        self.explorations = ExplorationStore(self.engine)
        self.interest = self.interests.spawn(*interest_tests.spawn_args(), now=NOW)

    def test_an_attempt_runs_then_records_a_cleaned_finding_once(self):
        started = self.explorations.start(self.interest.id, now=NOW)
        self.assertEqual((started.status, started.finished_at, started.finding), ("running", None, None))
        self.assertEqual((started.next_questions, started.sources), ((), ()))
        found = self.explorations.finish(
            started.id, "  Carburettors were tuned by ear  ",
            ["How did fuel injection replace them?", "How did fuel injection replace them?"],
            ["https://example.com/a", "https://example.com/a", "http://example.org/b"],
            now=NOW + timedelta(minutes=2),
        )
        self.assertEqual(found.status, "found")
        self.assertEqual(found.finding, "Carburettors were tuned by ear")
        self.assertEqual(found.next_questions, ("How did fuel injection replace them?",))
        self.assertEqual(found.sources, ("https://example.com/a", "http://example.org/b"))
        self.assertEqual(found.finished_at, NOW + timedelta(minutes=2))
        self.assertEqual(self.explorations.list_recent(), [found])
        self.assertIsNone(self.explorations.finish(started.id, *FINDING, now=NOW))
        self.assertIsNone(self.explorations.fail(started.id, now=NOW))
        self.assertEqual(self.explorations.list_recent(), [found])

    def test_a_failed_attempt_keeps_no_finding_and_cannot_be_finished_later(self):
        started = self.explorations.start(self.interest.id, now=NOW)
        failed = self.explorations.fail(started.id, now=NOW - timedelta(hours=1))  # clock moved back
        self.assertEqual((failed.status, failed.finding, failed.finished_at), ("failed", None, NOW))
        self.assertIsNone(self.explorations.finish(started.id, *FINDING, now=NOW))
        self.assertEqual(self.explorations.list_recent(), [failed])

    def test_unknown_attempts_and_interests_are_errors(self):
        with self.assertRaises(LookupError):
            self.explorations.finish(uuid4(), *FINDING, now=NOW)
        with self.assertRaises(LookupError):
            self.explorations.fail(uuid4(), now=NOW)
        with self.assertRaises(Exception):
            self.explorations.start(uuid4(), now=NOW)
        self.assertEqual(self.explorations.list_recent(), [])

    def test_recent_attempts_are_newest_first_and_limited(self):
        ids = [self.explorations.start(self.interest.id, now=NOW + timedelta(hours=n)).id for n in range(3)]
        self.assertEqual([item.id for item in self.explorations.list_recent(2)], ids[:0:-1])
        self.assertEqual([item.id for item in self.explorations.list_recent()], ids[::-1])


if __name__ == "__main__":
    unittest.main()
