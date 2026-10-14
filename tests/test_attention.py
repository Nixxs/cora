import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock

from subconscious.attention import AttentionManager


class AttentionManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ready = MagicMock(return_value=False)
        self.closing = MagicMock(return_value=False)
        self.schedule = MagicMock(side_effect=lambda: SimpleNamespace(is_finished=False))
        self.send = AsyncMock()
        self.record_activity = MagicMock()
        self.finished = MagicMock()
        self.activity = MagicMock()
        self.manager = AttentionManager(
            can_deliver=self.ready, is_closing=self.closing,
            schedule_delivery=self.schedule, send_observation=self.send,
            record_activity=self.record_activity, delivery_finished=self.finished,
            write_activity=self.activity,
        )

    async def deliver_scheduled(self):
        await self.manager.deliver_next()
        self.manager.worker.is_finished = True

    async def test_waiting_observations_deliver_fifo_when_ready(self):
        for text in ("First", "Second", "Third"):
            self.manager.submit(text)
        self.schedule.assert_not_called()
        self.assertEqual(list(self.manager.pending), ["First", "Second", "Third"])

        self.ready.return_value = True
        for text in ("First", "Second", "Third"):
            self.manager.check()
            await self.deliver_scheduled()
            self.send.assert_awaited_with(text)
        self.assertFalse(self.manager.pending)
        self.assertEqual(self.send.await_count, 3)
        self.assertEqual(self.finished.call_count, 3)

    async def test_pending_worker_is_scheduled_only_once(self):
        self.ready.return_value = True
        self.manager.submit("First")
        worker = self.manager.worker
        self.manager.submit("Second")
        self.manager.check()
        self.schedule.assert_called_once()
        self.assertIs(self.manager.worker, worker)
        self.assertTrue(self.manager.busy)
        await self.deliver_scheduled()
        self.assertFalse(self.manager.busy)
        self.assertEqual(list(self.manager.pending), ["Second"])

    async def test_readiness_recheck_keeps_item_if_live_became_busy(self):
        self.ready.return_value = True
        self.manager.submit("Keep me")
        self.ready.return_value = False
        await self.deliver_scheduled()
        self.send.assert_not_called()
        self.finished.assert_not_called()
        self.assertEqual(list(self.manager.pending), ["Keep me"])

    async def test_inflight_send_prevents_concurrent_delivery(self):
        self.manager.submit("First")
        self.manager.submit("Second")
        self.ready.return_value = True
        started = asyncio.Event()
        finish = asyncio.Event()

        async def send(text):
            started.set()
            await finish.wait()

        self.send.side_effect = send
        task = asyncio.create_task(self.manager.deliver_next())
        try:
            await asyncio.wait_for(started.wait(), 3)
            self.assertTrue(self.manager.busy)
            self.manager.check()
            await self.manager.deliver_next()
            self.schedule.assert_not_called()
            self.send.assert_awaited_once_with("First")
            self.assertEqual(list(self.manager.pending), ["Second"])
        finally:
            finish.set()
            await task
        self.assertFalse(self.manager.busy)

    async def test_failed_attempt_is_not_retried_and_does_not_block_next_item(self):
        self.manager.submit("Private observation")
        self.manager.submit("Second")
        self.send.side_effect = [OSError("private error details"), None]
        self.ready.return_value = True
        self.manager.check()
        await self.deliver_scheduled()
        self.assertEqual(list(self.manager.pending), ["Second"])
        self.assertNotIn("Private observation", str(self.activity.call_args_list))
        self.assertNotIn("private error details", str(self.activity.call_args_list))
        self.assertIn("OSError", str(self.activity.call_args_list))

        self.manager.check()
        await self.deliver_scheduled()
        self.manager.check()
        self.assertEqual(self.send.await_count, 2)
        self.assertFalse(self.manager.pending)
        self.assertEqual(self.finished.call_count, 2)

    async def test_closing_rejects_submissions_and_retains_scheduled_item(self):
        self.ready.return_value = True
        self.manager.submit("Waiting")
        self.closing.return_value = True
        self.manager.submit("Too late")
        await self.deliver_scheduled()
        self.manager.check()
        self.assertEqual(list(self.manager.pending), ["Waiting"])
        self.send.assert_not_called()
        self.schedule.assert_called_once()

    async def test_stop_rejects_work_even_if_live_remains_ready(self):
        self.ready.return_value = True
        self.manager.submit("Waiting")
        self.manager.stop()
        self.manager.submit("Too late")
        await self.deliver_scheduled()
        self.manager.check()
        self.assertTrue(self.manager.stopped)
        self.assertEqual(list(self.manager.pending), ["Waiting"])
        self.send.assert_not_called()
        self.schedule.assert_called_once()

    async def test_cancellation_releases_sender_without_replaying_attempted_item(self):
        self.manager.submit("First")
        self.manager.submit("Second")
        self.ready.return_value = True
        started = asyncio.Event()

        async def send(text):
            started.set()
            await asyncio.Event().wait()

        self.send.side_effect = send
        task = asyncio.create_task(self.manager.deliver_next())
        try:
            await asyncio.wait_for(started.wait(), 3)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(self.manager.busy)
        self.assertEqual(list(self.manager.pending), ["Second"])
        self.finished.assert_called_once()