"""Listens for messages to Cora's Signal account, reconnecting when the stream drops."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json

from .setup import error_text

# Waits between reconnects after the stream drops, longest last and then repeated.
RETRY_SECONDS = (2, 5, 15, 30, 60)


@dataclass(frozen=True)
class IncomingMessage:
    sender: str
    text: str
    sent_at: datetime
    attachments: int = 0


def parse_event(data: str) -> IncomingMessage | None:
    """A direct text message from the daemon's event, or None for anything else
    (receipts, typing, reactions, groups, her own messages from her phone)."""
    try:
        event = json.loads(data)
    except json.JSONDecodeError:
        return None
    if not isinstance(event, dict):
        return None
    event = event.get("params", event)
    envelope = event.get("envelope") or {}
    message = envelope.get("dataMessage")
    sender = envelope.get("sourceNumber")
    if not isinstance(message, dict) or not sender:
        return None
    if message.get("groupInfo") or message.get("groupV2"):
        return None  # group chats are not handled
    text = (message.get("message") or "").strip()
    attachments = len(message.get("attachments") or [])
    if not text and not attachments:
        return None
    milliseconds = envelope.get("timestamp") or message.get("timestamp")
    sent_at = (
        datetime.fromtimestamp(milliseconds / 1000, timezone.utc) if isinstance(milliseconds, int)
        else datetime.now(timezone.utc)
    )
    return IncomingMessage(sender, text, sent_at, attachments)


class SignalReceiver:
    def __init__(self, client, account: str, on_message, write_activity,
                 retry_seconds: tuple[float, ...] = RETRY_SECONDS) -> None:
        self.client = client
        self.account = account
        self.on_message = on_message
        self.write_activity = write_activity
        self.retry_seconds = retry_seconds

    async def run(self) -> None:
        """Runs until cancelled; each drop waits a little longer before reconnecting."""
        failures = 0
        while True:
            try:
                async for data in self.client.events(self.account):
                    failures = 0
                    message = parse_event(data)
                    if message is not None:
                        self.on_message(message)
                # The daemon closed the stream (for example it restarted): reconnect soon.
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Said once per outage, not on every retry.
                if failures == 0:
                    self.write_activity(
                        f"Signal listening stopped ({error_text(error)}); retrying.", "Signal reconnecting",
                    )
                failures += 1
            await asyncio.sleep(self.retry_seconds[min(max(failures - 1, 0), len(self.retry_seconds) - 1)])
