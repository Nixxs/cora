"""The person's conversation as the web page sees it: recent messages, then each new one as it's said.

Every message said in the conversation (at the desk, by voice, on Signal, by email or on the page)
is published here, numbered in order. A page that connects gets the recent ones and then each new
one; one that reconnects says the last number it saw (the Last-Event-ID that browsers send by
themselves) and gets only what it missed. Numbers start again when Cora restarts, so each run has
its own random epoch: a page from an earlier run starts afresh.
"""

import asyncio
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import secrets

# How many messages the feed keeps for pages that connect or catch up.
KEEP = 200
# What a page gets when it first opens.
FIRST_LOAD = 60
# A page this far behind is dropped (it reconnects and catches up from what's kept).
BACKLOG = 500


# What a feed item is: a message said, a request for the person's approval on the page, or the
# end of one (approved, declined or lapsed), so every page shows the request and its outcome.
MESSAGE, APPROVAL, APPROVAL_DONE = "message", "approval", "approval_done"


@dataclass(frozen=True)
class FeedMessage:
    seq: int
    role: str
    channel: str
    text: str
    at: datetime
    # Set for a message sent from a web page (and her reply to it): that page already shows it.
    ref: str | None = None
    kind: str = MESSAGE
    # An approval's details: {"id", "title", "detail", "seconds"}, or {"id", "outcome"} when done.
    data: dict | None = None
    # The paired device a web message came from ("iPhone"), else None.
    device: str | None = None


@dataclass(frozen=True)
class Status:
    """The desk's Live state now (active, standby, connecting…): sent as it changes, never kept."""

    state: str


class MessageFeed:
    def __init__(self, keep: int = KEEP) -> None:
        self.epoch = secrets.token_hex(4)
        self.items: deque[FeedMessage] = deque(maxlen=keep)
        self.seq = 0
        self.subscribers: set[asyncio.Queue] = set()
        self.seeded = False
        # The desk's Live state as last set, for pages that connect.
        self.status: str | None = None

    def publish(self, role: str, text: str, channel: str, at: datetime | None = None,
                ref: str | None = None, device: str | None = None) -> FeedMessage:
        return self._add(FeedMessage(self.seq + 1, role, channel, text, at or datetime.now(timezone.utc), ref,
                                     device=device))

    def publish_approval(self, kind: str, data: dict) -> FeedMessage:
        """An approval asked for on the page (APPROVAL), or its end (APPROVAL_DONE)."""
        return self._add(FeedMessage(self.seq + 1, "assistant", "web", "", datetime.now(timezone.utc),
                                     kind=kind, data=data))

    def _add(self, message: FeedMessage) -> FeedMessage:
        self.seq = message.seq
        self.items.append(message)
        self._send(message)
        return message

    def set_status(self, state: str) -> None:
        if state == self.status:
            return
        self.status = state
        self._send(Status(state))

    def _send(self, item) -> None:
        for queue in list(self.subscribers):
            if queue.qsize() >= BACKLOG:
                # Too far behind: dropped, and it catches up when it reconnects.
                self.subscribers.discard(queue)
                queue.put_nowait(None)
            else:
                queue.put_nowait(item)

    def seed(self, rows: list[tuple]) -> None:
        """Earlier messages from the database, (role, channel, text, at[, device]) oldest first, put
        before anything said since Cora started. Ones already here (said since) aren't repeated."""
        if self.seeded:
            return
        self.seeded = True
        since_start = list(self.items)
        said = {(item.role, item.text) for item in since_start if item.kind == MESSAGE}
        earlier = [row for row in rows if (row[0], row[2]) not in said]
        self.items.clear()
        self.seq = 0
        for role, channel, text, at, *device in earlier[-self.items.maxlen:]:
            self.seq += 1
            self.items.append(FeedMessage(self.seq, role, channel, text, at, device=device[0] if device else None))
        # Renumbered after the earlier ones. Nobody can have seen them yet: pages wait for seeding.
        for item in since_start:
            self.seq += 1
            self.items.append(replace(item, seq=self.seq))

    def event_id(self, message: FeedMessage) -> str:
        return f"{self.epoch}:{message.seq}"

    def since(self, last_event_id: str | None) -> tuple[bool, list[FeedMessage]]:
        """(fresh, messages): what a page that last saw last_event_id should get. fresh means it
        should clear what it shows first (a first load, or a page from an earlier run)."""
        epoch, _, seq = (last_event_id or "").partition(":")
        if epoch == self.epoch and seq.isdigit():
            seen = int(seq)
            oldest = self.items[0].seq if self.items else self.seq + 1
            # Missed more than is kept: start afresh rather than show a gap.
            if seen + 1 >= oldest or seen >= self.seq:
                return False, [item for item in self.items if item.seq > seen]
        return True, list(self.items)[-FIRST_LOAD:]

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.subscribers.discard(queue)
