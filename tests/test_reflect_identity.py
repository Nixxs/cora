from datetime import timedelta
import asyncio
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from subconscious.identity import reflect_identity as reflect
from subconscious.identity.identity_evidence import ExperienceBatch
from subconscious.identity.identity_rules import Change, Evidence, Trait
import test_identity_proposals as proposal_tests
import test_keyboard_persistence as keyboard_tests

NOW = proposal_tests.AT + timedelta(days=2)


class ReflectIdentityTests(unittest.TestCase):
    def setUp(self):
        self.held = Trait(uuid4(), "perspective", "I like seeing how things work.", None, None, 0.6, "active")
        self.store = MagicMock()
        self.store.last_reflected_at.return_value = None
        self.store.last_value_change_at.return_value = None
        self.store.list_traits.return_value = [self.held]
        self.found = [proposal_tests.experience() for _ in range(3)]
        self.form = Change("form", "Kept noticing it", kind="tendency", statement="I explain by example.",
                           confidence=0.3, evidence=(Evidence("exploration", self.found[0].id),))
        self.revise = Change("revise", "Saw it again", trait_id=self.held.id,
                             statement="I like seeing how things work.", confidence=0.9,
                             evidence=(Evidence("exploration", self.found[1].id),))
        self.store.apply_reflection.return_value = [None, "Confidence moves at most 0.2 per revision"]
        self.batch = ExperienceBatch(tuple(self.found), proposal_tests.AT, False)
        self.enterContext(patch.object(reflect, "read_experiences", side_effect=lambda *a, **k: self.batch))
        self.propose = self.enterContext(patch.object(
            reflect, "propose_identity", return_value=[self.form, self.revise]))
        self.client = MagicMock()

    def test_a_due_reflection_applies_changes_and_stamps_how_far_it_read(self):
        self.assertEqual(reflect.reflect_identity(self.store, self.client, "chosen-model", now=NOW),
                         "Reflected on 3 experiences: formed tendency at 0.3: I explain by example.; "
                         "1 refused by the rules.")
        reflect.read_experiences.assert_called_once_with(self.store.engine, after=None, now=NOW)
        self.propose.assert_called_once_with(self.client, "chosen-model", [self.held], self.batch.experiences)
        self.store.apply_reflection.assert_called_once_with(
            [self.form, self.revise], read_after=None, read_until=proposal_tests.AT, now=NOW)

    def test_waits_a_day_and_for_enough_experiences(self):
        self.store.last_reflected_at.return_value = NOW - timedelta(hours=23)
        self.assertIsNone(reflect.reflect_identity(self.store, self.client, now=NOW))
        reflect.read_experiences.assert_not_called()
        self.store.last_reflected_at.return_value = NOW - timedelta(hours=24)
        for found, more, outcome in (
            ((), False, "No new experiences."),
            (self.found[:2], False, "Waiting for more experiences (2 of 3)."),
        ):
            with self.subTest(outcome=outcome):
                self.batch = ExperienceBatch(tuple(found), NOW, more)
                self.assertEqual(reflect.reflect_identity(self.store, self.client, now=NOW), outcome)
        self.propose.assert_not_called()
        self.store.apply_reflection.assert_not_called()
        # A full batch with more waiting does not wait for three.
        self.batch = ExperienceBatch(tuple(self.found[:1]), NOW, True)
        self.store.apply_reflection.return_value = [None, None]
        self.assertIn("revised perspective 0.6 -> 0.9", reflect.reflect_identity(self.store, self.client, now=NOW))
        self.assertEqual(self.store.apply_reflection.call_args.kwargs["read_after"], NOW - timedelta(hours=24))

    def test_a_reflection_that_lost_the_race_saves_nothing(self):
        self.store.apply_reflection.return_value = None
        self.assertEqual(reflect.reflect_identity(self.store, self.client, now=NOW),
                         "Another reflection ran first; nothing changed.")

    def test_preview_shows_what_the_rules_would_refuse_without_saving(self):
        self.store.last_reflected_at.return_value = NOW - timedelta(hours=1)  # previews ignore the daily wait
        self.assertEqual(reflect.preview_identity(self.store, self.client, now=NOW).splitlines(), [
            "Proposal from 3 experiences (not saved):",
            "formed tendency at 0.3: I explain by example.",
            "     why: Kept noticing it",
            "     rules: allowed",
            "revised perspective 0.6 -> 0.9: I like seeing how things work.",
            "     why: Saw it again",
            "     rules: Confidence moves at most 0.2 per revision",
        ])
        self.store.apply_reflection.assert_not_called()
        self.propose.return_value = []
        self.assertEqual(reflect.preview_identity(self.store, self.client, now=NOW).splitlines()[1], "no changes")
        self.batch = ExperienceBatch((), NOW, False)
        self.assertEqual(reflect.preview_identity(self.store, self.client, now=NOW), "No new experiences.")


