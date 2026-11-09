"""Cora's OpenAI API key: entered in the app (first start or Settings > General) and kept in the
system credential store, like her other secrets; `OPENAI_API_KEY` in `.env` still works.

One saved in the app wins over `.env`. Every OpenAI client reads `OPENAI_API_KEY` from the
environment when it's made, so applying a key here reaches every later call, Live's next connect
included. Blocking calls: run them off the UI thread.
"""

import os

from dotenv import load_dotenv
import keyring
import openai

from services.credentials import KEYRING_SERVICE

ACCOUNT = "openai:api_key"
ENVIRONMENT = "OPENAI_API_KEY"
SAVED, FROM_ENV, MISSING = "saved", "env", "missing"
CHECK_SECONDS = 20


def saved_key() -> str | None:
    return keyring.get_password(KEYRING_SERVICE, ACCOUNT) or None


def apply_api_key() -> str:
    """Put the key the app should use in the environment: SAVED, FROM_ENV or MISSING."""
    key = saved_key()
    if key:
        os.environ[ENVIRONMENT] = key
        return SAVED
    return FROM_ENV if os.environ.get(ENVIRONMENT, "").strip() else MISSING


def load_environment() -> str:
    """For the command-line tools: `.env`, then the key saved in the app."""
    load_dotenv()
    return apply_api_key()


def has_key() -> bool:
    return bool(os.environ.get(ENVIRONMENT, "").strip())


def masked(key: str | None = None) -> str:
    """The key in use, shown only by its last four characters."""
    key = (key if key is not None else os.environ.get(ENVIRONMENT, "")).strip()
    return f"…{key[-4:]}" if len(key) > 8 else ""


def check_key(key: str) -> str | None:
    """None when OpenAI accepts the key, or a short reason for the person (never the key)."""
    key = key.strip()
    if not key:
        return "Paste a key first."
    if any(character.isspace() for character in key):
        return "That doesn't look like a key: it has spaces in it."
    try:
        with openai.OpenAI(api_key=key, max_retries=0, timeout=CHECK_SECONDS) as client:
            client.models.list()
    except openai.AuthenticationError:
        return "OpenAI didn't accept that key."
    except openai.PermissionDeniedError:
        return "OpenAI accepted the key but it isn't allowed to use models; check its permissions."
    except openai.APIConnectionError:
        return "Couldn't reach OpenAI to check the key; check the internet connection."
    except openai.APIError as error:
        return f"OpenAI couldn't check the key ({type(error).__name__})."
    return None


def save_key(key: str) -> None:
    """Save the key (checked first by the caller) and use it from now on."""
    key = key.strip()
    if not key:
        raise ValueError("An empty key cannot be saved")
    keyring.set_password(KEYRING_SERVICE, ACCOUNT, key)
    os.environ[ENVIRONMENT] = key
