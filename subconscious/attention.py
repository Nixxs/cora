"""First attention policy: retain observations and deliver FIFO when Live is quiet."""

import asyncio
from collections import deque


class AttentionManager:
    """Queue/delivery coordination independent of Live, Textual, and event transport.

    Call from Cora's event-loop thread. Background threads must marshal submissions
    to that thread; this is an in-memory component, not a cross-process event bus.
    """

    def __init__(
        self, *, can_deliver, is_closing, schedule_delivery, send_observation,
        record_activity, delivery_finished, write_activity,
    ):
        self.can_deliver = can_deliver
        self.is_closing = is_closing
        self.schedule_delivery = schedule_delivery
        self.send_observation = send_observation
        self.record_activity = record_activity
        self.delivery_finished = delivery_finished
        self.write_activity = write_activity
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
        observation = self.pending.popleft()
        self.sending = True
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
        finally:
            self.sending = False
            self.delivery_finished()

    def stop(self) -> None:
        """Reject new work; the app waits for an already-running bounded send."""
        self.stopped = True