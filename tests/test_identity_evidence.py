"""Identity evidence reader: validation plus opt-in disposable PostgreSQL tests."""

from datetime import datetime, timedelta
import os
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from sqlalchemy.orm import Session

from storage.models import ConversationSummaryRecord, InterestEventRecord, InterestExplorationRecord, InterestRecord
from subconscious.identity.identity_evidence import COMMIT_MARGIN, MAX_EXPERIENCES, read_experiences
from subconscious.identity.identity_rules import Evidence
import test_interest_store as interest_tests

NOW = interest_tests.NOW


class EvidenceValidationTests(unittest.TestCase):
    def test_naive_times_are_rejected_before_connecting(self):
        engine = MagicMock()
        with patch("subconscious.identity.identity_evidence.Session") as session:
            for after, now in ((None, datetime(2026, 10, 15)), (datetime(2026, 10, 14), NOW)):
                with self.subTest(after=after, now=now), self.assertRaises(ValueError):
                    read_experiences(engine, after=after, now=now)
            session.assert_not_called()

    def test_nothing_is_read_when_already_read_through_the_window(self):
        with patch("subconscious.identity.identity_evidence.Session") as session:
            batch = read_experiences(MagicMock(), after=NOW - COMMIT_MARGIN, now=NOW)
            session.assert_not_called()
        self.assertEqual((batch.experiences, batch.until, batch.more), ((), NOW - COMMIT_MARGIN, False))


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class EvidenceDatabaseTests(unittest.TestCase):
    setUpClass = classmethod(interest_tests.summary_tests.SummaryDatabaseTests.setUpClass.__func__)

    def setUp(self):
        interest_tests.InterestStoreDatabaseTests.setUp(self)
        with Session(self.engine) as session, session.begin():
            interest = InterestRecord(title="Old engines", description="How they work", why_emerged="A car",
                                      strength=0.2, strength_at=NOW, status="emerging")
            session.add(interest)
            session.flush()
            self.interest_id = interest.id

    def event(self, at, reason="Came up again"):
        with Session(self.engine) as session, session.begin():
            record = InterestEventRecord(interest_id=self.interest_id, kind="reinforce", strength_before=0.2,
                                         strength_after=0.3, reason=reason, created_at=at)
            session.add(record)
            session.flush()
            return record.id

    def exploration(self, at, status="found", reaction=None):
        values = {"finding": "Carburettors were tuned by ear"} if status == "found" else {}
        with Session(self.engine) as session, session.begin():
            record = InterestExplorationRecord(interest_id=self.interest_id, status=status, started_at=at,
                                               finished_at=at, reaction=reaction, **values)
            session.add(record)
            session.flush()
            return record.id

    def test_experiences_from_all_sources_oldest_first_within_the_window(self):
        before = self.event(NOW - timedelta(days=2))
        event = self.event(NOW - timedelta(hours=3))
        found = self.exploration(NOW - timedelta(hours=2), reaction="Lovely, and odd.")
        plain = self.exploration(NOW - timedelta(hours=1, minutes=30))
        self.exploration(NOW - timedelta(hours=1), status="failed")
        with Session(self.engine) as session, session.begin():
            session.add(ConversationSummaryRecord(conversation_id=self.conversation_id, text="We talked engines",
                                                  created_at=NOW - timedelta(hours=1)))
        self.event(NOW - timedelta(minutes=1))  # may still be committing: next time
        batch = read_experiences(self.engine, after=NOW - timedelta(days=1), now=NOW)
        self.assertEqual((batch.until, batch.more), (NOW - COMMIT_MARGIN, False))
        self.assertEqual([item.evidence for item in batch.experiences], [
            Evidence("interest_event", event), Evidence("exploration", found),
            Evidence("exploration", plain), Evidence("conversation", self.conversation_id),
        ])
        self.assertEqual([dict(item.details) for item in batch.experiences], [
            {"interest": "Old engines", "change": "reinforce", "reason": "Came up again"},
            {"interest": "Old engines", "finding": "Carburettors were tuned by ear",
             "your_reaction": "Lovely, and odd."},
            {"interest": "Old engines", "finding": "Carburettors were tuned by ear"},
            {"summary": "We talked engines"},
        ])
        self.assertEqual(batch.experiences[0].at, NOW - timedelta(hours=3))
        first = read_experiences(self.engine, after=None, now=NOW).experiences
        self.assertEqual(first[0].evidence, Evidence("interest_event", before))  # never reflected: all

    def test_a_full_batch_stops_between_timestamps_and_the_rest_waits(self):
        start = NOW - timedelta(days=1)
        ids = [self.event(start + timedelta(minutes=n)) for n in range(MAX_EXPERIENCES - 1)]
        tied = [self.event(start + timedelta(hours=1)) for _ in range(3)]  # one transaction's time
        batch = read_experiences(self.engine, after=None, now=NOW)
        self.assertTrue(batch.more)
        self.assertEqual([item.id for item in batch.experiences], ids)
        self.assertEqual(batch.until, start + timedelta(minutes=MAX_EXPERIENCES - 2))
        rest = read_experiences(self.engine, after=batch.until, now=NOW)
        self.assertEqual(({item.id for item in rest.experiences}, rest.more), (set(tied), False))

    def test_one_timestamp_larger_than_a_batch_is_read_whole(self):
        at = NOW - timedelta(hours=1)
        ids = {self.event(at) for _ in range(MAX_EXPERIENCES + 1)}
        batch = read_experiences(self.engine, after=None, now=NOW)
        self.assertEqual(({item.id for item in batch.experiences}, batch.until, batch.more), (ids, at, True))


if __name__ == "__main__":
    unittest.main()
