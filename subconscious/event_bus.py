"""Small synchronous event router; attention owns the waiting/delivery queue."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
from typing import Any, TypeVar
from uuid import UUID, uuid4


@dataclass(frozen=True)
class ObservationEvent:
    """Transport-independent contextual data, never instructions/tool permissions."""

    source: str
    content: str
    event_id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class FindingEvent:
    """One of Cora's findings may be worth raising; attention picks and claims it at delivery."""

    source: str
    event_id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


Event = TypeVar("Event")


class EventBus:
    """Route events by exact type to quick, synchronous handlers.

    All operations run on the app's event-loop thread. Background threads use
    CoraApp.call_from_thread(bus.publish, event). No persistence or replay.
    """

    def __init__(self, *, on_error: Callable[[Exception], None] | None = None):
        self.on_error = on_error
        self.closed = False
        self._subscribers: dict[type, dict[object, Callable[[Any], None]]] = {}

    def subscribe(self, event_type: type[Event], handler: Callable[[Event], None]) -> Callable[[], None]:
        if self.closed:
            raise RuntimeError("Event bus is closed")
        token = object()
        subscribers = self._subscribers.setdefault(event_type, {})
        subscribers[token] = handler

        def unsubscribe() -> None:
            subscribers.pop(token, None)

        return unsubscribe

    def publish(self, event: object) -> bool:
        """Return whether any handler ran successfully, not whether Live spoke."""
        if self.closed:
            return False
        delivered = False
        # Subscription changes affect the next publication, not this snapshot.
        for handler in tuple(self._subscribers.get(type(event), {}).values()):
            if self.closed:
                break
            try:
                handler(event)
            except Exception as error:
                # Isolate subscribers; never log event content or exception details.
                if self.on_error is not None:
                    self.on_error(error)
                else:
                    logging.getLogger(__name__).warning(
                        "Event subscriber failed (%s)", type(error).__name__,
                    )
            else:
                delivered = True
        return delivered

    def close(self) -> None:
        self.closed = True
        self._subscribers.clear()