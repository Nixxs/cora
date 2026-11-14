"""Watching the game through the Decisions API (Settings > Game), recording only. Mocked API and screen."""

import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image
from textual.widgets import Switch

from app_areas import game_watch
from app_areas.game_watch import frame_line, notable
from game.watcher import MODEL, QUESTIONS, Frame, GameWatcher, read_answers
from storage.app_settings_store import AppSettingsStore
from ui.model_settings import ModelSettingsScreen
import test_keyboard_persistence as keyboard_tests
from test_model_settings_screen import MODELS


def response(tokens=1200, **answers):
    parts = []
    for name, value in answers.items():
        if value is None:
            parts.append(SimpleNamespace(type="refusal", name=name))
        elif isinstance(value, str):
            parts.append(SimpleNamespace(type="choice", name=name, choice=value))
        elif name in ("health", "worth_mentioning"):
            parts.append(SimpleNamespace(type="score", name=name, score=value))
        else:
            parts.append(SimpleNamespace(type="predicate", name=name, probability=value))
    return SimpleNamespace(answers=parts, usage=SimpleNamespace(input_tokens=tokens))


class AnswerTests(unittest.TestCase):
    def test_each_kind_of_answer_is_read_and_refusals_are_none(self):
        answers = read_answers(response(scene="combat", health=2.5, close_call=0.1, dead=None))
        self.assertEqual(answers, {"scene": "combat", "health": 2.5, "close_call": 0.1, "dead": None})

    def test_the_questions_have_unique_names_and_the_needed_fields(self):
        names = [question["name"] for question in QUESTIONS]
        self.assertEqual(len(names), len(set(names)))
        for question in QUESTIONS:
            self.assertTrue(question["instructions"])
            if question["type"] == "choice":
                self.assertGreaterEqual(len(question["choices"]), 2)
            if question["type"] == "score":
                self.assertTrue(2 <= len(question["levels"]) <= 10)

    def test_a_frame_in_a_few_words_and_what_is_notable(self):
        quiet = Frame({"scene": "exploring", "health": 3.0, "close_call": 0.05, "worth_mentioning": 0.4}, 1200, 0.3)
        self.assertEqual(frame_line(quiet), "exploring · health full (3.0)")
        self.assertFalse(notable(quiet, "exploring"))
        self.assertTrue(notable(quiet, "combat"))
        close = Frame({"scene": "combat", "health": 0.3, "close_call": 0.82, "worth_mentioning": 2.6}, 1200, 0.3)
        self.assertEqual(frame_line(close), "combat · health critical (0.3) · worth mentioning 2.6 · close_call 0.82")
        self.assertTrue(notable(close, "combat"))
        self.assertAlmostEqual(close.dollars, 0.00012)


class WatcherTests(unittest.IsolatedAsyncioTestCase):
    def make(self, in_front=True, colour=(0, 0, 0)):
        self.screen = Image.new("RGB", (2560, 1440), colour)
        self.client = MagicMock()
        self.client.decisions.create.return_value = response(scene="combat")
        self.frames, self.errors = [], []
        return GameWatcher(self.frames.append, self.errors.append, in_front=lambda: in_front,
                           grab=lambda: self.screen, client=self.client)

    async def test_nothing_is_sent_unless_the_game_is_in_front(self):
        watcher = self.make(in_front=False)
        await watcher.look()
        self.client.decisions.create.assert_not_called()

    async def test_a_frame_is_asked_about_and_an_unchanged_one_is_skipped(self):
        watcher = self.make()
        await watcher.look()
        options = self.client.decisions.create.call_args.kwargs
        self.assertEqual(options["model"], MODEL)
        self.assertEqual(options["questions"], QUESTIONS)
        image = options["input"][0]["content"][1]
        self.assertEqual(image["type"], "input_image")
        self.assertTrue(image["image_url"].startswith("data:image/jpeg;base64,"))
        self.assertEqual([frame.answers for frame in self.frames], [{"scene": "combat"}])
        await watcher.look()
        self.assertEqual(self.client.decisions.create.call_count, 1)
        self.assertEqual(watcher.totals.skipped, 1)
        self.screen = Image.new("RGB", (2560, 1440), (200, 50, 50))
        await watcher.look()
        self.assertEqual(self.client.decisions.create.call_count, 2)
        self.assertEqual(watcher.totals.input_tokens, 2400)
        self.assertIn("2 frames sent (1 unchanged skipped, 0 failed)", watcher.totals.summary())

    async def test_an_unchanged_frame_is_sent_anyway_after_a_while(self):
        watcher = self.make()
        await watcher.look()
        watcher.last_sent -= 31
        await watcher.look()
        self.assertEqual(self.client.decisions.create.call_count, 2)

    async def test_a_failed_frame_is_counted_and_reported(self):
        watcher = self.make()
        self.client.decisions.create.side_effect = TimeoutError()
        await watcher.look()
        self.assertEqual(watcher.totals.failed, 1)
        self.assertIsInstance(self.errors[0], TimeoutError)
        self.assertEqual(self.frames, [])


