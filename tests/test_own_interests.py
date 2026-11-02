"""Interests of her own: tangents and pull from exploring, sparks weighed once, and the own-interest cap.

Rules are checked without a database; the store tests are opt-in, on disposable PostgreSQL.
"""

from datetime import timedelta
import os
import unittest
from unittest.mock import MagicMock
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from storage.models import InterestRecord
from subconscious.interests.exploration_store import ExplorationStore, clean_tangents
from subconscious.interests.interest_proposals import Proposal, Spawn
from subconscious.interests.interest_store import MemoryBatch, Spark
from subconscious.interests.interest_strength import (
    MAX_LIVE_INTERESTS, MAX_OWN_LIVE_INTERESTS, PULL_CEILING, has_room, pulled, reinforce,
)
import test_exploration_store as exploration_tests
import test_interest_store as interest_tests

NOW = interest_tests.NOW
FINDING = exploration_tests.FINDING
SWIFTS = ("Swift migration", "They can stay airborne for ten months.")
LICHEN = ("Lichen as a partnership", "Two organisms living as one.")


class OwnInterestRuleTests(unittest.TestCase):
    def test_her_pull_adds_a_little_and_stops_at_the_ceiling(self):
        self.assertEqual(pulled(0.3, "none"), 0.3)
        self.assertAlmostEqual(pulled(0.3, "some"), reinforce(0.3, 0.03))
        self.assertAlmostEqual(pulled(0.3, "strong"), reinforce(0.3, 0.06))
        self.assertEqual(pulled(0.59, "strong"), PULL_CEILING)
        # Above the ceiling (from the person's own conversations) it never pulls down.
        self.assertEqual(pulled(0.8, "strong"), 0.8)
        with self.assertRaises(ValueError):
            pulled(0.3, "huge")

    def test_room_for_her_own_interests_is_capped_within_the_whole(self):
        self.assertTrue(has_room(MAX_LIVE_INTERESTS - 1))
        self.assertFalse(has_room(MAX_LIVE_INTERESTS))
        self.assertTrue(has_room(3, MAX_OWN_LIVE_INTERESTS - 1))
        self.assertFalse(has_room(3, MAX_OWN_LIVE_INTERESTS))
        self.assertFalse(has_room(MAX_LIVE_INTERESTS, 0))
        self.assertLess(MAX_OWN_LIVE_INTERESTS, MAX_LIVE_INTERESTS)

    def test_tangents_are_cleaned_deduplicated_and_capped(self):
        self.assertEqual(clean_tangents([(" Swift migration ", " On the wing "), ("swift MIGRATION", "again")]),
                         [{"title": "Swift migration", "why": "On the wing"}])
        for tangents in ([SWIFTS, LICHEN, ("Third", "x")], [(" ", "x")], [("x" * 201, "x")], [("Title", "")]):
            with self.subTest(tangents=tangents), self.assertRaises(ValueError):
                clean_tangents(tangents)

    def test_weighted_choice_among_explored_interests(self):
        rng = MagicMock()
        rng.choices.side_effect = lambda population, weights: [population[-1]]
        store = ExplorationStore(MagicMock(), rng=rng)
        self.assertIs(store.rng, rng)


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class OwnInterestDatabaseTests(unittest.TestCase):
    setUpClass = classmethod(interest_tests.summary_tests.SummaryDatabaseTests.setUpClass.__func__)

    def setUp(self):
        exploration_tests.ExplorationStoreDatabaseTests.setUp(self)

    def explore(self, now=NOW, tangents=(SWIFTS,), pull="some"):
        started = self.explorations.start_next(now=now)
        self.assertEqual(started.reason, "started")
        return self.explorations.finish(started.exploration.id, *FINDING, now=now, tangents=tangents, pull=pull)

    def no_memories(self):
        return MemoryBatch((), None, None, False)

    def own_spawn(self, title, sparks):
        return Proposal((), Spawn(title, "Something she came across", "Noticed while exploring", "Caught her eye",
                                  None, (), tuple(spark.exploration_id for spark in sparks)))

    def test_tangents_and_pull_are_kept_and_offered_once_as_sparks(self):
        found = self.explore()
        self.assertEqual((found.tangents, found.pull), ((SWIFTS,), "some"))
        sparks = self.interests.read_sparks(now=NOW)
        self.assertEqual(sparks, (Spark(found.id, *SWIFTS, self.interest.title),))
        evolution = self.interests.apply_evolution(
            self.no_memories(), self.own_spawn("Swift migration", sparks), now=NOW, sparks=sparks)
        self.assertEqual((evolution.spawned.title, evolution.spawned.origin), ("Swift migration", "own"))
        # Weighed once: not offered again, and the same sparks can't be used twice.
        self.assertEqual(self.interests.read_sparks(now=NOW), ())
        self.assertIsNone(self.interests.apply_evolution(
            self.no_memories(), self.own_spawn("Again", sparks), now=NOW, sparks=sparks))
        self.assertEqual(self.interests.last_evolved_at(), NOW)

    def test_sparks_are_weighed_even_when_nothing_comes_of_them_and_go_stale(self):
        self.explore()
        sparks = self.interests.read_sparks(now=NOW)
        self.assertIsNotNone(self.interests.apply_evolution(self.no_memories(), Proposal((), None), now=NOW, sparks=sparks))
        self.assertEqual(self.interests.read_sparks(now=NOW), ())
        self.explore(NOW + timedelta(days=1), tangents=(LICHEN,))
        self.assertEqual(len(self.interests.read_sparks(now=NOW + timedelta(days=14))), 1)
        self.assertEqual(self.interests.read_sparks(now=NOW + timedelta(days=16)), ())

    def test_her_own_interests_are_capped_while_the_persons_still_grow(self):
        for n in range(MAX_OWN_LIVE_INTERESTS):
            self.assertIsNotNone(self.interests.spawn(*interest_tests.spawn_args(f"Own {n}"), now=NOW, origin="own"))
        self.assertIsNone(self.interests.spawn(*interest_tests.spawn_args("One too many"), now=NOW, origin="own"))
        # The person's interests still have room (one already exists from setUp).
        self.assertIsNotNone(self.interests.spawn(*interest_tests.spawn_args("From Nick"), now=NOW))
        with Session(self.engine) as session:
            origins = sorted(session.scalars(select(InterestRecord.origin)))
        self.assertEqual(origins.count("own"), MAX_OWN_LIVE_INTERESTS)

    def test_her_pull_strengthens_a_live_interest_only_up_to_the_ceiling(self):
        before = self.interest.strength
        after = self.interests.pull(self.interest.id, "strong", now=NOW)
        self.assertAlmostEqual(after.strength, pulled(before, "strong"))
        self.assertIsNone(self.interests.pull(self.interest.id, "none", now=NOW))
        for _ in range(40):
            self.interests.pull(self.interest.id, "strong", now=NOW)
        self.assertAlmostEqual(self.interests.list_interests()[0].strength, PULL_CEILING)
        # One that has faded is never revived by her pull alone. Decay counts at most 14 days per
        # settlement, so it takes two to fade from the ceiling.
        self.interests.settle_all(NOW + timedelta(days=14))
        faded = NOW + timedelta(days=28)
        self.interests.settle_all(faded)
        self.assertEqual(self.interests.list_interests(include_dormant=True)[0].status, "dormant")
        self.assertIsNone(self.interests.pull(self.interest.id, "strong", now=faded))
        with self.assertRaises(LookupError):
            self.interests.pull(uuid4(), "some", now=NOW)


if __name__ == "__main__":
    unittest.main()
