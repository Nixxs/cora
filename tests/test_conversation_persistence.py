"""Coordinator boundaries: serialized storage and exceptional teardown."""
import asyncio
from threading import Event
import unittest
from unittest.mock import MagicMock

from storage.conversation_persistence import ConversationPersistence


class ConversationPersistenceTests(unittest.IsolatedAsyncioTestCase):
    def coordinator(self):
        return ConversationPersistence(
            MagicMock(), schedule_save=MagicMock(), is_closing=lambda: False,
            set_memory_context=MagicMock(), write_activity=MagicMock(), notify=MagicMock(),
        )

    async def test_database_operations_are_serialized_off_thread(self):
        coordinator = self.coordinator()
        started, release = Event(), Event()
        order = []

        def first():
            started.set()
            if not release.wait(3):
                raise TimeoutError("Test did not release storage")
            order.append("first")

        task = asyncio.create_task(coordinator.call_database(first))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            second = asyncio.create_task(coordinator.call_database(order.append, "second"))
            await asyncio.sleep(0)
            self.assertEqual(order, [])
            release.set()
            await asyncio.gather(task, second)
            self.assertEqual(order, ["first", "second"])
        finally:
            release.set()
            await task

    async def test_flush_failure_still_disposes_storage(self):
        coordinator = self.coordinator()
        flush = MagicMock(side_effect=RuntimeError("flush failed"))
        with self.assertRaisesRegex(RuntimeError, "flush failed"):
            await coordinator.finish(flush)
        coordinator.storage.finish_conversation.assert_not_called()
        coordinator.storage.close.assert_called_once_with()

    async def test_final_flush_can_schedule_save_before_recording_closes(self):
        coordinator = self.coordinator()
        worker = MagicMock()
        from unittest.mock import AsyncMock
        worker.wait = AsyncMock()
        coordinator.schedule_save.return_value = worker
        await coordinator.finish(lambda: coordinator.record_chat_message("assistant", "final", "voice"))
        worker.wait.assert_awaited_once()
        coordinator.record_chat_message("assistant", "late", "voice")
        coordinator.schedule_save.assert_called_once()
        coordinator.storage.finish_conversation.assert_called_once_with()
        coordinator.storage.close.assert_called_once_with()
