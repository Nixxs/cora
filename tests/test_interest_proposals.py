from datetime import datetime, timezone
import unittest
from uuid import uuid4

from subconscious.interests.interest_proposals import Proposal, Reinforcement, Spawn, validate_proposal
from subconscious.interests.interest_store import Interest, NewMemory

NOW = datetime(2026, 10, 15, 9, tzinfo=timezone.utc)
CAR = Interest(uuid4(), "Old mechanical design", "Why older machines were built as they were",
               "Nick is restoring a car", 0.3, NOW, "active", None, NOW, None)
WEDDING = NewMemory(uuid4(), "event", "Nick's sister is getting married in March", NOW)
CARBY = NewMemory(uuid4(), "fact", "Nick rebuilt the carburettor himself", NOW)


def reinforcement(**changes):
    row = {"interest_id": str(CAR.id), "delta": 0.1, "reason": "Rebuilt the carburettor",
           "memory_ids": [str(CARBY.id)]}
    return row | changes


def spawn(**changes):
    row = {"title": "Wedding customs", "description": "How weddings differ between cultures",
           "why_emerged": "Nick's sister is getting married", "reason": "Upcoming family wedding",
           "parent_interest_id": None, "memory_ids": [str(WEDDING.id)]}
    return row | changes


def check(reinforce=(), new=None):
    return validate_proposal({"reinforce": list(reinforce), "spawn": new}, [CAR], [WEDDING, CARBY])


class ValidProposalTests(unittest.TestCase):
    def test_nothing_is_a_valid_proposal(self):
        self.assertEqual(check(), Proposal((), None))

    def test_reinforcement_and_spawn_are_parsed_and_cleaned(self):
        proposal = check([reinforcement(reason="  Rebuilt it  ", memory_ids=[str(CARBY.id)] * 2)], spawn(
            title=" Wedding customs ", parent_interest_id=str(CAR.id),
            memory_ids=[str(WEDDING.id).upper(), str(CARBY.id)],
        ))
        self.assertEqual(proposal.reinforcements, (Reinforcement(CAR.id, 0.1, "Rebuilt it", (CARBY.id,)),))
        self.assertEqual(proposal.spawn, Spawn(
            "Wedding customs", "How weddings differ between cultures", "Nick's sister is getting married",
            "Upcoming family wedding", CAR.id, (WEDDING.id, CARBY.id),
        ))


class InvalidProposalTests(unittest.TestCase):
    def assertRejected(self, payload):
        with self.assertRaises(ValueError):
            validate_proposal(payload, [CAR], [WEDDING, CARBY])

    def test_bad_shapes_are_rejected(self):
        for payload in (
            None, [], {"reinforce": []}, {"reinforce": [], "spawn": None, "extra": 1},
            {"reinforce": {}, "spawn": None}, {"reinforce": [[]], "spawn": None},
            {"reinforce": [reinforcement() | {"extra": 1}], "spawn": None},
            {"reinforce": [], "spawn": [spawn()]},
            {"reinforce": [], "spawn": {k: v for k, v in spawn().items() if k != "reason"}},
        ):
            with self.subTest(payload=payload):
                self.assertRejected(payload)

    def test_bad_reinforcements_are_rejected(self):
        for row in (
            reinforcement(interest_id=str(uuid4())), reinforcement(interest_id="not an id"),
            reinforcement(interest_id=None), reinforcement(delta=0), reinforcement(delta=0.2),
            reinforcement(delta=True), reinforcement(delta="0.1"), reinforcement(reason=" "),
            reinforcement(memory_ids=[]), reinforcement(memory_ids=str(CARBY.id)),
            reinforcement(memory_ids=[str(uuid4())]), reinforcement(memory_ids=[1]),
        ):
            with self.subTest(row=row):
                self.assertRejected({"reinforce": [row], "spawn": None})

    def test_an_interest_is_reinforced_at_most_once(self):
        self.assertRejected({"reinforce": [reinforcement(), reinforcement(delta=0.05)], "spawn": None})

    def test_bad_spawns_are_rejected(self):
        for row in (
            spawn(title=""), spawn(title="x" * 201), spawn(description=None), spawn(why_emerged=" "),
            spawn(title="  old   MECHANICAL design "), spawn(parent_interest_id=str(uuid4())),
            spawn(memory_ids=[]), spawn(memory_ids=[str(uuid4())]),
        ):
            with self.subTest(row=row):
                self.assertRejected({"reinforce": [], "spawn": row})


if __name__ == "__main__":
    unittest.main()
