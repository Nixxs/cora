"""Twilio, for calls to Cora's number (P1): checking a request really came from Twilio, and what to say.

Her phone number is a Twilio number (her Signal SIM's carrier wouldn't forward calls to it), and
Twilio asks Cora's listener what to do with each one (POST /phone/incoming). Every request is
signed with the Twilio account's auth token (X-Twilio-Signature), which is checked before anything
else, so nobody else can pretend to be a call. The caller's number then decides what follows: the
person's own number reaches her, anyone else hears a short polite line and the call ends.

Caller ID can be faked, so a call is never trusted to approve anything (see the README).

The auth token is kept in the system credential store. Blocking calls: run them off the UI thread.
"""

import base64
import hashlib
import hmac
import re
from xml.sax.saxutils import escape

import keyring
from keyring.errors import PasswordDeleteError

KEYRING_SERVICE = "cora"
TOKEN_ACCOUNT = "phone:twilio-auth-token"
# Twilio's auth tokens are 32 hexadecimal characters.
TOKEN_PATTERN = re.compile(r"[0-9a-fA-F]{32}")
INCOMING_PATH = "/phone/incoming"
# Where Twilio streams a call's audio once it's connected to her (a WebSocket).
STREAM_PATH = "/phone/stream"

# What Twilio's voice says: an Australian English voice.
VOICE_LANGUAGE = "en-AU"
STRANGER_LINE = (
    "Hi, you've reached Cora, an A.I. assistant. I can only take calls from people I already know, "
    "sorry. Goodbye."
)
# A second call from the person while one is already connected to her.
BUSY_LINE = "Sorry, Cora is already on a call with you. Goodbye."
NOT_SET_UP_LINE = "Sorry, Cora can't take calls right now. Goodbye."


def twilio_token() -> str:
    """The Twilio auth token, or "" when none is set."""
    return keyring.get_password(KEYRING_SERVICE, TOKEN_ACCOUNT) or ""


def save_twilio_token(value: str) -> str:
    """Saves it trimmed; empty forgets it. ValueError when it isn't a Twilio auth token."""
    value = value.strip()
    if not value:
        try:
            keyring.delete_password(KEYRING_SERVICE, TOKEN_ACCOUNT)
        except PasswordDeleteError:
            pass  # nothing was saved
        return ""
    if not TOKEN_PATTERN.fullmatch(value):
        raise ValueError("That isn't a Twilio auth token (32 letters and digits, from the Twilio console).")
    keyring.set_password(KEYRING_SERVICE, TOKEN_ACCOUNT, value)
    return value


def signature(token: str, url: str, params: list[tuple[str, str]]) -> str:
    """Twilio's signature for a request: the full URL it called, then each parameter's name and
    value in name order, signed with HMAC-SHA1 and the auth token, in base64."""
    data = url + "".join(name + value for name, value in sorted(params, key=lambda pair: pair[0]))
    return base64.b64encode(hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()).decode()


def valid_signature(token: str, url: str, params: list[tuple[str, str]], given: str | None) -> bool:
    if not token or not given:
        return False
    return hmac.compare_digest(signature(token, url, params), given)


def same_number(caller: str | None, known: str | None) -> bool:
    """Whether a caller ID is a known number: both in full international form, digits compared."""
    if not caller or not known:
        return False
    digits = re.sub(r"\D", "", caller)
    return len(digits) >= 8 and caller.strip().startswith("+") and digits == re.sub(r"\D", "", known)


def say_and_hang_up(line: str) -> str:
    """TwiML: say one line, then end the call."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Response><Say language="{VOICE_LANGUAGE}">{escape(line)}</Say><Hangup/></Response>'
    )


def stream_url(public_url: str) -> str:
    """The WebSocket address for a call's audio, from the public https address."""
    return "wss://" + public_url.removeprefix("https://").rstrip("/") + STREAM_PATH


def connect_stream(url: str, token: str) -> str:
    """TwiML: connect the call's audio, both ways, to her listener's WebSocket, with a one-time
    token so only this call's stream is let in. When the stream ends, so does the call."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Response><Connect><Stream url="{escape(url)}">'
        f'<Parameter name="token" value="{escape(token)}"/>'
        '</Stream></Connect></Response>'
    )
