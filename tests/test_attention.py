import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock

from subconscious.attention import FINDING_REQUEST, AttentionManager

MENTION = SimpleNamespace(finding="Private finding text")


class AttentionManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ready = MagicMock(return_value=False)
        self.closing = MagicMock(return_value=False)
        self.schedule = MagicMock(side_effect=lambda: SimpleNamespace(is_finished=False))
        self.send = AsyncMock()
        self.record_activity = MagicMock()
        self.finished = MagicMock()
        self.activity = MagicMock()
        self.claim = AsyncMock(return_value=MENTION)
        self.send_finding = AsyncMock()
        self.release = AsyncMock(return_value=True)
        self.manager = AttentionManager(
            can_deliver=self.ready, is_closing=self.closing,
            schedule_delivery=self.schedule, send_observation=self.send,
            record_activity=self.record_activity, delivery_finished=self.finished,
            write_activity=self.activity, claim_finding=self.claim,
            send_finding=self.send_finding, release_finding=self.release,
        )

    async def deliver_scheduled(self):
        await self.manager.deliver_next()
        self.manager.worker.is_finished = True

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

    async def test_closing_or_stop_rejects_work_and_retains_scheduled_item(self):
        for stop in (False, True):
            with self.subTest(stop=stop):
                self.setUp()
                self.ready.return_value = True
                self.manager.submit("Waiting")
                if stop:
                    self.manager.stop()  # even though Live remains ready
                else:
                    self.closing.return_value = True
                self.manager.submit("Too late")
                self.manager.submit_finding()
                await self.deliver_scheduled()
                self.manager.check()
                self.assertEqual(self.manager.stopped, stop)
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

    async def test_finding_waits_unclaimed_once_then_is_claimed_and_sent_at_delivery(self):
        self.manager.submit_finding()
        self.manager.submit_finding()
        self.assertEqual(list(self.manager.pending), [FINDING_REQUEST])
        self.claim.assert_not_called()
        self.record_activity.assert_not_called()  # waiting must not reset idle time

        self.ready.return_value = True
        self.manager.check()
        await self.deliver_scheduled()
        self.claim.assert_awaited_once()
        self.send_finding.assert_awaited_once_with(MENTION)
        self.release.assert_not_called()
        self.record_activity.assert_called_once()
        self.finished.assert_called_once()
        self.assertIn("Finding offered to Live.", str(self.activity.call_args_list))
        self.assertFalse(self.manager.pending)

    async def test_nothing_to_claim_sends_nothing_and_stays_quiet(self):
        self.claim.return_value = None
        self.ready.return_value = True
        self.manager.submit_finding()
        await self.deliver_scheduled()
        self.send_finding.assert_not_called()
        self.record_activity.assert_not_called()
        self.finished.assert_not_called()
        self.activity.assert_not_called()
        self.assertFalse(self.manager.busy)

    async def test_failed_send_releases_the_claim_without_logging_the_finding(self):
        self.send_finding.side_effect = OSError("private error details")
        self.ready.return_value = True
        self.manager.submit_finding()
        await self.deliver_scheduled()
        self.release.assert_awaited_once_with(MENTION)
        self.finished.assert_called_once()
        log = str(self.activity.call_args_list)
        self.assertIn("Finding send failed (OSError); it can be offered later.", log)
        self.assertNotIn("private", log.lower())
        self.assertNotIn("Private finding text", log)

    async def test_claim_is_released_if_live_became_busy_during_the_database_call(self):
        self.ready.return_value = True

        async def claim():
            self.ready.return_value = False
            return MENTION

        self.claim.side_effect = claim
        self.manager.submit_finding()
        await self.deliver_scheduled()
        self.send_finding.assert_not_called()
        self.release.assert_awaited_once_with(MENTION)
        self.finished.assert_not_called()
        self.assertIn("Live became busy", str(self.activity.call_args_list))

    async def test_claim_and_release_errors_report_only_their_type(self):
        self.ready.return_value = True
        self.claim.side_effect = OSError("private database details")
        self.manager.submit_finding()
        await self.deliver_scheduled()
        self.send_finding.assert_not_called()
        self.assertIn("Finding check failed (OSError).", str(self.activity.call_args_list))

        self.claim.side_effect = None
        self.send_finding.side_effect = TimeoutError()
        self.release.side_effect = OSError("private database details")
        self.manager.submit_finding()
        await self.deliver_scheduled()
        log = str(self.activity.call_args_list)
        self.assertIn("Finding send failed (TimeoutError); release failed (OSError).", log)
        self.assertNotIn("private", log)
