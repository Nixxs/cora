"""Service tokens live in the operating system's credential store, never in a file or the database.

Blocking calls: run them off the UI thread.
"""

import keyring
from keyring.errors import PasswordDeleteError

KEYRING_SERVICE = "cora"


def _account(service_id: str) -> str:
    return f"mcp:{service_id}"


def get_token(service_id: str) -> str | None:
    return keyring.get_password(KEYRING_SERVICE, _account(service_id)) or None


def save_token(service_id: str, token: str) -> None:
    if not token.strip():
        raise ValueError("An empty token cannot be saved")
    keyring.set_password(KEYRING_SERVICE, _account(service_id), token.strip())


def forget_token(service_id: str) -> None:
    try:
        keyring.delete_password(KEYRING_SERVICE, _account(service_id))
    except PasswordDeleteError:
        pass  # nothing was saved
