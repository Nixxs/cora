"""The game as Cora knows it: when the state note is read, reading it, and passing it to Live. Mocked models."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from game.moments import NOTHING
from game.state import STATE_SECONDS, STATE_SOON_SECONDS, StateClock, StateReader
from game.watcher import Frame
from live import CoraLive
import test_keyboard_persistence as keyboard_tests


class ClockTests(unittest.TestCase):
    def test_read_at_first_then_about_once_a_minute(self):
        clock = StateClock()
        self.assertTrue(clock.due({"scene": "exploring"}, 0))
        clock.read(0)
        self.assertFalse(clock.due({"scene": "combat"}, STATE_SECONDS - 1))
        self.assertTrue(clock.due({"scene": "combat"}, STATE_SECONDS))

    def test_sooner_after_something_that_changes_it(self):
        clock = StateClock()
        clock.read(0)
        self.assertFalse(clock.due({"scene": "exploring", "new_area": 0.9}, 5))  # not that soon
        self.assertTrue(clock.due({"scene": "exploring", "new_area": 0.1}, STATE_SOON_SECONDS))  # remembered
        clock.read(STATE_SOON_SECONDS)
        self.assertFalse(clock.due({"scene": "exploring", "quest_moment": 0.5}, 2 * STATE_SOON_SECONDS))
        self.assertTrue(clock.due({"scene": "town", "level_up": 1.0}, 2 * STATE_SOON_SECONDS))

    def test_never_from_a_menu_or_loading_screen(self):
        clock = StateClock()
        self.assertFalse(clock.due({"scene": "menu"}, 0))
        self.assertTrue(clock.due({"scene": "town"}, 1))


class ReaderTests(unittest.TestCase):
    def test_the_first_note_and_nothing_when_unchanged(self):
        looker = MagicMock(side_effect=["Nick, level 6 druid,\n in Teldrassil (Dolanaar).", NOTHING + "."])
        reader = StateReader(looker=looker)
        self.assertEqual(reader.read(b"jpeg", None), "Nick, level 6 druid, in Teldrassil (Dolanaar).")
        self.assertEqual(looker.call_args.args, ("Write the note.", b"jpeg"))
        self.assertIn("untrusted", looker.call_args.kwargs["instructions"])
        self.assertIsNone(reader.read(b"jpeg", "Nick, level 6 druid, in Teldrassil (Dolanaar)."))
        self.assertIn("The last note was: Nick, level 6 druid", looker.call_args.args[0])

    def test_a_long_note_is_cut(self):
        reader = StateReader(looker=MagicMock(return_value="x " * 1000))
        self.assertLessEqual(len(reader.read(b"jpeg", None)), 600)


class LiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_note_is_silent_context(self):
        live = CoraLive.__new__(CoraLive)
        live.connection = MagicMock()
        live.connection.session.instructions.append = AsyncMock()
        live.ready = True
        live.context = MagicMock()
        self.assertTrue(await live.note_game_state("Nick, level 6 druid, in Teldrassil."))
        content = live.connection.session.instructions.append.await_args.kwargs["content"]
        self.assertIn("Silent context", content)
        self.assertIn("say nothing now", content)
        self.assertIn("This game is new to you", content)
        self.assertIn("not a guide", content)
        # The longest state still fits Live's limit whole, with its instructions.
        await live.note_game_state("x" * 600)
        longest = live.connection.session.instructions.append.await_args.kwargs["content"]
        self.assertTrue(longest.endswith('x"'), len(longest))
        live.connection.session.commentary.append = AsyncMock()
        live.on_trace = None
        for kind in ("new_area", "dead"):
            await live.send_game_moment("y" * 500, kind)
            moment = live.connection.session.commentary.append.await_args.kwargs["content"]
            self.assertTrue(moment.endswith('y"'), (kind, len(moment)))
        self.assertIn("replaces any earlier game note", content)
        self.assertIn("Nick, level 6 druid, in Teldrassil.", content)
        live.ready = False
        self.assertFalse(await live.note_game_state("anything"))


class AppTests(unittest.IsolatedAsyncioTestCase):
    def frame(self, **answers):
        return Frame({"scene": "exploring", **answers}, 1200, 0.3, b"jpeg")

    async def test_the_state_goes_to_live_when_changed_and_again_to_a_new_session(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.game_state_reader = MagicMock()
            app.game_state_reader.read.side_effect = ["In Dolanaar.", None, "In Dolanaar."]
            first, second = object(), object()
            app.live.connection = first
            with patch.object(app, "can_deliver_observation", return_value=True), \
                    patch.object(app.live, "note_game_state", AsyncMock(return_value=True)) as note, \
                    patch("app_areas.game_watch.LOG_PATH", MagicMock()), \
                    patch("app_areas.game_watch.monotonic", side_effect=lambda: self.now):
                for self.now, connection in ((1000.0, first), (1060.0, first), (1120.0, second)):
                    app.live.connection = connection
                    app.game_frame_seen(self.frame())
                    await pilot.pause()
                    await asyncio.wait_for(app.workers.wait_for_complete(
                        [worker for worker in app.workers if worker.group == "game-state"]), 3)
            previous = [call.args[1] for call in app.game_state_reader.read.call_args_list]
            self.assertEqual(previous, [None, "In Dolanaar.", None])  # a new session isn't told what changed
            self.assertEqual(note.await_count, 2)
            self.assertEqual(app.game_state_connection, second)
            self.assertIn("Game state: In Dolanaar.", "\n".join(app.activity.entries))

    async def test_not_read_while_shes_not_active(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.game_state_reader = MagicMock()
            with patch.object(app, "can_deliver_observation", return_value=False), \
                    patch("app_areas.game_watch.LOG_PATH", MagicMock()):
                app.game_frame_seen(self.frame())
                await pilot.pause()
            app.game_state_reader.read.assert_not_called()
            self.assertIsNone(app.game_state_clock.last_read)  # read as soon as she's back


if __name__ == "__main__":
    unittest.main()
