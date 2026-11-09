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
from os import getenv
import re
from xml.sax.saxutils import escape

import httpx2 as httpx
import keyring
from keyring.errors import PasswordDeleteError

KEYRING_SERVICE = "cora"
TOKEN_ACCOUNT = "phone:twilio-auth-token"
# Twilio's auth tokens are 32 hexadecimal characters.
TOKEN_PATTERN = re.compile(r"[0-9a-fA-F]{32}")
INCOMING_PATH = "/phone/incoming"
# Where Twilio streams a call's audio once it's connected to her (a WebSocket).
STREAM_PATH = "/phone/stream"
# Where Twilio says how a call she placed ended (answered or not).
STATUS_PATH = "/phone/status"
# Twilio's API for the region her account is in, where the auth token in Settings belongs: chosen
# in Settings > Services > Phone calls, else CORA_TWILIO_API from .env, else Australia (AU1).
REGIONS = {
    "au1": ("Sydney (AU1)", "https://api.sydney.au1.twilio.com"),
    "us1": ("United States (US1)", "https://api.twilio.com"),
    "ie1": ("Ireland (IE1)", "https://api.dublin.ie1.twilio.com"),
}
API_BASE = getenv("CORA_TWILIO_API", REGIONS["au1"][1]).strip().rstrip("/")


def region() -> str | None:
    """The region in use ('au1', 'us1' or 'ie1'), or None for another address from .env."""
    return next((code for code, (_, base) in REGIONS.items() if base == API_BASE), None)


def set_region(code: str) -> None:
    """From Settings: calls she places from now on go through this region's API."""
    global API_BASE
    API_BASE = REGIONS[code][1]
# How long a call she places rings before it counts as not answered.
RING_SECONDS = 30
API_SECONDS = 15

# What Twilio's voice says: an Australian English voice.
VOICE_LANGUAGE = "en-AU"
STRANGER_LINE = (
    "Hi, you've reached Cora, an A.I. assistant. I can only take calls from people I already know, "
    "sorry. Goodbye."
)
# Someone the person added, calling while another contact's call is on. (With calls from them off,
# or today's calls used up, their call is rejected unanswered: see reject.)
CONTACT_BUSY_LINE = ("Hi {name}, it's Cora. I'm on another call just now; try me again a bit later, or "
                     "message me on Signal. Bye.")
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


def reject() -> str:
    """TwiML: refuse the call without answering it, so nothing is said and no call minutes are used
    (the caller hears it as busy)."""
    return '<?xml version="1.0" encoding="UTF-8"?><Response><Reject reason="busy"/></Response>'


class TwilioError(Exception):
    """Twilio refused a request: its error code and HTTP status, never the message (it may echo numbers)."""

    def __init__(self, status: int, code) -> None:
        super().__init__(f"Twilio said {status}" + (f" (error {code})" if code else ""))
        self.status = status
        self.code = code


async def place_call(account_sid: str, token: str, from_number: str, to_number: str, twiml: str,
                     status_url: str, time_limit: int, transport=None) -> str:
    """Ask Twilio to ring someone from her number; once they answer it does what the TwiML says.
    Returns the call's ID. Twilio itself ends the call after time_limit seconds, whatever happens
    here, and tells status_url how it ended."""
    async with httpx.AsyncClient(timeout=API_SECONDS, transport=transport) as client:
        response = await client.post(
            f"{API_BASE}/2010-04-01/Accounts/{account_sid}/Calls.json",
            auth=(account_sid, token),
            data={
                "To": to_number, "From": from_number, "Twiml": twiml,
                "StatusCallback": status_url, "StatusCallbackMethod": "POST",
                "Timeout": str(RING_SECONDS), "TimeLimit": str(time_limit),
            },
        )
    if response.status_code >= 300:
        try:
            code = response.json().get("code")
        except ValueError:
            code = None
        raise TwilioError(response.status_code, code)
    return response.json()["sid"]
