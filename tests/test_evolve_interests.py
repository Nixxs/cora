from dataclasses import replace
from datetime import timedelta
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from subconscious.evolve_interests import EVOLVE_EVERY, evolve_interests, preview_interests
from subconscious.interest_proposals import Proposal, Reinforcement, Spawn
from subconscious.interest_store import Evolution, InterestStore, MemoryBatch, NewMemory
import test_interest_proposals as proposal_tests

NOW = proposal_tests.NOW
CAR = proposal_tests.CAR
MEMORIES = (proposal_tests.WEDDING, proposal_tests.CARBY, NewMemory(uuid4(), "fact", "Nick sails", NOW))
CURSOR = (NOW, MEMORIES[-1].id)


def batch(memories=MEMORIES, *, waiting=False, more=False):
    return MemoryBatch(tuple(memories), None, CURSOR, waiting, more)


class EvolveInterestsTests(unittest.TestCase):
    def setUp(self):
        self.store = MagicMock(spec=InterestStore)
        self.store.last_evolved_at.return_value = None
        self.store.read_new_memories.return_value = batch()
        self.store.list_interests.return_value = [CAR]
        self.store.apply_evolution.return_value = Evolution((CAR,), None, 0)
        self.client = MagicMock()
        patcher = patch("subconscious.evolve_interests.propose_interests", return_value=Proposal((), None))
        self.propose = patcher.start()
        self.addCleanup(patcher.stop)

    def evolve(self):
        return evolve_interests(self.store, self.client, "chosen-model", now=NOW)

    def test_a_due_cycle_proposes_and_applies_atomically(self):
        self.assertEqual(self.evolve(), "Evolved from 3 memories: reinforced Old mechanical design.")
        self.store.list_interests.assert_called_once_with(include_dormant=True)
        self.propose.assert_called_once_with(self.client, "chosen-model", [CAR], MEMORIES, now=NOW)
        self.store.apply_evolution.assert_called_once_with(batch(), self.propose.return_value, now=NOW)
        self.store.advance_memory_cursor.assert_not_called()

    def test_runs_at_most_once_per_24_hours(self):
        self.store.last_evolved_at.return_value = NOW - EVOLVE_EVERY + timedelta(seconds=1)
        self.assertEqual(self.evolve(), "Interests already evolved in the last 24 hours.")
        self.store.read_new_memories.assert_not_called()
        self.store.last_evolved_at.return_value = NOW - EVOLVE_EVERY
        self.assertTrue(self.evolve().startswith("Evolved"))

    def test_a_clock_moved_backwards_waits_instead_of_running(self):
        self.store.last_evolved_at.return_value = NOW + timedelta(days=1)
        self.assertEqual(self.evolve(), "Interests already evolved in the last 24 hours.")

    def test_batches_without_new_memories_never_call_the_model(self):
        for advanced, waiting, outcome in (
            (True, False, "Passed over duplicate memories."),
            (False, True, "Waiting for memories to finish processing."),
            (False, False, "No new memories."),
        ):
            with self.subTest(outcome=outcome):
                self.store.read_new_memories.return_value = batch((), waiting=waiting)
                self.store.advance_memory_cursor.return_value = advanced
                self.assertEqual(self.evolve(), outcome)
        self.propose.assert_not_called()
        self.store.apply_evolution.assert_not_called()

    def test_waits_for_three_memories_unless_the_batch_was_full(self):
        self.store.read_new_memories.return_value = batch(MEMORIES[:2])
        self.assertEqual(self.evolve(), "Waiting for more memories (2 of 3).")
        self.propose.assert_not_called()
        self.store.advance_memory_cursor.assert_not_called()
        self.store.read_new_memories.return_value = batch(MEMORIES[:2], more=True)
        self.assertTrue(self.evolve().startswith("Evolved from 2 memories"))

    def test_outcome_reports_spawns_refusals_and_stale_cursors(self):
        spawned = replace(CAR, title="Wedding customs")
        self.store.apply_evolution.return_value = Evolution((), spawned, 2)
        self.assertEqual(self.evolve(), "Evolved from 3 memories: new interest Wedding customs; "
                                        "2 refused (live interests full).")
        self.store.apply_evolution.return_value = Evolution((), None, 0)
        self.assertEqual(self.evolve(), "Evolved from 3 memories: no changes.")
        self.store.apply_evolution.return_value = None
        self.assertEqual(self.evolve(), "Memory cursor moved during the cycle; nothing changed.")

    def test_model_errors_propagate_without_saving(self):
        self.propose.side_effect = ValueError("bad output")
        with self.assertRaises(ValueError):
            self.evolve()
        self.store.apply_evolution.assert_not_called()

    def test_preview_shows_the_proposal_and_saves_nothing(self):
        self.store.last_evolved_at.return_value = NOW
        self.propose.return_value = Proposal(
            (Reinforcement(CAR.id, 0.1, "Rebuilt the carburettor", (MEMORIES[1].id,)),),
            Spawn("Wedding customs", "How weddings differ", "A family wedding", "New", None, (MEMORIES[0].id,)),
        )
        self.assertEqual(preview_interests(self.store, self.client, "chosen-model", now=NOW), "\n".join((
            "Proposal from 3 memories (not saved):",
            "+0.10 Old mechanical design: Rebuilt the carburettor",
            "new: Wedding customs", "     How weddings differ", "     why: A family wedding",
        )))
        self.store.apply_evolution.assert_not_called()
        self.store.advance_memory_cursor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
