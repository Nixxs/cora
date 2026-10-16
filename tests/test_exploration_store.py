"""Exploration attempts: validation plus opt-in disposable PostgreSQL tests."""

from datetime import datetime, timedelta, timezone
import json
import os
import threading
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from storage.models import InterestExplorationRecord
from subconscious.exploration_store import LIST_LIMIT, ExplorationStore
from subconscious.interest_strength import HALF_LIFE
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
            for call in (lambda: store.start_next(now=naive), lambda: store.fail(uuid4(), now=naive),
                         lambda: store.finish(uuid4(), *FINDING, now=naive),
                         lambda: store.claim_mention(now=naive)):
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

    def start(self, now=NOW):
        result = self.explorations.start_next(now=now)
        self.assertEqual(result.reason, "started")
        return result.exploration

    def attempts(self):
        with Session(self.engine) as session:
            return list(session.scalars(select(InterestExplorationRecord).order_by(
                InterestExplorationRecord.started_at)))

    def test_an_attempt_runs_then_records_a_cleaned_finding_once(self):
        started = self.start()
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
        started = self.start()
        failed = self.explorations.fail(started.id, now=NOW - timedelta(hours=1))  # clock moved back
        self.assertEqual((failed.status, failed.finding, failed.finished_at), ("failed", None, NOW))
        self.assertIsNone(self.explorations.finish(started.id, *FINDING, now=NOW))
        self.assertEqual(self.explorations.list_recent(), [failed])

    def test_unknown_attempts_are_errors(self):
        with self.assertRaises(LookupError):
            self.explorations.finish(uuid4(), *FINDING, now=NOW)
        with self.assertRaises(LookupError):
            self.explorations.fail(uuid4(), now=NOW)
        self.assertEqual(self.explorations.list_recent(), [])

    def test_recent_attempts_are_newest_first_and_limited(self):
        for title in ("Second", "Third"):
            self.interests.spawn(*interest_tests.spawn_args(title), now=NOW)
        ids = [self.start(NOW + timedelta(hours=6 * n)).id for n in range(3)]
        self.assertEqual([item.id for item in self.explorations.list_recent(2)], ids[:0:-1])
        self.assertEqual([item.id for item in self.explorations.list_recent()], ids[::-1])

    def test_one_attempt_per_six_hours_counting_failed_attempts(self):
        self.interests.spawn(*interest_tests.spawn_args("Second"), now=NOW)
        self.explorations.fail(self.start().id, now=NOW)
        for hours in (1, 5.9):
            self.assertEqual(self.explorations.start_next(now=NOW + timedelta(hours=hours)).reason, "too soon")
        self.start(NOW + timedelta(hours=6, seconds=1))

    def test_at_most_three_attempts_in_any_24_hours(self):
        for title in ("Second", "Third", "Fourth"):
            self.interests.spawn(*interest_tests.spawn_args(title), now=NOW)
        for hours in (0, 6.1, 12.2):
            self.start(NOW + timedelta(hours=hours))
        self.assertEqual(self.explorations.start_next(now=NOW + timedelta(hours=18.3)).reason, "daily limit")
        self.start(NOW + timedelta(hours=24, seconds=1))  # the first attempt has left the window

    def test_each_interest_waits_a_day_and_least_recently_explored_goes_first(self):
        second = self.interests.spawn(*interest_tests.spawn_args("Second"), now=NOW)
        self.interests.reinforce(second.id, 0.15, "Stronger", now=NOW)
        order = [self.explorations.start_next(now=NOW + timedelta(hours=hours)) for hours in (0, 6.1, 12.2)]
        # Strongest unexplored first, then the other, then both are cooling down.
        self.assertEqual([result.reason for result in order], ["started", "started", "no eligible interest"])
        self.assertEqual([result.interest.id for result in order[:2]], [second.id, self.interest.id])
        self.assertEqual(order[0].interest.title, "Second")
        self.assertEqual(self.start(NOW + timedelta(hours=24, seconds=1)).interest_id, second.id)

    def test_dormant_and_faded_interests_are_never_explored(self):
        later = NOW + 2 * HALF_LIFE  # 0.2 decays to 0.05: still stored as emerging, but faded
        self.assertEqual(self.explorations.start_next(now=later).reason, "no eligible interest")
        self.interests.settle_all(later)  # now stored as dormant
        self.assertEqual(self.explorations.start_next(now=later).reason, "no eligible interest")
        self.assertEqual(self.attempts(), [])

    def test_abandoned_running_attempts_fail_but_still_count(self):
        started = self.start()
        self.assertEqual(self.explorations.start_next(now=NOW + timedelta(minutes=29)).reason, "too soon")
        self.assertEqual(self.attempts()[0].status, "running")
        later = NOW + timedelta(minutes=31)
        self.assertEqual(self.explorations.start_next(now=later).reason, "too soon")
        [record] = self.attempts()
        self.assertEqual((record.status, record.finished_at), ("failed", later))
        self.assertIsNone(self.explorations.finish(started.id, *FINDING, now=later))

    def test_attempts_from_a_later_clock_still_block(self):
        self.interests.spawn(*interest_tests.spawn_args("Second"), now=NOW)
        self.start(NOW + timedelta(days=2))
        self.assertEqual(self.explorations.start_next(now=NOW).reason, "too soon")

    def test_startup_context_carries_only_the_newest_finding_per_interest(self):
        other = self.interests.spawn(*interest_tests.spawn_args("Second"), now=NOW)
        titles = {self.interest.id: self.interest.title, other.id: other.title}
        older = self.start()
        self.explorations.finish(older.id, "Older finding", [], [], now=NOW)
        failed = self.start(NOW + timedelta(hours=6, seconds=1))
        self.explorations.fail(failed.id, now=NOW + timedelta(hours=7))
        later = NOW + timedelta(hours=24, seconds=2)
        newer = self.start(later)
        self.assertEqual(newer.interest_id, older.interest_id)
        self.explorations.finish(newer.id, "Newer finding", [], [], now=later)
        self.assertEqual(self.interests.latest_findings([older.interest_id, failed.interest_id]),
                         {older.interest_id: ("Newer finding", later)})
        records = json.loads(self.interests.get_startup_context(now=later))
        self.assertEqual({record["title"]: record.get("latest_finding") for record in records},
                         {titles[older.interest_id]: "Newer finding", titles[failed.interest_id]: None})

    def found(self, finding, now=NOW):
        self.explorations.finish(self.start(now).id, finding, [], [], now=now)

    def mentioned_at(self):
        return {record.finding: record.mentioned_at for record in self.attempts()}

    def test_a_fresh_finding_is_claimed_once_before_it_is_offered(self):
        self.found("Carburettors were tuned by ear")
        later = NOW + timedelta(minutes=5)
        mention = self.explorations.claim_mention(now=later)
        self.assertEqual((mention.interest_title, mention.finding, mention.found_at),
                         (self.interest.title, "Carburettors were tuned by ear", NOW))
        self.assertEqual(self.mentioned_at(), {"Carburettors were tuned by ear": later})
        self.assertIsNone(self.explorations.claim_mention(now=later + timedelta(days=1)))
        self.assertEqual(self.mentioned_at(), {"Carburettors were tuned by ear": later})

    def test_mentions_wait_four_hours_and_at_most_two_a_day(self):
        for title in ("Second", "Third"):
            self.interests.spawn(*interest_tests.spawn_args(title), now=NOW)
        for n, finding in enumerate(("First", "Second", "Third")):
            self.found(finding, NOW + timedelta(hours=6.1 * n))
        first = NOW + timedelta(hours=13)
        claims = [self.explorations.claim_mention(now=first + timedelta(hours=hours))
                  for hours in (0, 3.9, 4.1, 8.2, 24.1)]
        # Newest first; then too soon, the next, the daily limit, and the oldest a day later.
        self.assertEqual([claim and claim.finding for claim in claims],
                         ["Third", None, "Second", None, "First"])

    def test_only_each_interests_latest_fresh_finding_from_a_live_interest(self):
        self.found("Older")
        later = NOW + timedelta(hours=24, seconds=1)
        self.found("Newer", later)
        self.assertEqual(self.explorations.claim_mention(now=later).finding, "Newer")
        # The older finding is superseded, never raised afterwards.
        self.assertIsNone(self.explorations.claim_mention(now=later + timedelta(days=1)))

        other = self.interests.spawn(*interest_tests.spawn_args("Second"), now=later)
        stale = later + timedelta(hours=6, seconds=1)
        self.found("Stale", stale)
        self.assertIsNone(self.explorations.claim_mention(now=stale + timedelta(days=3)))
        with self.engine.begin() as connection:
            connection.execute(text(
                "UPDATE public.interests SET status = 'dormant', strength = 0.05 WHERE id = :id"
            ), {"id": other.id})
        self.assertIsNone(self.explorations.claim_mention(now=stale + timedelta(days=1)))
        self.assertIsNone(self.mentioned_at()["Stale"])

    def test_failed_attempts_are_never_mentioned(self):
        self.explorations.fail(self.start().id, now=NOW)
        self.assertIsNone(self.explorations.claim_mention(now=NOW))
        self.assertEqual(self.mentioned_at(), {None: None})

    def test_a_released_claim_can_be_offered_again_and_frees_the_limits(self):
        self.found("Carburettors were tuned by ear")
        mention = self.explorations.claim_mention(now=NOW)
        self.assertEqual(mention.mentioned_at, NOW)
        self.assertTrue(self.explorations.release_mention(mention))
        self.assertFalse(self.explorations.release_mention(mention))  # already released
        self.assertEqual(self.mentioned_at(), {"Carburettors were tuned by ear": None})
        later = NOW + timedelta(minutes=1)  # the released claim no longer counts toward the gap
        again = self.explorations.claim_mention(now=later)
        self.assertEqual(again.exploration_id, mention.exploration_id)
        # An old claim never undoes a newer one.
        self.assertFalse(self.explorations.release_mention(mention))
        self.assertEqual(self.mentioned_at(), {"Carburettors were tuned by ear": later})

    def test_a_start_waits_for_a_concurrent_start_and_sees_its_attempt(self):
        self.interests.spawn(*interest_tests.spawn_args("Second"), now=NOW)
        results = []
        with self.engine.connect() as other, other.begin():
            other.execute(text("LOCK TABLE public.interest_explorations IN SHARE ROW EXCLUSIVE MODE"))
            other.execute(text(
                "INSERT INTO public.interest_explorations (interest_id, status, started_at, next_questions, sources) "
                "VALUES (:interest_id, 'running', :now, '{}', '{}')"
            ), {"interest_id": self.interest.id, "now": NOW})
            thread = threading.Thread(target=lambda: results.append(self.explorations.start_next(now=NOW)))
            thread.start()
            thread.join(0.5)
            self.assertTrue(thread.is_alive())  # blocked by the other transaction's lock
        thread.join(5)
        self.assertEqual([result.reason for result in results], ["too soon"])
        self.assertEqual(len(self.attempts()), 1)


if __name__ == "__main__":
    unittest.main()
