"""Hanging up after goodbye on calls that aren't the person's, without waiting on the model."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app_areas.contact_calls import note_for_goodbye
from phone import goodbye
from phone.goodbye import GoodbyeWatch, ends_in_farewell
import test_keyboard_persistence as keyboard_tests


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class FarewellTests(unittest.TestCase):
    def test_farewells_at_the_end_of_a_turn(self):
        for text in ("Thanks so much, bye!", "Goodbye, Karene.", "Okay, take care.", "Have a lovely day!",
                     "Great, talk soon.", "Bye-bye now", "Thanks, see you"):
            self.assertTrue(ends_in_farewell(text), text)

    def test_not_a_farewell(self):
        for text in ("Before I say goodbye, " + "could you tell me what time works best for you on Tuesday?",
                     "What time suits you?", "I'll pass that on.", ""):
            self.assertFalse(ends_in_farewell(text), text)


class GoodbyeWatchTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.watch = GoodbyeWatch(clock=self.clock)

    def later(self, seconds):
        self.clock.now += seconds

    def test_hangs_up_once_her_goodbye_has_played_and_the_line_is_quiet(self):
        self.watch.heard("user", "That's all, thanks.")
        self.watch.heard("assistant", "No worries, have a great day. Bye!")
        self.assertFalse(self.watch.due(playing=True))
        self.later(2)
        self.assertFalse(self.watch.due(playing=True))
        self.later(goodbye.QUIET_SECONDS - 0.5)
        self.assertFalse(self.watch.due(playing=False))
        self.later(1)
        self.assertTrue(self.watch.due(playing=False))
        self.assertFalse(self.watch.due(playing=False))  # only once

    def test_a_farewell_back_still_hangs_up(self):
        self.watch.heard("assistant", "Thanks, bye!")
        self.watch.heard("user", "Bye!")
        self.later(goodbye.QUIET_SECONDS + 1)
        self.assertTrue(self.watch.due(playing=False))

    def test_anything_more_from_them_keeps_the_call_open(self):
        self.watch.heard("assistant", "Thanks, bye!")
        self.watch.heard("user", "Oh wait, one more thing about Tuesday.")
        self.later(goodbye.QUIET_SECONDS + 1)
        self.assertFalse(self.watch.due(playing=False))

    def test_carrying_on_after_bye_keeps_the_call_open(self):
        self.watch.heard("assistant", "Bye! Oh, actually, one more question: do you open on weekends?")
        self.later(goodbye.QUIET_SECONDS + 1)
        self.assertFalse(self.watch.due(playing=False))

    def test_what_she_adds_after_goodbyes_both_ways_doesnt_keep_it_open(self):
        # A real call: she said "have a good rest of your day", they said bye, and she carried on.
        self.watch.heard("assistant", "Thanks for helping us test. Have a good rest of your day.")
        self.watch.heard("user", "Okay Thank you. Bye")
        self.watch.heard("assistant", "Bye! Yep. All right. I'll pass that on and hang up.")
        self.later(goodbye.QUIET_SECONDS + 1)
        self.assertTrue(self.watch.due(playing=False))

    def test_their_bye_first_then_hers_with_more(self):
        self.watch.heard("assistant", "Tuesday at nine then.")
        self.watch.heard("user", "Great, bye!")
        self.watch.heard("assistant", "Bye, thanks so much, I'll let Nick know.")
        self.later(goodbye.QUIET_SECONDS + 1)
        self.assertTrue(self.watch.due(playing=False))

    def test_more_from_them_after_goodbyes_reopens_it(self):
        self.watch.heard("assistant", "Have a good day.")
        self.watch.heard("user", "Bye")
        self.watch.heard("assistant", "Bye! I'll pass that on.")
        self.watch.heard("user", "Sorry, actually, can you also ask him about Friday?")
        self.later(goodbye.QUIET_SECONDS + 1)
        self.assertFalse(self.watch.due(playing=False))
        self.watch.heard("assistant", "Sure, I'll ask him about Friday.")
        self.later(goodbye.QUIET_SECONDS + 1)
        self.assertFalse(self.watch.due(playing=False))

    def test_no_goodbye_no_hang_up(self):
        self.watch.heard("assistant", "What time suits you?")
        self.later(60)
        self.assertFalse(self.watch.due(playing=False))

    def test_her_backend_still_working_holds_the_call(self):
        self.watch.heard("assistant", "Thanks, bye!")
        self.watch.delegation(True)  # passing on what she learned
        self.later(goodbye.QUIET_SECONDS + 1)
        self.assertFalse(self.watch.due(playing=False))
        self.watch.delegation(False)
        self.assertFalse(self.watch.due(playing=False))
        self.later(goodbye.QUIET_SECONDS + 1)
        self.assertTrue(self.watch.due(playing=False))

    def test_backend_work_that_hangs_doesnt_hold_it_forever(self):
        self.watch.heard("assistant", "Thanks, bye!")
        self.watch.delegation(True)
        self.later(goodbye.DELEGATION_MAX_SECONDS + 1)
        self.assertTrue(self.watch.due(playing=False))

    def test_session_events_feed_the_watch(self):
        note_for_goodbye(self.watch, SimpleNamespace(type="session.output_transcript.delta", delta="Bye!"))
        note_for_goodbye(self.watch, SimpleNamespace(type="session.delegation.created"))
        self.assertIsNotNone(self.watch.delegating_since)
        note_for_goodbye(self.watch, SimpleNamespace(type="response.event", event={"type": "response.completed"}))
        self.assertIsNone(self.watch.delegating_since)
        self.later(goodbye.QUIET_SECONDS + 1)
        self.assertTrue(self.watch.due(playing=False))


class WatchGoodbyeAppTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_call_closes_when_the_watch_says_so(self):
        async with keyboard_tests.running_app() as (app, pilot):
            live = SimpleNamespace(close=AsyncMock())
            relay = SimpleNamespace(is_playback_busy=lambda: False)
            watch = SimpleNamespace(due=lambda playing: True)
            with patch("app_areas.contact_calls.GOODBYE_CHECK_SECONDS", 0):
                await asyncio.wait_for(app.watch_goodbye(live, relay, watch, "Ace Plumbing"), 3)
            live.close.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