class StoreTests(unittest.TestCase):
    def test_watching_is_off_unless_switched_on(self):
        store = AppSettingsStore(MagicMock())
        for saved, expected in ((None, False), ("off", False), ("on", True)):
            with self.subTest(saved=saved), patch.object(store, "_get", return_value=saved):
                self.assertIs(store.game_watch(), expected)
        with patch.object(store, "_set") as saved:
            store.set_game_watch(True)
            saved.assert_called_once_with("game_watch", "on")


async def settle(app):
    """Wait for every worker but the watcher, which runs until stopped. (An empty list would make
    Textual wait for all of them.)"""
    workers = [worker for worker in app.workers if worker.group != "game-watch" and not worker.is_cancelled]
    if workers:
        await asyncio.wait_for(app.workers.wait_for_complete(workers), 3)


class AppTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.mkdtemp()
        self.log = Path(folder) / "game-watch.log"
        self.enterContext(patch.object(game_watch, "LOG_PATH", self.log))

    async def test_frames_are_logged_and_notable_ones_shown_in_activity(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.game_frame_seen(Frame({"scene": "exploring", "close_call": 0.02}, 1200, 0.25))
            app.game_frame_seen(Frame({"scene": "exploring", "close_call": 0.03}, 1200, 0.25))
            app.game_frame_seen(Frame({"scene": "combat", "close_call": 0.9}, 1300, 0.25))
            lines = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([line["answers"]["scene"] for line in lines], ["exploring", "exploring", "combat"])
            self.assertEqual(lines[2]["input_tokens"], 1300)
            shown = [entry.split("  ", 1)[1] for entry in app.activity.entries if "  Game: " in entry]
            self.assertEqual(shown, ["Game: exploring", "Game: combat · close_call 0.90"])

    async def test_saved_on_starts_it_and_the_switch_stops_it(self):
        async with keyboard_tests.running_app() as (app, pilot):
            async def forever(watcher):
                await asyncio.sleep(3600)

            with patch.object(GameWatcher, "run", forever):
                app.apply_game_settings({"watch": True})
                await pilot.pause()
                self.assertIsNotNone(app.game_watch_worker)
                app.persistence.load_models.return_value = MODELS
                await pilot.click("#settings-button")
                await settle(app)
                await pilot.pause()
                self.assertIsInstance(app.screen, ModelSettingsScreen)
                switch = app.screen.query_one("#game-watch", Switch)
                self.assertTrue(switch.value)
                switch.value = False
                await pilot.pause()
                await settle(app)
                self.assertIsNone(app.game_watch_worker)
                app.persistence.save_game_watch.assert_called_once_with(False)

    async def test_nothing_is_looked_at_in_standby(self):
        async with keyboard_tests.running_app() as (app, pilot):
            async def forever(watcher):
                await asyncio.sleep(3600)

            with patch.object(GameWatcher, "run", forever), patch.object(game_watch, "game_in_front", return_value=True):
                app.apply_game_settings({"watch": True})
                await pilot.pause()
                watcher = app.game_screen_watcher
                app.standby = False
                self.assertTrue(watcher.in_front())
                app.standby = True
                self.assertFalse(watcher.in_front())
                app.stop_game_watch()

    async def test_unset_settings_dont_start_it(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.apply_game_settings(MagicMock())
            self.assertIsNone(app.game_watch_worker)


if __name__ == "__main__":
    unittest.main()
