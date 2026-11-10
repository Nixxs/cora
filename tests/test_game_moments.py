"""Moments in the game worth Cora reacting to: spotting, describing and delivering them. Mocked models."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from textual.widgets import Select

from game.moments import LOOK_AROUND, NOTHING, TRIGGERS, MomentDescriber, MomentSpotter
from game.watcher import Frame
from storage.app_settings_store import AppSettingsStore
from subconscious.attention import AttentionManager, GameMoment
from subconscious.event_bus import GameMomentEvent
from ui.model_settings import ModelSettingsScreen
import test_keyboard_persistence as keyboard_tests
from test_model_settings_screen import MODELS


class SpotterTests(unittest.TestCase):
    def test_a_moment_fires_once_and_again_only_after_dropping_back(self):
        spotter = MomentSpotter("chatty")
        self.assertEqual(spotter.feed({"surprising": 0.8}, 0), "surprising")
        self.assertIsNone(spotter.feed({"surprising": 0.9}, 200))  # still the same moment
        self.assertIsNone(spotter.feed({"surprising": 0.2}, 210))  # dropped back: armed again
        self.assertEqual(spotter.feed({"surprising": 0.8}, 220), "surprising")

    def test_its_own_cooldown_holds_even_when_re_armed(self):
        spotter = MomentSpotter("chatty")
        spotter.feed({"close_call": 0.9}, 0)
        spotter.feed({"close_call": 0.1}, 10)
        self.assertIsNone(spotter.feed({"close_call": 0.9}, 30))
        self.assertEqual(spotter.feed({"close_call": 0.9}, 61), "close_call")

    def test_chattiness_spaces_reactions_but_not_the_big_moments(self):
        spotter = MomentSpotter("normal")
        self.assertEqual(spotter.feed({"quest_moment": 0.95}, 0), "quest_moment")
        self.assertIsNone(spotter.feed({"surprising": 0.9}, 10))
        self.assertEqual(spotter.feed({"level_up": 1.0}, 12), "level_up")
        # The surprise stayed armed, so it goes once the gap has passed, if it lasts.
        self.assertEqual(spotter.feed({"surprising": 0.9}, 73), "surprising")

    def test_the_most_important_wins_and_off_never_fires(self):
        self.assertEqual(MomentSpotter("chatty").feed({"quest_moment": 1.0, "dead": 0.9}, 0), "dead")
        self.assertIsNone(MomentSpotter("off").feed({"dead": 1.0}, 0))

    def test_chat_fires_again_after_its_cooldown_while_still_on_screen(self):
        spotter = MomentSpotter("chatty")
        self.assertEqual(spotter.feed({"chat_worth_reacting": 0.9}, 0), "chat_worth_reacting")
        self.assertIsNone(spotter.feed({"chat_worth_reacting": 0.9}, 30))
        self.assertEqual(spotter.feed({"chat_worth_reacting": 0.9}, 61), "chat_worth_reacting")
        self.assertTrue(TRIGGERS["chat_worth_reacting"].repeats_while_high)

    def test_refused_or_missing_answers_are_ignored(self):
        self.assertIsNone(MomentSpotter("chatty").feed({"dead": None, "scene": "combat"}, 0))

    def test_a_new_area_is_spaced_like_other_moments(self):
        spotter = MomentSpotter("normal")
        self.assertEqual(spotter.feed({"new_area": 0.9}, 0), "new_area")
        self.assertIsNone(spotter.feed({"new_area": 0.9}, 5))  # the banner is still up
        self.assertFalse(TRIGGERS["new_area"].urgent)
        self.assertGreater(TRIGGERS["new_area"].lasts, TRIGGERS["close_call"].lasts)

    def test_she_looks_around_when_its_been_calm_and_quiet_a_while(self):
        spotter = MomentSpotter("normal")
        calm = {"scene": "exploring", "surprising": 0.1}
        self.assertIsNone(spotter.feed(calm, 0))
        self.assertIsNone(spotter.feed(calm, 299))
        self.assertEqual(spotter.feed(calm, 300), LOOK_AROUND)
        self.assertIsNone(spotter.feed(calm, 400))  # the quiet starts again from her look
        self.assertEqual(spotter.feed(calm, 600), LOOK_AROUND)

    def test_no_looking_around_mid_fight_soon_after_a_reaction_or_when_off(self):
        spotter = MomentSpotter("chatty")
        spotter.feed({"scene": "exploring"}, 0)
        self.assertIsNone(spotter.feed({"scene": "combat"}, 500))
        spotter.feed({"scene": "exploring", "surprising": 0.9}, 501)
        self.assertIsNone(spotter.feed({"scene": "town", "surprising": 0.1}, 600))
        self.assertEqual(spotter.feed({"scene": "town"}, 681), LOOK_AROUND)
        off = MomentSpotter("off")
        off.feed({"scene": "town"}, 0)
        self.assertIsNone(off.feed({"scene": "town"}, 5000))


class DescriberTests(unittest.TestCase):
    def test_it_is_told_what_was_mentioned_and_nothing_means_none(self):
        looker = MagicMock(side_effect=["Two Webwood Spiders at once, and Nick's on low health.", NOTHING + "."])
        describer = MomentDescriber(looker=looker)
        self.assertEqual(describer.describe("close_call", b"jpeg"), "Two Webwood Spiders at once, and Nick's on low health.")
        self.assertIsNone(describer.describe("close_call", b"jpeg"))
        question = looker.call_args.args[0]
        self.assertIn("close to dying", question)
        self.assertIn("Two Webwood Spiders", question)
        self.assertEqual(looker.call_args.args[1], b"jpeg")
        self.assertIn("untrusted", looker.call_args.kwargs["instructions"])

    def test_place_moments_ask_for_lore_and_suggestions(self):
        looker = MagicMock(return_value="Shadowthread Cave: the Gnarlpine were driven out by these spiders.")
        describer = MomentDescriber(looker=looker)
        describer.describe("new_area", b"jpeg")
        self.assertIn("lore", looker.call_args.args[0])
        describer.describe(LOOK_AROUND, b"jpeg")
        question = looker.call_args.args[0]
        self.assertIn("fishing", question)
        self.assertIn("Shadowthread Cave", question)  # already mentioned


class AttentionTests(unittest.IsolatedAsyncioTestCase):
    def manager(self, now=100.0):
        self.now = now
        self.sent = AsyncMock()
        self.record = MagicMock()
        self.notes = []
        manager = AttentionManager(
            can_deliver=lambda: True, is_closing=lambda: False, schedule_delivery=MagicMock(),
            send_observation=AsyncMock(), record_activity=self.record, delivery_finished=MagicMock(),
            write_activity=lambda message, summary: self.notes.append(message),
            claim_finding=AsyncMock(), send_finding=AsyncMock(), release_finding=AsyncMock(),
            send_game_moment=self.sent, clock=lambda: self.now,
        )
        return manager

    async def test_the_newest_moment_replaces_one_waiting_and_isnt_user_activity(self):
        manager = self.manager()
        manager.submit_game_moment(GameMoment("old", 120))
        manager.submit_game_moment(GameMoment("new", 120, "new_area"))
        self.assertEqual(list(manager.pending), [GameMoment("new", 120, "new_area")])
        await manager.deliver_next()
        self.sent.assert_awaited_once_with("new", "new_area")
        self.record.assert_not_called()

    async def test_a_stale_moment_is_dropped(self):
        manager = self.manager()
        manager.submit_game_moment(GameMoment("too late", 99))
        await manager.deliver_next()
        self.sent.assert_not_awaited()
        self.assertIn("Game moment dropped: too late to react to.", self.notes)


class StoreTests(unittest.TestCase):
    def test_chattiness_is_normal_unless_chosen(self):
        store = AppSettingsStore(MagicMock())
        for saved, expected in ((None, "normal"), ("quiet", "quiet"), ("loud", "normal")):
            with self.subTest(saved=saved), patch.object(store, "_get", return_value=saved):
                self.assertEqual(store.game_chattiness(), expected)
        with self.assertRaises(ValueError):
            store.set_game_chattiness("loud")


class AppTests(unittest.IsolatedAsyncioTestCase):
    def frame(self, **answers):
        return Frame({"scene": "combat", **answers}, 1200, 0.3, b"jpeg")

    async def test_a_moment_is_described_and_offered_to_her(self):
        async with keyboard_tests.running_app() as (app, pilot):
            published = []
            app.event_bus.subscribe(GameMomentEvent, published.append)
            app.game_describer = MagicMock()
            app.game_describer.describe.return_value = "Nick just hit level 5!"
            with patch.object(app, "can_deliver_observation", return_value=True), \
                    patch("app_areas.game_watch.LOG_PATH", MagicMock()):
                app.game_frame_seen(self.frame(level_up=1.0))
                await pilot.pause()
                await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            app.game_describer.describe.assert_called_once_with("level_up", b"jpeg")
            self.assertEqual([(event.kind, event.description) for event in published],
                             [("level_up", "Nick just hit level 5!")])
            self.assertIn("Game moment (level_up): Nick just hit level 5!", "\n".join(app.activity.entries))

    async def test_nothing_is_described_while_shes_not_active(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.game_describer = MagicMock()
            with patch.object(app, "can_deliver_observation", return_value=False), \
                    patch("app_areas.game_watch.LOG_PATH", MagicMock()):
                app.game_frame_seen(self.frame(dead=1.0))
                await pilot.pause()
            app.game_describer.describe.assert_not_called()
            self.assertIn("not offered: Cora isn't active", "\n".join(app.activity.entries))

    async def test_a_repeat_is_not_offered(self):
        async with keyboard_tests.running_app() as (app, pilot):
            published = []
            app.event_bus.subscribe(GameMomentEvent, published.append)
            app.game_describer = MagicMock()
            app.game_describer.describe.return_value = None
            with patch.object(app, "can_deliver_observation", return_value=True), \
                    patch("app_areas.game_watch.LOG_PATH", MagicMock()):
                app.game_frame_seen(self.frame(chat_worth_reacting=0.9))
                await pilot.pause()
                await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            self.assertEqual(published, [])
            self.assertIn("nothing new on a closer look", "\n".join(app.activity.entries))

    async def test_chattiness_is_loaded_and_chosen_in_settings(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.apply_game_settings({"chattiness": "quiet"})
            self.assertEqual((app.game_chattiness, app.game_spotter.chattiness), ("quiet", "quiet"))
            app.persistence.load_models.return_value = MODELS
            await pilot.click("#settings-button")
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            await pilot.pause()
            self.assertIsInstance(app.screen, ModelSettingsScreen)
            app.screen.query_one("#game-chattiness", Select).value = "off"
            await pilot.pause()
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            self.assertEqual(app.game_spotter.chattiness, "off")
            app.persistence.save_game_chattiness.assert_called_once_with("off")


if __name__ == "__main__":
    unittest.main()