class RunIdentityCycleTests(unittest.TestCase):
    def setUp(self):
        self.engine = MagicMock()
        self.build_engine = self.enterContext(patch.object(reflect, "build_engine", return_value=self.engine))
        self.OpenAI = self.enterContext(patch.object(reflect, "OpenAI"))
        self.reflect = self.enterContext(patch.object(
            reflect, "reflect_identity", return_value="Reflected on 3 experiences: no changes."))

    def test_cycle_uses_its_own_engine_and_client_and_disposes(self):
        self.assertEqual(reflect.run_identity_cycle(should_stop=lambda: False),
                         "Reflected on 3 experiences: no changes.")
        self.OpenAI.assert_called_once_with(max_retries=0)
        store, client = self.reflect.call_args.args
        self.assertIs(store.engine, self.engine)
        self.assertIs(client, self.OpenAI.return_value.__enter__.return_value)
        self.assertIsNotNone(self.reflect.call_args.kwargs["now"].tzinfo)
        self.engine.dispose.assert_called_once_with()

    def test_failures_report_only_the_error_type_and_dispose(self):
        self.reflect.side_effect = ValueError("private conversation summary")
        self.assertEqual(reflect.run_identity_cycle(should_stop=lambda: False),
                         "Identity reflection stopped (ValueError).")
        self.engine.dispose.assert_called_once_with()

    def test_stopping_skips_the_cycle_or_hides_its_outcome(self):
        self.assertIsNone(reflect.run_identity_cycle(should_stop=lambda: True))
        self.build_engine.assert_not_called()
        answers = iter((False, True))
        self.assertIsNone(reflect.run_identity_cycle(should_stop=lambda: next(answers)))
        self.engine.dispose.assert_called_once_with()


class AppIdentityWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_checks_are_single_flight_and_stop_when_closing(self):
        async with keyboard_tests.running_app() as (app, pilot):
            running = MagicMock(is_finished=False)
            with patch.object(app, "reflect_identity", return_value=running) as start:
                app.check_identity()
                app.check_identity()
                start.assert_called_once_with()
                running.is_finished = True
                app.check_identity()
                self.assertEqual(start.call_count, 2)
                app.identity_worker = None
                app.closing = True
                app.check_identity()
                self.assertEqual(start.call_count, 2)
                app.closing = False

    async def test_worker_writes_each_new_outcome_once_under_identity(self):
        async with keyboard_tests.running_app() as (app, pilot):
            outcomes = iter(("Identity reflection stopped (APIConnectionError).",) * 2 + (
                None, "Reflected on 3 experiences: formed tendency at 0.3: I explain by example."))
            with patch("app.run_identity_cycle", side_effect=lambda should_stop: next(outcomes)) as cycle:
                for _ in range(4):
                    app.check_identity()
                    await asyncio.wait_for(app.identity_worker.wait(), 5)
            self.assertEqual(app.identity_worker.group, "identity")
            self.assertFalse(cycle.call_args.kwargs["should_stop"]())
            await pilot.pause()
            entries = "\n".join(app.activity.entries)
            self.assertEqual(entries.count("Identity reflection stopped (APIConnectionError)."), 1)
            self.assertIn("formed tendency at 0.3: I explain by example.", entries)


if __name__ == "__main__":
    unittest.main()
