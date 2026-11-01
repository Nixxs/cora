"""Signing up to sites for accounts of her own, with the person's yes each time: part of CoraApp (see app.py)."""

import asyncio
from time import monotonic, sleep

from openai import OpenAI
from sqlalchemy.exc import IntegrityError
from textual import work
from textual.worker import get_current_worker

from mail.account import READY as EMAIL_READY
from services.confirmation import APPROVED, DECLINED as ANSWER_DECLINED
from services.web_guard import refused_address
from sign_ups import password as passwords
from sign_ups.session import SIGN_UP_TOOLS, Details, Outcome, sign_up
from sign_ups.sites import site_of
from sign_ups.store import BLOCKED, CONFIRM_EMAIL, DAILY_LIMIT, DECLINED, DONE, FAILED, SIGNING_UP, TIMED_OUT
from subconscious.interests.browse_exploration import BROWSE_SECONDS

# A sign-up waits this long for an exploration to finish with the browser.
BROWSER_WAIT_SECONDS = BROWSE_SECONDS + 2 * 60
# What stopped her, as she says it to the person.
BLOCKED_BY = {
    "captcha": "a captcha I couldn't get past",
    "phone": "it wanted a phone number",
    "payment": "it wanted card details",
    "personal_details": "it wanted personal details I don't have to give",
    "other": "something I couldn't get past",
}


