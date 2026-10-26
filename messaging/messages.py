"""The person's Signal messages as chat lines and as backend input."""

from datetime import datetime, timedelta
import json

from .receiver import IncomingMessage

# Sent this long ago or more, a message is marked with when it was sent.
LATE_AFTER = timedelta(minutes=2)
# Sent back when a reply fails, so the person isn't left waiting with no answer.
SIGNAL_SORRY = "Sorry, I couldn't answer that just now. Try again in a little while."


# How long a request asked in the person's Signal chat waits for their answer.
APPROVAL_SECONDS = 10 * 60
APPROVAL_ARGUMENTS_MAX_CHARS = 1500
# A clear yes approves and a clear no declines; anything else declines and is answered as a message.
YES_WORDS = {"yes", "y", "yep", "yeah", "yup", "approve", "approved", "yes please"}
NO_WORDS = {"no", "n", "nope", "nah", "decline", "cancel", "don't", "dont", "no thanks"}


def approval_request_text(request) -> str:
    """What she sends to ask: exactly what would be done, and how to answer."""
    arguments = json.dumps(request.arguments, indent=1, ensure_ascii=False)
    if len(arguments) > APPROVAL_ARGUMENTS_MAX_CHARS:
        arguments = arguments[:APPROVAL_ARGUMENTS_MAX_CHARS] + "…"
    return (
        f"I'd like to use {request.title} · {request.tool}:\n{arguments}\n\n"
        f"Reply YES to approve or NO to decline. Nothing happens until you do "
        f"(it lapses in {APPROVAL_SECONDS // 60} minutes)."
    )


def approval_answer(text: str) -> bool | None:
    """True for a clear yes, False for a clear no, None for anything else."""
    words = " ".join(text.lower().replace("’", "'").strip(" .!?").split())
    if words in YES_WORDS:
        return True
    if words in NO_WORDS:
        return False
    return None


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
