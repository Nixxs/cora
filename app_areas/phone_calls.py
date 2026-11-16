"""Phone calls to Cora's own number (P1): turning them on, Twilio's auth token, and who gets through:
part of CoraApp (see app.py).

Her phone number is a Twilio number, and Twilio asks her listener (the same one
as the webhooks and the web page) what to do with each. Only the person's own number (the one set
under Signal) reaches her; anyone else hears a short polite line.

The person's call is connected to her voice: Twilio streams its audio to the listener, which hands
it to the same relay as the web page's Talk (web/voice.py). So it's her usual session, tools and
memory; a call wakes her from standby and hanging up puts her back. Each call gets a one-time
token in its TwiML, so only that call's stream is let in.

Caller ID can be faked, so nothing is approved by voice on a call: while one is connected, her
requests to use tools that change things go to the person's Signal chat (reply YES), as when they
message her there; without Signal linked they wait on the desk's pop-up.
"""

import asyncio
from dataclasses import dataclass
import secrets
from time import monotonic

from textual import work

from messaging.messages import APPROVAL_SECONDS
from messaging.setup import LINKED as SIGNAL_LINKED
from phone import twilio
from phone.routes import CallLink, PhoneAccess
from app_areas.task_calls import TaskCall
from ui.model_settings import ENV_REGION, PhoneControls


# Her note on a call, so she knows the person can only hear her (see voice_talk).
ON_THE_PHONE = (
    "they've rung your phone number and are talking to you on a phone call, so they can only hear "
    "you: nothing shown on the desk's screen or the web page reaches them, so say what matters, "
    "in a way that's easy to follow by ear. Where they are is never checked during a call; ask if "
    "it matters. Tools that change things are never approved by voice on a call: the request goes "
    "to their Signal chat, where they reply YES."
)
# How long a call's token waits for its stream to open.
TOKEN_SECONDS = 30


@dataclass
class OnCallApproval:
    """How a request to use a tool is asked in the person's Signal chat instead of the desk's pop-up
    (see Confirmations): while they're on a call, and Claude Code's while she's in standby."""

    ask_approval: object
    approval_seconds: float = APPROVAL_SECONDS
    asked_where: str = "in your Signal chat (you're on a phone call)"


def number_ending(number: str) -> str:
    """Enough of a caller's number to tell calls apart in Activity, not the whole of it."""
    digits = "".join(character for character in number if character.isdigit())
    return f"…{digits[-3:]}" if len(digits) >= 3 else "unknown"


