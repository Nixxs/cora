import asyncio
import os
from threading import Event
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from app import CoraApp
from storage.conversation_store import ConversationStore
from storage.keyboard_persistence import KeyboardPersistence
import test_keyboard_persistence as keyboard_tests
from test_keyboard_persistence import running_app


class StartupInterruptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_check_runs_before_live_connection_and_logs_tag_count(self):
        connect = CoraApp.connect_to_cora
        async with running_app() as (app, pilot):
            app.persistence.mark_interrupted_conversations.return_value = 2
            app.live.run = AsyncMock()
            order = MagicMock()
            order.attach_mock(app.persistence.mark_interrupted_conversations, "check")
            order.attach_mock(app.live.run, "live")
            worker = connect(app)
            await asyncio.wait_for(worker.wait(), 3)
            self.assertEqual([entry[0] for entry in order.mock_calls], ["check", "live"])
            self.assertTrue(any("Tagged 2" in entry for entry in app.activity.entries))

    async def test_database_failure_is_redacted_and_does_not_stop_live_connection(self):
        connect = CoraApp.connect_to_cora
        async with running_app() as (app, pilot):
            app.persistence.mark_interrupted_conversations.side_effect = RuntimeError("private database details")
            app.live.run = AsyncMock()
            worker = connect(app)
            await asyncio.wait_for(worker.wait(), 3)
            app.live.run.assert_awaited_once()
            entries = "\n".join(app.activity.entries)
            self.assertIn("Interrupted conversation check failed (RuntimeError)", entries)
            self.assertNotIn("private database details", entries)

    async def test_shutdown_waits_for_startup_check_without_opening_live_connection(self):
        connect = CoraApp.connect_to_cora
        async with running_app() as (app, pilot):
            started = asyncio.Event()
            release = Event()
            loop = asyncio.get_running_loop()

            def slow_check():
                loop.call_soon_threadsafe(started.set)
                if not release.wait(timeout=5):
                    raise RuntimeError("Test did not release startup check")
                return 1

            app.persistence.mark_interrupted_conversations.side_effect = slow_check
            app.live.run = AsyncMock()
            worker = connect(app)
            try:
                await asyncio.wait_for(started.wait(), 2)
                await pilot.click("#activity-panel")
                with patch.object(app, "exit") as exit_app:
                    close = asyncio.create_task(app.action_disconnect_and_quit())
                    await asyncio.sleep(0)
                    app.persistence.finish_conversation.assert_not_called()
                    app.persistence.close.assert_not_called()
                    release.set()
                    await asyncio.wait_for(close, 3)
                    app.live.run.assert_not_called()
                    app.persistence.close.assert_called_once()
                    exit_app.assert_called_once()
            finally:
                release.set()
                if not worker.is_finished:
                    await asyncio.wait_for(worker.wait(), 3)


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class StartupInterruptionDatabaseTests(unittest.IsolatedAsyncioTestCase):
    # Reuse the existing disposable database setup, without inheriting its tests.
    setUpClass = classmethod(keyboard_tests.KeyboardDatabaseTests.setUpClass.__func__)

    async def test_real_app_startup_tags_previous_run_without_creating_an_empty_new_run(self):
        store = ConversationStore(self.engine)
        previous = KeyboardPersistence(store)
        previous.save_message(uuid4(), "Committed before the simulated crash")
        saved = store.get_messages(previous.conversation_id)[0]
        current = KeyboardPersistence(store)
        connect = CoraApp.connect_to_cora
        async with running_app(current) as (app, pilot):
            app.live.run = AsyncMock()
            worker = connect(app)
            await asyncio.wait_for(worker.wait(), 3)
            self.assertIsNone(store.get_conversation(current.conversation_id))
            with patch.object(app, "exit"):
                await app.action_disconnect_and_quit()
        result = store.get_conversation(previous.conversation_id)
        self.assertEqual(result.status, "interrupted")
        self.assertIsNone(result.ended_at)
        self.assertIsNone(store.get_conversation(current.conversation_id))
        message = store.get_messages(previous.conversation_id)[0]
        self.assertEqual((message.id, message.text, message.delivery_status),
                         (saved.id, saved.text, "pending"))


if __name__ == "__main__":
    unittest.main()