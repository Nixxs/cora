"""Phone calls to Cora's own number (P1): turning them on, Twilio's auth token, and who gets through:
part of CoraApp (see app.py).

Her phone number is a Twilio number, and Twilio asks her listener (the same one
as the webhooks and the web page) what to do with each. Only the person's own number (the one set
under Signal) reaches her; anyone else hears a short polite line. In P1a the person hears a test
line; P1b connects the call to her voice.
"""

import asyncio

from textual import work

from phone import twilio
from phone.routes import PhoneAccess
from ui.model_settings import PhoneControls


def number_ending(number: str) -> str:
    """Enough of a caller's number to tell calls apart in Activity, not the whole of it."""
    digits = "".join(character for character in number if character.isdigit())
    return f"…{digits[-3:]}" if len(digits) >= 3 else "unknown"


class PhoneCallsMixin:
    """Phone calls to her number, through Twilio."""

    def phone_access(self) -> PhoneAccess:
        """What the phone routes may do, for the listener."""
        return PhoneAccess(token=lambda: self.twilio_token, public_url=lambda: self.webhook_settings.public_url or "",
                           on_call=self.on_phone_call)

    def on_phone_call(self, caller: str, call_id: str) -> str:
        """A call Twilio vouched for: the TwiML that answers it."""
        if not self.webhook_settings.phone_enabled:
            return twilio.say_and_hang_up(twilio.NOT_SET_UP_LINE)
        if twilio.same_number(caller, self.signal_settings.allowed_number):
            self.activity.write("You called Cora's number; for now you hear a test line (calls reach her in P1b).",
                                "Phone call")
            self.notify("You're calling Cora's number.")
            return twilio.say_and_hang_up(twilio.TEST_LINE)
        # Strangers (and withheld numbers) are turned away, as on Signal.
        self.activity.write(f"A call from a number Cora doesn't know ({number_ending(caller)}) was turned away.",
                            "Phone call")
        return twilio.say_and_hang_up(twilio.STRANGER_LINE)

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
