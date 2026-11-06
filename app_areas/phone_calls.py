"""Phone calls to Cora's own number (P1): turning them on, Twilio's auth token, and who gets through:
part of CoraApp (see app.py).

Her phone number is a Twilio number, and Twilio asks her listener (the same one
as the webhooks and the web page) what to do with each. Only the person's own number (the one set
under Signal) reaches her; anyone else hears a short polite line.

The person's call is connected to her voice: Twilio streams its audio to the listener, which hands
it to the same relay as the web page's Talk (web/voice.py). So it's her usual session, tools and
memory; a call wakes her from standby and hanging up puts her back. Each call gets a one-time
token in its TwiML, so only that call's stream is let in.
"""

import asyncio
import secrets
from time import monotonic

from textual import work

from phone import twilio
from phone.routes import PhoneAccess
from ui.model_settings import PhoneControls


# Her note on a call, so she knows the person can only hear her (see voice_talk).
ON_THE_PHONE = (
    "they've rung your phone number and are talking to you on a phone call, so they can only hear "
    "you: nothing shown on the desk's screen or the web page reaches them, so say what matters, "
    "in a way that's easy to follow by ear. Where they are is never checked during a call; ask if "
    "it matters. Tools that change things can't be approved on a call (never by saying yes): the "
    "request waits on the desk's screen, so tell them it'll need doing from the desk."
)
# How long a call's token waits for its stream to open.
TOKEN_SECONDS = 30


def number_ending(number: str) -> str:
    """Enough of a caller's number to tell calls apart in Activity, not the whole of it."""
    digits = "".join(character for character in number if character.isdigit())
    return f"…{digits[-3:]}" if len(digits) >= 3 else "unknown"


class PhoneCallsMixin:
    """Phone calls to her number, through Twilio."""

    def phone_access(self) -> PhoneAccess:
        """What the phone routes may do, for the listener."""
        return PhoneAccess(token=lambda: self.twilio_token, public_url=lambda: self.webhook_settings.public_url or "",
                           on_call=self.on_phone_call, claim=self.claim_phone_token, voice=self.web_voice,
                           on_stream=self.on_phone_stream)

    def on_phone_call(self, caller: str, call_id: str) -> str:
        """A call Twilio vouched for: the TwiML that answers it."""
        if not self.webhook_settings.phone_enabled:
            return twilio.say_and_hang_up(twilio.NOT_SET_UP_LINE)
        if twilio.same_number(caller, self.signal_settings.allowed_number):
            if self.phone_listener is not None:
                self.activity.write("You called Cora's number while already on a call with her.", "Phone call")
                return twilio.say_and_hang_up(twilio.BUSY_LINE)
            now = monotonic()
            self.phone_tokens = {token: until for token, until in self.phone_tokens.items() if until > now}
            token = secrets.token_urlsafe(32)
            self.phone_tokens[token] = now + TOKEN_SECONDS
            self.activity.write("You're calling Cora's number; connecting the call to her.", "Phone call")
            return twilio.connect_stream(twilio.stream_url(self.webhook_settings.public_url or ""), token)
        # Strangers (and withheld numbers) are turned away, as on Signal.
        self.activity.write(f"A call from a number Cora doesn't know ({number_ending(caller)}) was turned away.",
                            "Phone call")
        return twilio.say_and_hang_up(twilio.STRANGER_LINE)

    def claim_phone_token(self, token: str) -> bool:
        """Whether a stream's token was given to a call moments ago; each works once."""
        until = self.phone_tokens.pop(token, None)
        return until is not None and until > monotonic() and self.webhook_settings.phone_enabled

    def on_phone_stream(self, listener, connected: bool) -> None:
        """A call's audio starts or stops reaching her: like a page's Talk, with the phone's
        audio for her microphone and its line for her speaker (the desk is quiet meanwhile)."""
        if connected:
            self.phone_listener = listener
            if len(self.web_voice.listeners) == 1:
                # What the desk was still about to say goes quiet, rather than both at once.
                self.live.yield_speaker()
            self.notify("You're on a call with Cora.")
            self.voice_talk(listener, True, "on a phone call", "", ON_THE_PHONE, greeting="phone")
            return
        if self.phone_listener is listener:
            self.phone_listener = None
        self.voice_talk(listener, False, "", "The phone call ended.", "")

    async def load_twilio_token(self) -> None:
        try:
            self.twilio_token = await asyncio.to_thread(twilio.twilio_token)
        except Exception as error:
            self.activity.write(f"The Twilio auth token didn't load ({type(error).__name__}).", "Phone calls")

    @work(group="webhook-setup", exit_on_error=False)
    async def set_phone_enabled(self, enabled: bool) -> None:
        try:
            await self.conversation_persistence.call_database(self.persistence.save_phone_enabled, enabled)
        except Exception as error:
            self.activity.write(f"Saving phone calls {'on' if enabled else 'off'} failed ({type(error).__name__}).",
                                "Phone calls")
            self.notify("That couldn't be saved. Check Activity.", severity="error")
            return
        await self.apply_webhook_settings()

    @work(group="twilio-token", exit_on_error=False)
    async def save_twilio_token(self, token: str) -> None:
        """Twilio's auth token, in the system credential store; empty forgets it (calls are then refused)."""
        try:
            saved = await asyncio.to_thread(twilio.save_twilio_token, token)
        except ValueError as error:
            self.notify(str(error), severity="warning")
            return
        except Exception as error:
            self.activity.write(f"Saving the Twilio auth token failed ({type(error).__name__}).", "Phone calls")
            self.notify("The Twilio auth token couldn't be saved. Check Activity.", severity="error")
            return
        self.twilio_token = saved
        if self.phone_controls is not None:
            self.phone_controls.has_token = bool(saved)
        self.activity.write("Saved the Twilio auth token." if saved else "Forgot the Twilio auth token.", "Phone calls")

    def phone_status(self) -> tuple[str, str]:
        """(the row's line, what's still missing) for Settings."""
        settings = self.webhook_settings
        if not settings.phone_enabled:
            return "", ""
        if not self.webhook_listening():
            return "● on · not listening yet", ""
        missing = [what for what, there in (
            ("the Twilio auth token below", self.twilio_token),
            ("your number under Signal", self.signal_settings.allowed_number),
        ) if not there]
        if missing:
            return "● on · not answering yet", "Still needed: " + " and ".join(missing) + "."
        return "● answering calls", ""

    def phone_incoming_url(self) -> str:
        url = self.webhook_settings.public_url
        return f"{url}{twilio.INCOMING_PATH}" if url else ""

    def make_phone_controls(self) -> PhoneControls:
        self.phone_controls = PhoneControls(
            status=self.phone_status, set_enabled=self.set_phone_enabled, save_token=self.save_twilio_token,
            incoming_url=self.phone_incoming_url, enabled=self.webhook_settings.phone_enabled,
            has_token=bool(self.twilio_token),
        )
        return self.phone_controls
