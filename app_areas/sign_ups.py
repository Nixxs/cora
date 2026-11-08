"""Signing up to sites for accounts of her own, with the person's yes each time: part of CoraApp (see app.py)."""

import asyncio
from datetime import datetime
from time import monotonic, sleep

from openai import OpenAI
from sqlalchemy.exc import IntegrityError
from textual import work
from textual.worker import get_current_worker

from mail.account import READY as EMAIL_READY
from services.confirmation import APPROVED, DECLINED as ANSWER_DECLINED
from services.connection import CONNECTED
from services.tools import result_text
from services.web_guard import refused_address
from sign_ups import password as passwords
from sign_ups.mail import confirmation_link, verified_site
from sign_ups.session import SIGN_UP_TOOLS, Details, Outcome, sign_up
from sign_ups.sites import site_of
from sign_ups.store import BLOCKED, CONFIRM_EMAIL, DAILY_LIMIT, DECLINED, DONE, FAILED, SIGNING_UP, TIMED_OUT
from subconscious.interests.browse_exploration import BROWSE_SECONDS, PAGE_URL
from subconscious.interests.exploration_store import claim_sign_up_wish_now
from app_areas.shared import NUDGE_HOURS

# A sign-up waits this long for an exploration to finish with the browser.
BROWSER_WAIT_SECONDS = BROWSE_SECONDS + 2 * 60
# One browser step while opening a confirmation link.
CONFIRM_CALL_SECONDS = 70
# The browser is in use (signing up or exploring): a confirmation waits for the next mail check.
BROWSER_BUSY = "busy"
CONFIRM_TOOLS = frozenset({"browser_navigate", "browser_snapshot", "browser_close"})
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
        self.sign_up_worker = self.run_sign_up(record, details, models["browser"], asyncio.get_running_loop())
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

    async def settle_sign_ups_by_mail(self, mails: list) -> set[str]:
        """Mail from a site with a sign-up waiting on it, before her new mail is sorted.

        From a site verified by Microsoft: a confirmation link is opened and the sign-up is done; any
        mail from a blocked sign-up's site means the person finished it for her, so it's done too.
        Returns the ids of mail kept out of sorting: confirmations she opened, and ones waiting for
        the browser, which the next mail check tries again.
        """
        try:
            waiting = await self.conversation_persistence.call_database(self.persistence.sign_ups_awaiting_mail)
        except Exception as error:
            self.activity.write(f"Checking mail for her sign-ups failed ({type(error).__name__}).", "Sign-up")
            return set()
        # Newest first, so a site's latest sign-up is the one settled.
        by_site = {}
        for sign_up_record in waiting:
            by_site.setdefault(sign_up_record.site, sign_up_record)
        kept = set()
        for mail in mails:
            site = verified_site(mail)
            record = by_site.get(site)
            if record is None:
                continue
            link = confirmation_link(mail.text)
            if link is not None:
                problem = await self.open_confirmation(link)
                if problem == BROWSER_BUSY:
                    kept.add(mail.id)  # unread and unsorted, for the next check
                    continue
                if problem is None:
                    await self.settle_sign_up(record, "Confirmed from the site's email.")
                    del by_site[site]
                    kept.add(mail.id)
                    self.mail_sorted.add(mail.id)
                    await self.mark_mail_read(mail.id)
                    continue
                self.activity.write(f"Couldn't open {site}'s confirmation link: {problem}", "Sign-up")
            if record.status == BLOCKED:
                await self.settle_sign_up(record, "The person finished it for her; the site's email reached her.")
                del by_site[site]
        return kept

    async def settle_sign_up(self, record, note: str) -> None:
        try:
            settled = await self.conversation_persistence.call_database(
                self.persistence.sign_up_settled_by_mail, record.id, note,
            )
        except Exception as error:
            self.activity.write(f"Saving her sign-up to {record.site} failed ({type(error).__name__}).", "Sign-up")
            return
        if settled:
            self.activity.write(f"Sign-up to {record.site}: done. {note}", "Sign-up")

    async def open_confirmation(self, url: str) -> str | None:
        """Open a confirmation link in her guarded browser; None once opened, else why not (or BROWSER_BUSY)."""
        if self.signing_up() or (self.exploration_worker is not None and not self.exploration_worker.is_finished):
            return BROWSER_BUSY
        status = self.services.status("playwright")
        if status.state != CONNECTED or not CONFIRM_TOOLS <= {tool.name for tool in status.tools}:
            return "her browser is off."
        if (refused := await refused_address(url)) is not None:
            return refused

        async def call(tool, arguments):
            return await asyncio.wait_for(self.services.call_tool("playwright", tool, arguments), CONFIRM_CALL_SECONDS)

        try:
            if (await call("browser_navigate", {"url": url})).is_error:
                return "the page didn't open."
            snapshot = await call("browser_snapshot", {})
            landed = PAGE_URL.search(result_text(snapshot))
            if landed and (refused := await refused_address(landed.group(1))) is not None:
                return refused
            self.activity.write(f"Opened a confirmation link: {landed.group(1) if landed else url}", "Sign-up")
            return None
        except Exception as error:
            return f"the browser failed ({type(error).__name__})."
        finally:
            try:
                await call("browser_close", {})
            except Exception:
                pass

    @work(group="sign-up-stalled", exclusive=True, exit_on_error=False)
    async def check_stalled_sign_ups(self) -> None:
        """Hourly: a confirmation email that hasn't come in two days is mentioned to the person, once."""
        try:
            stalled = await self.conversation_persistence.call_database(self.persistence.claim_stalled_sign_ups)
        except Exception as error:
            self.activity.write(f"Checking her sign-ups failed ({type(error).__name__}).", "Sign-up")
            return
        for record in stalled:
            try:
                await self.tell_person(
                    f"My sign-up to {record.site} is still waiting for its confirmation email after two days; it "
                    f"may have gone astray. Could you check, or sign me up yourself with {self.email_status.address} "
                    "and my sign-up password?"
                )
            except Exception:
                pass  # Activity has it
            self.activity.write(f"Sign-up to {record.site}: its confirmation email hasn't come.", "Sign-up")

    def check_sign_up_wish(self) -> None:
        """A site she'd like an account on, from her exploring: taken to the person in daytime, when
        they can answer (by voice, or on Signal), one at a time."""
        if self.closing or self.signing_up() or (
            self.sign_up_wish_worker is not None and not self.sign_up_wish_worker.is_finished
        ):
            return
        if datetime.now().hour not in NUDGE_HOURS or not (self.can_reach_by_voice() or self.signal_reaches_person()):
            return
        self.sign_up_wish_worker = self.ask_sign_up_wish()

    @work(group="sign-up-wish", exit_on_error=False)
    async def ask_sign_up_wish(self) -> None:
        try:
            # Claimed once, whatever comes of it, so the person isn't asked twice.
            wish = await asyncio.to_thread(claim_sign_up_wish_now)
        except Exception as error:
            self.activity.write(f"Checking for a sign-up she'd like failed ({type(error).__name__}).", "Sign-up")
            return
        if wish is None or self.closing:
            return
        site, problem = await self.check_sign_up(wish.url)
        if problem:
            self.activity.write(f"She'd like to sign up to {wish.url}, but didn't ask: {problem}", "Sign-up")
            return
        self.activity.write(f"Asking you about signing up to {site}, from her exploring.", "Sign-up")
        outcome = await self.ask_person_approval("Sign-up", "request_sign_up", {
            "site": wish.url, "what it is": wish.what, "why Cora wants it": wish.why,
            "came from": f"her exploring ({wish.topic})",
            "means": "Cora signs up with her own email and agrees to the site's terms",
        })
        await self.begin_sign_up(wish.url, site, wish.what, wish.why, outcome)

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
        self.live.own_name = saved
        if self.email_controls is not None:
            self.email_controls.sign_up_name = saved
        self.activity.write(f"Her name is now {saved}, for sign-ups and introductions.", "Name")

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