class PhoneCallsMixin:
    """Phone calls to her number, through Twilio."""

    def phone_access(self) -> PhoneAccess:
        """What the phone routes may do, for the listener."""
        return PhoneAccess(token=lambda: self.twilio_token, public_url=lambda: self.webhook_settings.public_url or "",
                           on_call=self.on_phone_call, claim=self.claim_phone_token, on_status=self.on_twilio_status)

    def on_phone_call(self, caller: str, call_id: str) -> str:
        """A call Twilio vouched for: the TwiML that answers it."""
        if not self.webhook_settings.phone_enabled:
            return twilio.say_and_hang_up(twilio.NOT_SET_UP_LINE)
        if twilio.same_number(caller, self.signal_settings.allowed_number):
            if self.phone_listener is not None:
                self.activity.write("You called Cora's number while already on a call with her.", "Phone call")
                return twilio.say_and_hang_up(twilio.BUSY_LINE)
            self.activity.write("You're calling Cora's number; connecting the call to her.", "Phone call")
            return twilio.connect_stream(twilio.stream_url(self.webhook_settings.public_url or ""),
                                         self.give_phone_token(None))
        contact = next((client for client in self.signal_clients.values()
                        if twilio.same_number(caller, client.number)), None)
        if contact is not None:
            if not contact.may_call:
                self.activity.write(f"{contact.name} called; calls from them are off, so it wasn't answered.",
                                    "Phone call")
                return twilio.reject()
            if self.contact_call is not None:
                self.activity.write(f"{contact.name} called while another contact's call was on.", "Phone call")
                return twilio.say_and_hang_up(twilio.CONTACT_BUSY_LINE.format(name=contact.name))
            if not self.contact_call_allowed(contact):
                self.activity.write(f"{contact.name} called again; today's calls from them are used up, so it "
                                    "wasn't answered.", "Phone call")
                return twilio.reject()
            self.count_contact_call(contact)
            self.activity.write(f"{contact.name} is calling; connecting them to Cora.", "Phone call")
            return twilio.connect_stream(twilio.stream_url(self.webhook_settings.public_url or ""),
                                         self.give_phone_token(contact))
        # Strangers (and withheld numbers) are turned away, as on Signal.
        self.activity.write(f"A call from a number Cora doesn't know ({number_ending(caller)}) was turned away.",
                            "Phone call")
        return twilio.say_and_hang_up(twilio.STRANGER_LINE)

    def give_phone_token(self, target, seconds: float = TOKEN_SECONDS) -> str:
        """A one-time token for a call being connected: the person's (target None), a contact's (their
        Client), or one she placed for a task (its TaskCall)."""
        now = monotonic()
        self.phone_tokens = {token: given for token, given in self.phone_tokens.items() if given[0] > now}
        token = secrets.token_urlsafe(32)
        self.phone_tokens[token] = (now + seconds, target)
        return token

    def claim_phone_token(self, token: str) -> CallLink | None:
        """Where a stream's call goes, if its token was given to a call moments ago (each works once):
        the person's call to her own session, a contact's to a session of its own."""
        given = self.phone_tokens.pop(token, None)
        if given is None or given[0] <= monotonic() or not self.webhook_settings.phone_enabled:
            return None
        target = given[1]
        if isinstance(target, TaskCall):
            return self.task_call_link(target)
        if target is not None:
            return self.contact_call_link(target)
        return CallLink(self.web_voice, self.on_phone_stream,
                        on_gaps=lambda summary: summary and self.activity.write(summary, "Phone call"))

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

    def phone_approval_route(self) -> OnCallApproval | None:
        """While a call is connected, voice Cora's requests are asked in the person's Signal chat
        (never by voice); None otherwise, or without Signal linked (the desk's pop-up, then)."""
        if self.phone_listener is None:
            return None
        account, person = self.signal.status.account, self.signal_settings.person_recipient
        if self.signal.status.state != SIGNAL_LINKED or not account or not person:
            self.activity.write("A request for approval during the call waits on the desk: Signal isn't linked.",
                                "Phone call")
            return None
        ask = self.signal_approval_asker(account, person, self.signal_settings.allowed_number)

        async def ask_on_signal(request) -> bool:
            self.tell_cora_approval_waiting(request.title, request.tool, on_signal=True,
                                            on_behalf=request.on_behalf)
            return await ask(request)

        return OnCallApproval(ask_on_signal)

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
        calling_out = self.webhook_settings
        if calling_out.twilio_account_sid and calling_out.phone_number:
            return "● answering calls", f"Calling out for tasks from {calling_out.phone_number}."
        return "● answering calls", "Not calling out: give the Account SID and her number below for that."

    def phone_incoming_url(self) -> str:
        url = self.webhook_settings.public_url
        return f"{url}{twilio.INCOMING_PATH}" if url else ""

    def make_phone_controls(self) -> PhoneControls:
        self.phone_controls = PhoneControls(
            status=self.phone_status, set_enabled=self.set_phone_enabled, save_token=self.save_twilio_token,
            incoming_url=self.phone_incoming_url, enabled=self.webhook_settings.phone_enabled,
            has_token=bool(self.twilio_token), account_sid=self.webhook_settings.twilio_account_sid or "",
            phone_number=self.webhook_settings.phone_number or "", save_account=self.save_twilio_account,
            region=twilio.region() or ENV_REGION, region_address=twilio.API_BASE,
            save_region=self.save_twilio_region,
        )
        return self.phone_controls
