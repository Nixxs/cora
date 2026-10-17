"""The launch loads when Cora last talked with the person, for the opening greeting. Mocked APIs only."""

import asyncio
from datetime import datetime, timezone
import unittest
from unittest.mock import AsyncMock, MagicMock

from app import CoraApp
from storage.keyboard_persistence import KeyboardPersistence
import test_keyboard_persistence as keyboard_tests


CONNECT_TO_CORA = CoraApp.connect_to_cora
LAST = datetime(2026, 10, 16, 21, 5, tzinfo=timezone.utc)


class LastTalkedAdapterTests(unittest.TestCase):
    def test_adapter_excludes_this_run_and_creates_no_row(self):
        store = MagicMock()
        store.last_message_time.return_value = LAST
        persistence = KeyboardPersistence(store)
        self.assertEqual(persistence.load_last_talked_at(), LAST)
        store.last_message_time.assert_called_once_with(persistence.conversation_id)
        store.create_conversation.assert_not_called()


class LastTalkedLaunchTests(unittest.IsolatedAsyncioTestCase):
    async def connect_twice(self, app):
        app.persistence.mark_interrupted_conversations.return_value = 0
        app.live.run = AsyncMock()
        for _ in range(2):  # launch, then waking from standby
            app.connection_worker = CONNECT_TO_CORA(app)
            await asyncio.wait_for(app.connection_worker.wait(), 3)

    async def test_launch_loads_the_time_once(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.persistence.load_last_talked_at.return_value = LAST
            persistence = app.conversation_persistence
            self.assertFalse(persistence.last_talked_loaded)
            await self.connect_twice(app)
            app.persistence.load_last_talked_at.assert_called_once_with()
            self.assertEqual(persistence.last_talked_at, LAST)
            self.assertTrue(persistence.last_talked_loaded)

    async def test_never_talked_is_loaded_as_none(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.connect_twice(app)
            self.assertIsNone(app.conversation_persistence.last_talked_at)
            self.assertTrue(app.conversation_persistence.last_talked_loaded)

    async def test_failure_is_redacted_unknown_and_still_connects(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.persistence.load_last_talked_at.side_effect = RuntimeError("private SQL")
            await self.connect_twice(app)
            self.assertEqual(app.live.run.await_count, 2)
            self.assertIsNone(app.conversation_persistence.last_talked_at)
            self.assertFalse(app.conversation_persistence.last_talked_loaded)
            activity = "\n".join(app.activity.entries)
            self.assertIn("Last talk time load failed (RuntimeError); skipping the greeting.", activity)
            self.assertNotIn("private SQL", activity)


if __name__ == "__main__":
    unittest.main()
