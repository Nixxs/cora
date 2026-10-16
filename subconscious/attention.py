"""First attention policy: retain observations and deliver FIFO when Live is quiet."""

import asyncio
from collections import deque

# Stands in the queue for "consider raising one of Cora's findings". The finding is
# chosen and marked as mentioned only at delivery, so nothing is claimed while waiting.
FINDING_REQUEST = object()


class AttentionManager:
    """Queue/delivery coordination independent of Live, Textual, and event transport.

    Call from Cora's event-loop thread. Background threads must marshal submissions
    to that thread; this is an in-memory component, not a cross-process event bus.
    """

    def __init__(
        self, *, can_deliver, is_closing, schedule_delivery, send_observation,
        record_activity, delivery_finished, write_activity,
        claim_finding, send_finding, release_finding,
    ):
        self.can_deliver = can_deliver
        self.is_closing = is_closing
        self.schedule_delivery = schedule_delivery
        self.send_observation = send_observation
        self.record_activity = record_activity
        self.delivery_finished = delivery_finished
        self.write_activity = write_activity
        self.claim_finding = claim_finding
        self.send_finding = send_finding
        self.release_finding = release_finding
        self.pending = deque()
        self.worker = None
        self.sending = False
        self.stopped = False

    @property
    def busy(self) -> bool:
        return self.sending or (self.worker is not None and not self.worker.is_finished)

    def submit(self, observation: str) -> None:
        if self.stopped or self.is_closing():
            return
        self.pending.append(observation)
        self.write_activity(
            f"Observation queued ({len(self.pending)} waiting).", "Awareness queued",
        )
        self.record_activity()
        self.check()

    def submit_finding(self) -> None:
        # Not user activity: it must not reset the idle time it is waiting for.
        if self.stopped or self.is_closing() or FINDING_REQUEST in self.pending:
            return
        self.pending.append(FINDING_REQUEST)
        self.check()

    def check(self) -> None:
        if self.stopped or self.is_closing() or not self.pending or self.busy:
            return
        if self.can_deliver():
            self.worker = self.schedule_delivery()

    async def deliver_next(self) -> None:
        # Readiness may change after scheduling; don't pop until it is safe to send.
        if (
            self.stopped or self.is_closing() or self.sending
            or not self.pending or not self.can_deliver()
        ):
            return
        item = self.pending.popleft()
        self.sending = True
        attempted = True
        try:
            if item is FINDING_REQUEST:
                attempted = await self._offer_finding()
            else:
                await self._send_observation(item)
        finally:
            self.sending = False
            if attempted:
                self.delivery_finished()

    async def _send_observation(self, observation: str) -> None:
        try:
            self.record_activity()
            await asyncio.wait_for(self.send_observation(observation), timeout=5)
        except Exception as error:
            # Delivery may be partial. Never retry an attempted item automatically.
            self.write_activity(
                f"Observation send failed ({type(error).__name__}); not retried.",
                "Awareness send failed",
            )
        else:
            self.write_activity("Observation sent to Live.", "Awareness sent")

    async def _offer_finding(self) -> bool:
        """Claim a finding only now that Live is quiet; return whether one was sent."""
        try:
            mention = await self.claim_finding()
        except Exception as error:
            self.write_activity(f"Finding check failed ({type(error).__name__}).", "Finding failed")
            return False
        if mention is None:
            return False
        # The database call takes time; Live may no longer be free.
        if self.stopped or self.is_closing() or not self.can_deliver():
            await self._release_finding(mention, "Finding not sent (Live became busy)")
            return False
        try:
            self.record_activity()
            await asyncio.wait_for(self.send_finding(mention), timeout=5)
        except Exception as error:
            # Unlike observations, release it: a rare partial send may repeat, but a
            # failed one is not lost.
            await self._release_finding(mention, f"Finding send failed ({type(error).__name__})")
        else:
            self.write_activity("Finding offered to Live.", "Finding offered")
        return True

    async def _release_finding(self, mention, reason: str) -> None:
        try:
            await self.release_finding(mention)
        except Exception as error:
            self.write_activity(f"{reason}; release failed ({type(error).__name__}).", "Finding failed")
        else:
            self.write_activity(f"{reason}; it can be offered later.", "Finding deferred")

    def stop(self) -> None:
        """Reject new work; the app waits for an already-running bounded send."""
        self.stopped = True