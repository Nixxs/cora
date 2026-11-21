"""What unavailable_tools reads beyond each test's own subject, for stand-in apps: email not checked
yet, no Signal, and no World of Warcraft."""

from types import SimpleNamespace


def gates() -> dict:
    return {"email_checked": False, "email_status": SimpleNamespace(state="no app"),
            "signal_settings": SimpleNamespace(enabled=False, account=None, person_recipient=None),
            "game_push_to_talk": False, "game_watch": False, "wow_running": lambda: False}
