from dataclasses import replace
from datetime import timedelta
import asyncio
import unittest
from unittest.mock import MagicMock, patch

from subconscious.desires import reflect_desires as reflect
from subconscious.desires.desire_rules import Change
from subconscious.identity.identity_evidence import ExperienceBatch
from subconscious.identity.identity_rules import Evidence
import test_keyboard_persistence as keyboard_tests
import test_propose_desires as proposal_tests

NOW = proposal_tests.NOW


class ReflectDesiresTests(unittest.TestCase):
    def setUp(self):
        self.held = proposal_tests.desire()
        self.faded = replace(proposal_tests.desire("candidate", strength=0.12), description="To learn the cello")
        self.store = MagicMock()
        self.store.last_reflected_at.return_value = None
        self.store.list_desires.return_value = [self.held, self.faded]
        self.found = [proposal_tests.experience() for _ in range(3)]
        self.form = Change("form", "Grew from forests", description="To see a redwood", motivation="I want to",
                           evidence=(Evidence("exploration", self.found[0].id),))
        self.reinforce = Change("reinforce", "Read more", desire_id=self.held.id, strength="some",
                                evidence=(Evidence("exploration", self.found[1].id),))
        self.store.apply_reflection.return_value = [None, "the candidate cap is full"]
        self.batch = ExperienceBatch(tuple(self.found), NOW - timedelta(minutes=5), False)
        self.enterContext(patch.object(reflect, "read_experiences", side_effect=lambda *a, **k: self.batch))
        self.enterContext(patch.object(reflect, "what_she_has", return_value=(["interest"], ["I like old machines."])))
        self.propose = self.enterContext(patch.object(
            reflect, "propose_desires", return_value=[self.reinforce, self.form]))
        self.client = MagicMock()

    def test_a_due_reflection_shows_only_live_desires_and_applies_changes(self):
        self.assertEqual(
            reflect.reflect_desires(self.store, self.client, "chosen-model", now=NOW),
            "Reflected on 3 experiences: reinforced (some): To walk through an old-growth forest; "
            "1 refused (the candidate cap is full); faded: To learn the cello.")
        self.propose.assert_called_once_with(self.client, "chosen-model", [self.held], ["interest"],
                                             ["I like old machines."], self.batch.experiences, now=NOW)
        self.store.apply_reflection.assert_called_once_with(
            [self.reinforce, self.form], read_after=None, read_until=self.batch.until, now=NOW)
        # What draws her now: a backlog is skipped, not worked through.
        reflect.read_experiences.assert_called_once_with(self.store.engine, after=None, now=NOW, newest=True)

    def test_waits_a_day_but_still_settles_when_there_is_too_little_to_reflect_on(self):
        self.store.last_reflected_at.return_value = NOW - timedelta(hours=23)
        self.assertIsNone(reflect.reflect_desires(self.store, self.client, now=NOW))
        self.store.settle_all.assert_not_called()
        self.store.last_reflected_at.return_value = NOW - timedelta(hours=24)
        for found, outcome in (((), "No new experiences; faded: To learn the cello"),
                               (self.found[:2], "Waiting for more experiences (2 of 3); faded: To learn the cello")):
            with self.subTest(outcome=outcome):
                self.batch = ExperienceBatch(tuple(found), NOW, False)
                self.assertEqual(reflect.reflect_desires(self.store, self.client, now=NOW), outcome)
        self.store.settle_all.assert_called_with(NOW)
        self.propose.assert_not_called()
        self.store.apply_reflection.assert_not_called()

    def test_a_reflection_that_lost_the_race_saves_nothing(self):
        self.store.apply_reflection.return_value = None
        self.assertEqual(reflect.reflect_desires(self.store, self.client, now=NOW),
                         "Another reflection ran first; nothing changed.")

    def test_preview_saves_nothing(self):
        self.store.last_reflected_at.return_value = NOW - timedelta(hours=1)  # previews ignore the daily wait
        lines = reflect.preview_desires(self.store, self.client, now=NOW).splitlines()
        self.assertEqual(lines[1:], [
            "reinforced (some): To walk through an old-growth forest", "     why: Read more",
            "formed a candidate: To see a redwood", "     why: Grew from forests", "     motivation: I want to",
        ])
        self.store.apply_reflection.assert_not_called()
        self.store.settle_all.assert_not_called()


class RunDesireCycleTests(unittest.TestCase):
    def setUp(self):
        self.engine = MagicMock()
        self.build_engine = self.enterContext(patch.object(reflect, "build_engine", return_value=self.engine))
        self.OpenAI = self.enterContext(patch.object(reflect, "OpenAI"))
        self.settings = self.enterContext(patch.object(reflect, "ModelSettingsStore"))
        self.settings.return_value.model_for.return_value = "subconscious-model"
        self.reflect = self.enterContext(patch.object(reflect, "reflect_desires", return_value="Reflected"))

    def test_cycle_uses_the_subconscious_model_its_own_engine_and_disposes(self):
        self.assertEqual(reflect.run_desire_cycle(should_stop=lambda: False), "Reflected")
        self.settings.return_value.model_for.assert_called_once_with("subconscious")
        self.assertEqual(self.reflect.call_args.kwargs["model"], "subconscious-model")
        self.engine.dispose.assert_called_once_with()

    def test_failures_report_only_the_error_type_and_stopping_hides_the_outcome(self):
        self.reflect.side_effect = ValueError("private conversation summary")
        self.assertEqual(reflect.run_desire_cycle(should_stop=lambda: False),
                         "Desire reflection stopped (ValueError).")
        self.assertIsNone(reflect.run_desire_cycle(should_stop=lambda: True))


class AppDesireWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_checks_are_single_flight_and_each_new_outcome_is_written_once(self):
        async with keyboard_tests.running_app() as (app, pilot):
            running = MagicMock(is_finished=False)
            with patch.object(app, "reflect_desires", return_value=running) as start:
                app.check_desires()
                app.check_desires()
                start.assert_called_once_with()
            app.desire_worker = None
            outcomes = iter(("Desire reflection stopped (APIConnectionError).",) * 2 + (
                "Reflected on 3 experiences: formed a candidate: To see a redwood.",))
            with patch("app_areas.background.run_desire_cycle", side_effect=lambda should_stop: next(outcomes)):
                for _ in range(3):
                    app.check_desires()
                    await asyncio.wait_for(app.desire_worker.wait(), 5)
            self.assertEqual(app.desire_worker.group, "desires")
            await pilot.pause()
            entries = "\n".join(app.activity.entries)
            self.assertEqual(entries.count("Desire reflection stopped (APIConnectionError)."), 1)
            self.assertIn("formed a candidate: To see a redwood.", entries)


if __name__ == "__main__":
    unittest.main()