class SignUpsMixin:
    """Signing up to sites for accounts of her own, with the person's yes each time."""

    def signing_up(self) -> bool:
        return self.sign_up_worker is not None and not self.sign_up_worker.is_finished

    async def check_sign_up(self, url: str) -> tuple[str | None, str | None]:
        """(the site, None) when she may ask to sign up there, or (None, why not), checked before asking."""
        site = site_of(url)
        if site is None:
            return None, "That isn't a website's address; give its https:// address."
        if (refused := await refused_address(url)) is not None:
            return None, refused
        if self.email_status.state != EMAIL_READY or not self.email_status.address:
            return None, "Your email isn't set up, and signing up needs it; the person can set it up in Settings."
        if not await asyncio.to_thread(passwords.sign_up_password):
            return None, ("You don't have a sign-up password yet; ask the person to set one in Settings > "
                          "Services > Email.")
        if self.exploration_browser(asyncio.get_running_loop(), needs=SIGN_UP_TOOLS) is None:
            return None, ("Your browser is off, or its click and typing tools are switched off; the person can "
                          "turn them on in Settings > Services > Browser.")
        if self.signing_up():
            return None, "You're already signing up somewhere; ask again once that's finished."
        held = await self.conversation_persistence.call_database(self.persistence.sign_up_holding, site)
        if held is not None:
            return None, f"You already have an account on {site} (or one on its way), so there's nothing to ask."
        if await self.conversation_persistence.call_database(self.persistence.sign_ups_asked_today) >= DAILY_LIMIT:
            return None, "You've asked to sign up to enough sites for today; ask again tomorrow."
        return site, None

    async def begin_sign_up(self, url: str, site: str, what: str, why: str, outcome: str) -> str:
        """Records the person's answer and, with a yes, starts signing up; returns what to tell her, or ""."""
        status = SIGNING_UP if outcome == APPROVED else DECLINED if outcome == ANSWER_DECLINED else TIMED_OUT
        try:
            record = await self.conversation_persistence.call_database(
                self.persistence.record_sign_up, site, url, what, why, status,
            )
        except IntegrityError:
            return f"You already have an account on {site} (or one on its way), so nothing more was done."
        if status != SIGNING_UP:
            self.activity.write(f"Sign-up to {site}: {status.replace('_', ' ')}.", "Sign-up")
            return ""
        try:
            models = await self.conversation_persistence.call_database(self.persistence.load_models)
            password = await asyncio.to_thread(passwords.sign_up_password)
            name = await asyncio.to_thread(passwords.sign_up_name)
        except Exception as error:
            await self.end_sign_up(record, Outcome(FAILED, None, f"Couldn't start ({type(error).__name__}).", 0))
            return "Signing up couldn't start; nothing was signed up to."
        details = Details(name, self.email_status.address, password, forbidden=tuple(filter(None, (
            self.live.person_name, self.email_person, self.signal_settings.allowed_number,
        ))))
        self.activity.write(f"Signing up to {site} ({what}).", "Sign-up")
        self.sign_up_worker = self.run_sign_up(record, details, models["delegation"], asyncio.get_running_loop())
        return ("They said yes. You're signing up in the background now, and the person will hear how it "
                "went, so don't say it's done yet.")

    @work(thread=True, group="sign-up", exit_on_error=False)
    def run_sign_up(self, record, details: Details, model: str, loop) -> None:
        worker = get_current_worker()
        # An exploration using the browser stops as soon as a sign-up wants it; wait for it to close.
        waited = monotonic()
        while self.exploration_worker is not None and not self.exploration_worker.is_finished:
            if self.closing or worker.is_cancelled or monotonic() - waited > BROWSER_WAIT_SECONDS:
                break
            sleep(1)
        browser = None if self.closing else self.call_from_thread(
            self.exploration_browser, loop, SIGN_UP_TOOLS, lambda: not (self.closing or worker.is_cancelled),
            "Sign-up",
        )
        if browser is None:
            outcome = Outcome(FAILED, None, "The browser wasn't available.", 0)
        else:
            try:
                with OpenAI(max_retries=0) as client:
                    outcome = sign_up(client, model, {"url": record.url, "what": record.what, "why": record.why},
                                      record.site, details, browser)
            except Exception as error:
                outcome = Outcome(FAILED, None, f"Signing up stopped ({type(error).__name__}).", 0)
        if not self.closing:
            self.call_from_thread(lambda: self.run_worker(self.end_sign_up(record, outcome), group="sign-up-end",
                                                          exit_on_error=False))

    async def end_sign_up(self, record, outcome: Outcome) -> None:
        """Saves how it went and tells the person."""
        try:
            await self.conversation_persistence.call_database(
                self.persistence.finish_sign_up, record.id, outcome.status, outcome.blocked_by, outcome.note or None,
            )
        except Exception as error:
            self.activity.write(f"Saving the sign-up to {record.site} failed ({type(error).__name__}).", "Sign-up")
        state = outcome.status.replace("_", " ")
        self.activity.write(f"Sign-up to {record.site}: {state}. {outcome.note}".strip(), "Sign-up")
        try:
            await self.tell_person(self.sign_up_news(record, outcome))
        except Exception:
            pass  # Activity has it

    def sign_up_news(self, record, outcome: Outcome) -> str:
        site, email = record.site, self.email_status.address
        # What she called it is her own sentence; its full stop would double up inside the brackets.
        what = record.what.strip().rstrip(".")
        note = f" {outcome.note}" if outcome.note else ""
        if outcome.status == DONE:
            return f"I've signed up to {site} ({what}) with my own email.{note}"
        if outcome.status == CONFIRM_EMAIL:
            return f"I've signed up to {site}; it wants me to confirm my email address before the account works.{note}"
        if outcome.status == BLOCKED:
            at = f" The form is at {outcome.page}." if outcome.page else ""
            return (f"I tried to sign up to {site} ({what}), but {BLOCKED_BY[outcome.blocked_by]}.{note}{at} "
                    f"Could you sign me up yourself? Use my email, {email}, and my sign-up password from Settings.")
        return f"Signing up to {site} didn't work out.{note}"

    @work(group="sign-up-password", exit_on_error=False)
    async def save_sign_up_name(self, name: str) -> None:
        """The name she signs up with, her own choice; empty goes back to just Cora."""
        try:
            saved = await asyncio.to_thread(passwords.save_sign_up_name, name)
        except ValueError as error:
            self.notify(str(error), severity="warning")
            return
        except Exception as error:
            self.activity.write(f"Saving her sign-up name failed ({type(error).__name__}).", "Sign-up")
            self.notify("Her sign-up name couldn't be saved. Check Activity.", severity="error")
            return
        self.sign_up_name = saved
        if self.email_controls is not None:
            self.email_controls.sign_up_name = saved
        self.activity.write(f"She'll sign up as {saved}.", "Sign-up")

    @work(group="sign-up-password", exit_on_error=False)
    async def save_sign_up_password(self, password: str) -> None:
        """Her one sign-up password, in the system credential store; empty forgets it."""
        try:
            saved = await asyncio.to_thread(passwords.save_sign_up_password, password)
        except ValueError as error:
            self.notify(str(error), severity="warning")
            return
        except Exception as error:
            self.activity.write(f"Saving her sign-up password failed ({type(error).__name__}).", "Sign-up")
            self.notify("Her sign-up password couldn't be saved. Check Activity.", severity="error")
            return
        if self.email_controls is not None:
            self.email_controls.has_sign_up_password = bool(saved)
        self.activity.write("Saved her sign-up password." if saved else "Forgot her sign-up password.", "Sign-up")
