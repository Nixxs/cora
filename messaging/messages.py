"""The person's Signal messages as chat lines and as backend input."""

from datetime import datetime, timedelta

from .receiver import IncomingMessage

# Sent this long ago or more, a message is marked with when it was sent.
LATE_AFTER = timedelta(minutes=2)
# Sent back when a reply fails, so the person isn't left waiting with no answer.
SIGNAL_SORRY = "Sorry, I couldn't answer that just now. Try again in a little while."


def signal_message_text(message: IncomingMessage) -> str:
    """The message as shown and saved; attachments are only noted, never read."""
    if not message.attachments:
        return message.text
    note = f"[{message.attachments} attachment{'s' if message.attachments > 1 else ''}, not opened]"
    return f"{message.text} {note}".strip()


def ago(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} minutes ago"
    hours = minutes // 60
    return f"{hours} hour{'s' if hours > 1 else ''} ago" if hours < 48 else f"{hours // 24} days ago"


def signal_content(batch: list[IncomingMessage], now: datetime) -> list[dict]:
    """One turn's input: each message in order, late ones marked with when they were sent."""
    content = []
    for message in batch:
        text = signal_message_text(message)
        late = now - message.sent_at
        if late >= LATE_AFTER:
            sent = message.sent_at.astimezone().strftime("%a %H:%M")
            text = f"[sent {sent}, {ago(late)}] {text}"
        content.append({"type": "input_text", "text": text})
    return content
