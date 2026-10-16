from dataclasses import replace
from datetime import timedelta
import asyncio
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from subconscious.interests.evolve_interests import EVOLVE_EVERY, evolve_interests, preview_interests, run_interest_cycle
from subconscious.interests.interest_proposals import Proposal, Reinforcement, Spawn
from subconscious.interests.interest_store import Evolution, InterestStore, MemoryBatch, NewMemory
import test_interest_proposals as proposal_tests
import test_keyboard_persistence as keyboard_tests

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
        patcher = patch("subconscious.interests.evolve_interests.propose_interests", return_value=Proposal((), None))
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
        self.assertIsNone(self.evolve())
        self.store.read_new_memories.assert_not_called()
        self.store.last_evolved_at.return_value = NOW - EVOLVE_EVERY
        self.assertTrue(self.evolve().startswith("Evolved"))

    def test_a_clock_moved_backwards_waits_instead_of_running(self):
        self.store.last_evolved_at.return_value = NOW + timedelta(days=1)
        self.assertIsNone(self.evolve())

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


class RunInterestCycleTests(unittest.TestCase):
    def setUp(self):
        self.engine = MagicMock()
        for name, value in (("build_engine", self.engine), ("OpenAI", MagicMock()),
                            ("evolve_interests", "Evolved from 3 memories: no changes.")):
            patcher = patch(f"subconscious.interests.evolve_interests.{name}", return_value=value)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    def test_cycle_uses_its_own_engine_and_client_and_disposes(self):
        self.assertEqual(run_interest_cycle(should_stop=lambda: False), "Evolved from 3 memories: no changes.")
        self.OpenAI.assert_called_once_with(max_retries=0)
        store, client = self.evolve_interests.call_args.args
        self.assertIs(store.engine, self.engine)
        self.assertIs(client, self.OpenAI.return_value.__enter__.return_value)
        self.engine.dispose.assert_called_once_with()

    def test_failures_report_only_the_error_type_and_dispose(self):
        self.evolve_interests.side_effect = RuntimeError("private memory text and API key")
        self.assertEqual(run_interest_cycle(should_stop=lambda: False),
                         "Interest cycle stopped (RuntimeError).")
        self.engine.dispose.assert_called_once_with()

    def test_stopping_skips_the_cycle_or_hides_its_outcome(self):
        self.assertIsNone(run_interest_cycle(should_stop=lambda: True))
        self.build_engine.assert_not_called()
        answers = iter((False, True))
        self.assertIsNone(run_interest_cycle(should_stop=lambda: next(answers)))
        self.engine.dispose.assert_called_once_with()


class AppInterestWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_checks_are_single_flight_and_stop_when_closing(self):
        async with keyboard_tests.running_app() as (app, pilot):
            running = MagicMock(is_finished=False)
            with patch.object(app, "evolve_interests", return_value=running) as start:
                app.check_interests()
                app.check_interests()
                start.assert_called_once_with()
                running.is_finished = True
                app.check_interests()
                self.assertEqual(start.call_count, 2)
                app.interest_worker = None
                app.closing = True
                app.check_interests()
                self.assertEqual(start.call_count, 2)
                app.closing = False

    async def test_worker_writes_each_new_outcome_once_off_the_ui_thread(self):
        async with keyboard_tests.running_app() as (app, pilot):
            outcomes = iter(("Interest cycle stopped (APIConnectionError).",) * 2 + (None, "No new memories."))
            with patch("app.run_interest_cycle", side_effect=lambda should_stop: next(outcomes)) as cycle:
                for _ in range(4):
                    app.check_interests()
                    await asyncio.wait_for(app.interest_worker.wait(), 5)
            self.assertEqual(app.interest_worker.group, "interests")
            self.assertFalse(cycle.call_args.kwargs["should_stop"]())
            await pilot.pause()
            entries = "\n".join(app.activity.entries)
            self.assertEqual(entries.count("Interest cycle stopped (APIConnectionError)."), 1)
            self.assertIn("No new memories.", entries)


if __name__ == "__main__":
    unittest.main()
