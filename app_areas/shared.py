"""Constants and helpers shared by app.py and its areas (app_areas); never imports app.py."""

from datetime import timedelta
import json

from storage.client_store import CONVERSATION_GAP

# A shorter standby continues the same conversation; a longer one ends it so memories form.
CONVERSATION_END_STANDBY_SECONDS = CONVERSATION_GAP.total_seconds()

# Nudges for a quiet party: at most TASK_NUDGES_MAX, only in these local hours, and only once their
# chat has been quiet this long, so one never lands mid-conversation.
NUDGE_HOURS = range(9, 19)

# With webhooks on, Microsoft says when mail lands (T4a): a burst is one check this long after the
# first, by which time Graph has the message. Graph's subscription to new mail lasts
# MAIL_SUBSCRIPTION_LIFETIME and is renewed by the mail round once less than MAIL_SUBSCRIPTION_RENEW is left.
MAIL_NOTIFY_SECONDS = 5
MAIL_SUBSCRIPTION_LIFETIME = timedelta(days=2)
MAIL_SUBSCRIPTION_RENEW = timedelta(hours=12)
WEBHOOK_STARTUP_SECONDS = 5


def email_view(message) -> dict:
    """One email as she reads it: who, when, subject and its own new text."""
    sender = f"{message.sender_name} <{message.sender_address}>" if message.sender_name else message.sender_address
    return {"from": sender, "received": message.received_at.isoformat(timespec="minutes"),
            "subject": message.subject, "text": message.text}


def email_history_item(message, own: str) -> dict:
    """An earlier email in the thread as chat history: hers as her own words, everyone else's as theirs."""
    if message.sender_address == own:
        return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": message.text}]}
    return {"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": json.dumps(email_view(message), ensure_ascii=False)},
    ]}
