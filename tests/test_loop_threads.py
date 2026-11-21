"""Enough loop threads for every timer, so the dashboard's animations keep their pace on Windows
(each Textual timer holds a default-executor thread while it sleeps there)."""

import asyncio
import unittest

from app import LOOP_THREADS
import test_keyboard_persistence as keyboard_tests

# Room left for asyncio.to_thread's work alongside every timer.
HEADROOM = 32


class LoopThreadTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_loop_has_threads_for_every_timer_with_the_dashboard_open(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await pilot.click("#view-button")
            await pilot.pause()
            executor = asyncio.get_running_loop()._default_executor
            self.assertEqual(executor._max_workers, LOOP_THREADS)
            nodes = [app, *app.screen_stack, *app.query("*"), *app.screen.query("*")]
            timers = {timer for node in nodes for timer in getattr(node, "_timers", ())}
            self.assertLessEqual(len(timers) + HEADROOM, LOOP_THREADS)


if __name__ == "__main__":
    unittest.main()
