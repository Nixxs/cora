"""Cora's one sign-up password, and the name she signs up with, set in Settings > Services > Email.

Both are kept in the system credential store. She uses the same password for every site she
signs up to, so the person can sign in for her (or finish a sign-up she couldn't) without
looking anything up. It is never her email password, and never reaches the model: she types a
placeholder that the browser step swaps for it. Her name is hers to choose (a surname, say),
so it's a setting rather than written into the code.
"""

import keyring
from keyring.errors import PasswordDeleteError

KEYRING_SERVICE = "cora"
PASSWORD_ACCOUNT = "sign-up:password"
PASSWORD_MIN_CHARS = 12
PASSWORD_MAX_CHARS = 128
NAME_ACCOUNT = "sign-up:name"
DEFAULT_NAME = "Cora"
NAME_MAX_CHARS = 100


def sign_up_password() -> str:
    """Her sign-up password, or "" when none is set."""
    return keyring.get_password(KEYRING_SERVICE, PASSWORD_ACCOUNT) or ""


def save_sign_up_password(value: str) -> str:
    """Saves it as typed (spaces at either end trimmed); empty forgets it. ValueError when it's too short or long."""
    value = value.strip()
    if not value:
        try:
            keyring.delete_password(KEYRING_SERVICE, PASSWORD_ACCOUNT)
        except PasswordDeleteError:
            pass  # nothing was saved
        return ""
    if not PASSWORD_MIN_CHARS <= len(value) <= PASSWORD_MAX_CHARS:
        raise ValueError(f"Use {PASSWORD_MIN_CHARS} to {PASSWORD_MAX_CHARS} characters for her sign-up password.")
    if any(character in value for character in "\r\n"):
        raise ValueError("Her sign-up password can't contain line breaks.")
    keyring.set_password(KEYRING_SERVICE, PASSWORD_ACCOUNT, value)
    return value


def sign_up_name() -> str:
    """The name she gives when signing up (her own choice, such as a surname), or DEFAULT_NAME."""
    return keyring.get_password(KEYRING_SERVICE, NAME_ACCOUNT) or DEFAULT_NAME


def save_sign_up_name(value: str) -> str:
    """Saves it tidied (single spaces); empty goes back to DEFAULT_NAME. ValueError when it's too long."""
    value = " ".join(value.split())
    if not value or value == DEFAULT_NAME:
        try:
            keyring.delete_password(KEYRING_SERVICE, NAME_ACCOUNT)
        except PasswordDeleteError:
            pass  # nothing was saved
        return DEFAULT_NAME
    if len(value) > NAME_MAX_CHARS:
        raise ValueError(f"Keep her name under {NAME_MAX_CHARS} characters.")
    keyring.set_password(KEYRING_SERVICE, NAME_ACCOUNT, value)
    return value
