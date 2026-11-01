"""Signing up: the site rule, her details only, the hidden password, payment stops, the tool, the app and the store."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from textual.widgets import Button, Input

from mail.account import EmailStatus, READY
from services.confirmation import APPROVED, DECLINED, TIMED_OUT
from session_config import BACKEND_INSTRUCTIONS
from sign_ups.password import (
    NAME_ACCOUNT, PASSWORD_ACCOUNT, save_sign_up_name, save_sign_up_password, sign_up_name, sign_up_password,
)
from sign_ups.session import (
    MAX_ACTIONS, NOT_A_CARD, NOT_YOURS, PAYMENT_STOP, SCHEMA, SIGN_UP_SECONDS, SIGN_UP_TOOLS, TOOLS, Details,
    Outcome, SignUpSession, sign_up,
)
from sign_ups.sites import on_site, site_of
from sign_ups.store import (
    BLOCKED, CONFIRM_EMAIL, DAILY_LIMIT, DONE, FAILED, SIGNING_UP, STALE_AFTER, SignUpStore,
)
from subconscious.interests.browse_exploration import BrowserAccess
from tools.request_sign_up import NOT_DONE, RequestSignUpTool, validate_arguments
import test_browse_exploration as browse_tests
import test_conversation_summaries as summary_tests
import test_email as email_tests
import test_keyboard_persistence as keyboard_tests

SITE_URL = "https://www.birdforum.net/register"
PASSWORD = "correct horse battery staple"
DETAILS = Details("Cora", "cora@intelligis.io", PASSWORD,
                  forbidden=("Nick", "nick@example.com", "+61400123456"))
FORM = """- heading "Join BirdForum" [level=1] [ref=e1]
- textbox "Email" [ref=e5]
- textbox "Password" [ref=e6]
- checkbox "Send me offers" [checked] [ref=e7]
- button "Create account" [ref=e8]"""
CARD_FORM = """- heading "Choose a plan" [level=1] [ref=e1]
- textbox "Card number" [ref=e9]"""


class FakeBrowser(browse_tests.FakeBrowser):
    """Pages land where asked; clicking a ref can lead somewhere set up in the test, and typing is echoed back."""

    def __init__(self, text=FORM, clicks=None, echo=""):
        super().__init__(text=text)
        self.clicks = clicks or {}
        self.echo = echo

    def call(self, tool, arguments):
        if tool == "browser_click" and arguments["target"] in self.clicks:
            self.at = self.clicks[arguments["target"]]
        if tool == "browser_type":
            self.calls.append((tool, arguments))
            # Playwright's reply quotes the code it ran, typed text and all.
            return f"await page.fill('{arguments['text']}')", False
        if tool == "browser_snapshot" and self.echo:
            self.calls.append((tool, arguments))
            return f"- Page URL: {self.at}\n{self.text}\n- textbox \"Password\" [ref=e6]: {self.echo}", False
        return super().call(tool, arguments)


def session(browser=None, **changes):
    return SignUpSession((browser or FakeBrowser()).access(), "birdforum.net", changes.get("details", DETAILS))


class SiteTests(unittest.TestCase):
    def test_a_site_is_its_registered_name_whatever_the_subdomain(self):
        self.assertEqual(site_of("https://www.birdforum.net/register"), "birdforum.net")
        self.assertEqual(site_of("https://accounts.app.birdforum.net"), "birdforum.net")
        self.assertEqual(site_of("https://shop.example.co.uk/join"), "example.co.uk")
        self.assertEqual(site_of("https://www.example.com.au"), "example.com.au")
        # A shared suffix (wa.gov.au, github.io) is never a site: each council or page is its own.
        self.assertEqual(site_of("https://www.cockburn.wa.gov.au/newsletters"), "cockburn.wa.gov.au")
        self.assertFalse(on_site("https://www.perth.wa.gov.au/", "cockburn.wa.gov.au"))
        self.assertIsNone(site_of("https://wa.gov.au/"))
        self.assertEqual(site_of("https://someone.github.io/x"), "someone.github.io")
        self.assertIsNone(site_of("ftp://birdforum.net"))
        self.assertIsNone(site_of("not an address"))
        self.assertTrue(on_site("https://login.birdforum.net/x", "birdforum.net"))
        self.assertFalse(on_site("https://birdforum.net.evil.com/x", "birdforum.net"))


class PasswordTests(unittest.TestCase):
    def test_her_one_password_is_kept_in_the_credential_store_and_can_be_forgotten(self):
        with patch("sign_ups.password.keyring") as keyring:
            keyring.get_password.return_value = None
            self.assertEqual(sign_up_password(), "")
            self.assertEqual(save_sign_up_password(f"  {PASSWORD}  "), PASSWORD)
            keyring.set_password.assert_called_once_with("cora", PASSWORD_ACCOUNT, PASSWORD)
            with self.assertRaises(ValueError):
                save_sign_up_password("short")
            with self.assertRaises(ValueError):
                save_sign_up_password("a long password\nwith a break")
            self.assertEqual(keyring.set_password.call_count, 1)
            self.assertEqual(save_sign_up_password(""), "")
            keyring.delete_password.assert_called_once_with("cora", PASSWORD_ACCOUNT)

    def test_the_name_she_signs_up_with_is_hers_to_choose_and_just_cora_until_then(self):
        with patch("sign_ups.password.keyring") as keyring:
            keyring.get_password.return_value = None
            self.assertEqual(sign_up_name(), "Cora")
            self.assertEqual(save_sign_up_name("  Cora   Vale "), "Cora Vale")
            keyring.set_password.assert_called_once_with("cora", NAME_ACCOUNT, "Cora Vale")
            with self.assertRaises(ValueError):
                save_sign_up_name("x" * 101)
            self.assertEqual(save_sign_up_name(""), "Cora")
            keyring.delete_password.assert_called_once_with("cora", NAME_ACCOUNT)


class SessionTests(unittest.TestCase):
    def test_she_opens_pages_on_the_site_only(self):
        browser = FakeBrowser()
        # A name on the site that leads to the local network is still refused.
        browser.refuse = lambda url: "Pages on this PC or the local network can't be opened." if "intranet" in url else None
        current = session(browser)
        page = current.run("open_page", json.dumps({"url": SITE_URL}))
        self.assertIn('[textbox "Email" ref=e5]', page)
        self.assertIn('[checkbox "Send me offers" checked ref=e7]', page)
        self.assertIn("Only pages on birdforum.net", current.run("open_page", json.dumps({"url": "https://evil.com"})))
        self.assertIn("local network", current.run("open_page", json.dumps({"url": "https://intranet.birdforum.net/"})))
        self.assertEqual(browser.navigated(), [SITE_URL])
        self.assertEqual(current.opened, [SITE_URL])

    def test_a_click_that_leads_off_the_site_is_closed(self):
        browser = FakeBrowser(clicks={"e8": "https://accounts.google.com/signin"})
        current = session(browser)
        current.run("open_page", json.dumps({"url": SITE_URL}))
        reply = current.run("click", json.dumps({"ref": "e8", "what": "Sign in with Google"}))
        self.assertIn("led off birdforum.net", reply)
        self.assertEqual(browser.navigated()[-1], "about:blank")
        self.assertIsNone(current.current)
        self.assertIn(("browser_click", {"target": "e8", "element": "Sign in with Google"}), browser.calls)

    def test_her_password_is_typed_but_never_shown_back(self):
        browser = FakeBrowser(echo=PASSWORD)
        current = session(browser)
        current.run("open_page", json.dumps({"url": SITE_URL}))
        reply = current.run("type", json.dumps({"ref": "e6", "what": "Password", "text": "{password}", "submit": False}))
        typed = [arguments for tool, arguments in browser.calls if tool == "browser_type"]
        self.assertEqual(typed, [{"target": "e6", "element": "Password", "text": PASSWORD, "submit": False}])
        self.assertNotIn(PASSWORD, reply)
        self.assertIn("{password}", reply)
        current.run("type", json.dumps({"ref": "e5", "what": "Email", "text": "{email}", "submit": True}))
        self.assertEqual(browser.calls[-2][1]["text"], "cora@intelligis.io")

    def test_the_persons_details_and_card_numbers_are_never_typed(self):
        browser = FakeBrowser()
        current = session(browser)
        for typed in ("nick@example.com", "Nick", "call 0400 123 456", "4111 1111 1111 1111"):
            reply = current.run("type", json.dumps({"ref": "e5", "what": "Field", "text": typed, "submit": False}))
            self.assertIn(reply, (NOT_YOURS, NOT_A_CARD))
        self.assertEqual(current.run("select", json.dumps({"ref": "e5", "what": "Name", "option": "Nick"})), NOT_YOURS)
        self.assertNotIn("browser_type", [tool for tool, _ in browser.calls])
        # Her own name and a username based on it are fine.
        current.run("type", json.dumps({"ref": "e5", "what": "Username", "text": "cora_nickel", "submit": False}))
        self.assertIn("browser_type", [tool for tool, _ in browser.calls])

    def test_a_page_asking_for_card_details_ends_the_session(self):
        browser = FakeBrowser(text=CARD_FORM)
        current = session(browser)
        self.assertEqual(current.run("open_page", json.dumps({"url": SITE_URL})), PAYMENT_STOP)
        self.assertTrue(current.out_of_room)
        self.assertEqual(current.run("click", json.dumps({"ref": "e9", "what": "Card"})), PAYMENT_STOP)
        self.assertNotIn("browser_click", [tool for tool, _ in browser.calls])


class SignUpTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()

    def replies(self, *replies):
        self.client.responses.create.side_effect = list(replies)

    def run_sign_up(self, browser):
        return sign_up(self.client, "m", {"url": SITE_URL, "what": "A birding forum", "why": "To ask about owls"},
                       "birdforum.net", DETAILS, browser.access(), clock=lambda: 0.0)

    def test_she_fills_in_the_form_and_says_how_it_went_without_ever_seeing_the_password(self):
        browser = FakeBrowser(echo=PASSWORD)
        call, response, answer = browse_tests.call, browse_tests.response, browse_tests.answer
        self.replies(
            response(call("open_page", 1, url=SITE_URL)),
            response(call("type", 2, ref="e5", what="Email", text="{email}", submit=False)),
            response(call("type", 3, ref="e6", what="Password", text="{password}", submit=False)),
            response(call("click", 4, ref="e7", what="Send me offers")),
            response(call("click", 5, ref="e8", what="Create account")),
            response(answer({"status": "confirm_email", "blocked_by": None, "note": f"Check mail. {PASSWORD}"})),
        )
        outcome = self.run_sign_up(browser)
        # The page she ended on comes back, for the person to pick up from if need be.
        self.assertEqual(outcome, Outcome(CONFIRM_EMAIL, None, "Check mail. {password}", 5, SITE_URL))
        self.assertEqual(browser.calls[-1], ("browser_close", {}))
        requests = [item.kwargs for item in self.client.responses.create.call_args_list]
        self.assertEqual(requests[0]["tools"], TOOLS)
        self.assertEqual(requests[0]["text"]["format"]["schema"], SCHEMA)
        self.assertNotIn(PASSWORD, json.dumps([request["input"] for request in requests], default=str))
        self.assertNotIn(PASSWORD, requests[0]["instructions"])
        # Signing up to a newsletter means subscribing to it; only extras are unticked.
        self.assertIn("subscribing to it is the point", requests[0]["instructions"])
        self.assertEqual({tool["name"] for tool in TOOLS}, {"open_page", "click", "type", "select"})
        self.assertTrue({"browser_click", "browser_type", "browser_select_option"} <= SIGN_UP_TOOLS)

    def test_a_card_field_makes_it_blocked_by_payment_whatever_she_says(self):
        browser = FakeBrowser(text=CARD_FORM)
        call, response, answer = browse_tests.call, browse_tests.response, browse_tests.answer
        self.replies(
            response(call("open_page", 1, url=SITE_URL)),
            response(answer({"status": "done", "blocked_by": None, "note": "All set."})),
        )
        outcome = self.run_sign_up(browser)
        self.assertEqual((outcome.status, outcome.blocked_by), (BLOCKED, "payment"))
        # Told she's out of room, and offered no tools for the last turn.
        self.assertEqual(self.client.responses.create.call_args_list[-1].kwargs["tool_choice"], "none")

    def test_blocked_without_a_reason_is_other_and_the_limits_are_hers(self):
        self.replies(browse_tests.response(browse_tests.answer({"status": "blocked", "blocked_by": None, "note": ""})))
        outcome = self.run_sign_up(FakeBrowser())
        self.assertEqual((outcome.status, outcome.blocked_by), (BLOCKED, "other"))
        self.assertEqual((MAX_ACTIONS, SIGN_UP_SECONDS), (30, 300))


class FakeConnection:
    def __init__(self):
        self.response = MagicMock()
        self.response.item.create = AsyncMock()
        self.response.create = AsyncMock()

    def output(self):
        return json.loads(self.response.item.create.await_args.kwargs["item"]["output"])


class ToolTests(unittest.IsolatedAsyncioTestCase):
    def tool(self, problem=None, outcome=APPROVED):
        self.connection = FakeConnection()
        self.check = AsyncMock(return_value=(None, problem) if problem else ("birdforum.net", None))
        self.confirm = AsyncMock(return_value=outcome)
        self.begin = AsyncMock(return_value="" if outcome != APPROVED else "Signing up now.")
        return RequestSignUpTool(lambda: self.connection, lambda: False, MagicMock(), self.check, self.confirm,
                                 self.begin)

    async def handle(self, tool):
        await tool.handle("call_1", {"site_url": SITE_URL, "what": "A birding forum", "why": "To ask about owls"})
        return self.connection.output()

    async def test_she_is_told_why_not_before_the_person_is_asked(self):
        result = await self.handle(self.tool(problem="You don't have a sign-up password yet."))
        self.assertEqual(result, {"status": "error", "message": "You don't have a sign-up password yet."})
        self.confirm.assert_not_awaited()
        self.begin.assert_not_awaited()

    async def test_the_person_sees_what_and_why_and_their_answer_decides(self):
        result = await self.handle(self.tool())
        self.assertEqual(result, {"status": "ok", "message": "Signing up now."})
        args = self.confirm.await_args
        self.assertEqual(args.args[:2], ("sign_up", "request_sign_up"))
        self.assertEqual(args.args[2]["site"], SITE_URL)
        self.assertEqual(args.args[2]["why Cora wants it"], "To ask about owls")
        self.assertIn("terms", args.args[2]["means"])
        self.begin.assert_awaited_once_with(SITE_URL, "birdforum.net", "A birding forum", "To ask about owls", APPROVED)
        for outcome in (DECLINED, TIMED_OUT):
            result = await self.handle(self.tool(outcome=outcome))
            self.assertEqual(result, {"status": "not_done", "message": NOT_DONE[outcome]})
            self.assertEqual(self.begin.await_args.args[-1], outcome)

    def test_arguments_are_checked(self):
        with self.assertRaises(ValueError):
            validate_arguments({"site_url": SITE_URL, "what": "", "why": "x"})
        with self.assertRaises(ValueError):
            validate_arguments({"site_url": SITE_URL, "what": "x"})


class AppSignUpTests(unittest.IsolatedAsyncioTestCase):
    def ready(self, app):
        app.email_status = EmailStatus(state=READY, address="cora@intelligis.io")
        app.persistence.sign_up_holding.return_value = None
        app.persistence.sign_ups_asked_today.return_value = 0
        app.exploration_browser = MagicMock(return_value=MagicMock())

    async def test_she_may_ask_only_with_email_password_browser_and_no_account_there(self):
        async with keyboard_tests.running_app() as (app, pilot):
            with patch("app_areas.sign_ups.passwords.sign_up_password", return_value=PASSWORD):
                self.assertIn("isn't set up", (await app.check_sign_up(SITE_URL))[1])
                self.ready(app)
                self.assertEqual(await app.check_sign_up(SITE_URL), ("birdforum.net", None))
                self.assertIsNotNone((await app.check_sign_up("http://localhost:3000/join"))[1])
                self.assertIn("https://", (await app.check_sign_up("birdforum"))[1])
                app.persistence.sign_ups_asked_today.return_value = DAILY_LIMIT
                self.assertIn("enough sites for today", (await app.check_sign_up(SITE_URL))[1])
                app.persistence.sign_up_holding.return_value = MagicMock()
                self.assertIn("already have an account", (await app.check_sign_up(SITE_URL))[1])
                app.persistence.sign_up_holding.return_value = None
                app.persistence.sign_ups_asked_today.return_value = 0
                app.sign_up_worker = MagicMock(is_finished=False)
                self.assertIn("already signing up", (await app.check_sign_up(SITE_URL))[1])
                app.sign_up_worker = None
                app.exploration_browser.return_value = None
                self.assertIn("browser is off", (await app.check_sign_up(SITE_URL))[1])
            with patch("app_areas.sign_ups.passwords.sign_up_password", return_value=""):
                self.assertIn("sign-up password", (await app.check_sign_up(SITE_URL))[1])

    async def test_a_yes_starts_signing_up_with_her_details_and_never_the_persons(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.ready(app)
            app.live.person_name = "Nick"
            app.email_person = "nick@example.com"
            record = SimpleNamespace(id=uuid4(), site="birdforum.net", url=SITE_URL, what="Forum", why="Owls")
            app.persistence.record_sign_up.return_value = record
            app.persistence.load_models.return_value = {"delegation": "big-model"}
            with patch("app_areas.sign_ups.passwords.sign_up_password", return_value=PASSWORD), \
                    patch("app_areas.sign_ups.passwords.sign_up_name", return_value="Cora Vale"), \
                    patch.object(app, "run_sign_up", return_value=MagicMock(is_finished=False)) as run:
                message = await app.begin_sign_up(SITE_URL, "birdforum.net", "Forum", "Owls", APPROVED)
            self.assertIn("don't say it's done", message)
            app.persistence.record_sign_up.assert_called_once_with("birdforum.net", SITE_URL, "Forum", "Owls",
                                                                   SIGNING_UP)
            got, details, model, _ = run.call_args.args
            self.assertEqual((got, model), (record, "big-model"))
            self.assertEqual((details.name, details.email, details.password),
                             ("Cora Vale", "cora@intelligis.io", PASSWORD))
            self.assertEqual(details.forbidden, ("Nick", "nick@example.com"))
            self.assertTrue(app.signing_up())

    async def test_a_no_is_recorded_and_nothing_starts(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.ready(app)
            with patch.object(app, "run_sign_up") as run:
                self.assertEqual(await app.begin_sign_up(SITE_URL, "birdforum.net", "Forum", "Owls", DECLINED), "")
                app.persistence.record_sign_up.side_effect = IntegrityError("x", {}, Exception())
                self.assertIn("already have", await app.begin_sign_up(SITE_URL, "birdforum.net", "F", "O", APPROVED))
            run.assert_not_called()
            self.assertEqual(app.persistence.record_sign_up.call_args_list[0].args[-1], "declined")

    async def test_how_it_went_is_saved_and_told_and_blocked_asks_the_person_for_help(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.ready(app)
            app.tell_person = AsyncMock()
            record = SimpleNamespace(id=uuid4(), site="birdforum.net", url=SITE_URL, what="Forum", why="Owls")
            outcome = Outcome(BLOCKED, "captcha", "A picture puzzle.", 3, SITE_URL)
            browser = BrowserAccess(lambda tool, arguments: ("", False))
            app.exploration_browser = MagicMock(return_value=browser)
            with patch("app_areas.sign_ups.sign_up", return_value=outcome) as signing, \
                    patch("app_areas.sign_ups.OpenAI"):
                worker = app.run_sign_up(record, DETAILS, "m", asyncio.get_running_loop())
                await asyncio.wait_for(worker.wait(), 5)
                await app.workers.wait_for_complete()
            self.assertEqual(signing.call_args.args[2:5], ({"url": SITE_URL, "what": "Forum", "why": "Owls"},
                                                           "birdforum.net", DETAILS))
            app.persistence.finish_sign_up.assert_called_once_with(record.id, BLOCKED, "captcha", "A picture puzzle.")
            told = app.tell_person.await_args.args[0]
            self.assertIn("a captcha I couldn't get past", told)
            self.assertIn(f"The form is at {SITE_URL}.", told)
            self.assertIn("cora@intelligis.io", told)
            self.assertIn("sign-up password from Settings", told)
            self.assertNotIn(PASSWORD, told)
            self.assertIn("Sign-up to birdforum.net: blocked.", "\n".join(app.activity.entries))
            self.assertIn("I've signed up to birdforum.net", app.sign_up_news(record, Outcome(DONE, None, "", 1)))
            record.what = "City news email newsletter."
            self.assertIn("(City news email newsletter) with",
                          app.sign_up_news(record, Outcome(DONE, None, "", 1)))

    async def test_she_knows_her_sign_ups_in_the_persons_chats_but_never_a_clients(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.persistence.load_sign_ups_context.return_value = (
                '[{"site": "cockburn.wa.gov.au", "status": "blocked", "blocked_by": "captcha"}]'
            )
            sections = await app.person_sections()
            self.assertIn("cockburn.wa.gov.au", sections["sign_ups"])
            self.assertIn("not tasks", sections["sign_ups"])
            self.assertIn("request_sign_up", sections["sign_ups"])
            client = SimpleNamespace(id=uuid4(), name="Sarah")
            app.persistence.load_client_memory_context.return_value = ""
            app.persistence.load_client_people_context.return_value = ""
            self.assertNotIn("sign_ups", await app.client_sections(client))

    def test_her_instructions_say_what_she_can_do_and_when_the_browser_beats_web_search(self):
        for words in ("send email from your own inbox", "request_sign_up", "fares or prices for exact dates",
                      "Run the site's own search rather than quoting advertised prices"):
            self.assertIn(words, BACKEND_INSTRUCTIONS)

    async def test_explorations_leave_the_browser_to_a_sign_up(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.sign_up_worker = MagicMock(is_finished=False)
            with patch.object(app, "can_explore", return_value=True), \
                    patch.object(app, "explore_interest") as explore:
                app.check_exploration()
            explore.assert_not_called()

    async def test_her_sign_up_password_is_set_in_settings_and_never_kept_in_the_field(self):
        async with keyboard_tests.running_app() as (app, pilot):
            screen = await email_tests.EmailSettingsTests.open_settings(self, app, pilot)
            await pilot.click("#service-title-email")
            await pilot.pause()
            field = screen.query_one("#email-sign-up-password", Input)
            self.assertTrue(field.password)
            self.assertEqual(field.placeholder, "at least 12 characters")
            field.value = PASSWORD
            with patch("app_areas.sign_ups.passwords.save_sign_up_password", return_value=PASSWORD) as save:
                screen.query_one("#email-save-sign-up-password", Button).press()
                await pilot.pause()
                await app.workers.wait_for_complete()
            save.assert_called_once_with(PASSWORD)
            self.assertEqual(field.value, "")
            self.assertEqual(field.placeholder, "password saved · type to replace")
            self.assertTrue(app.email_controls.has_sign_up_password)
            self.assertNotIn(PASSWORD, "\n".join(app.activity.entries))
            name = screen.query_one("#email-sign-up-name", Input)
            self.assertEqual(name.value, "Cora")
            name.value = "Cora Vale"
            with patch("app_areas.sign_ups.passwords.save_sign_up_name", return_value="Cora Vale") as save:
                screen.query_one("#email-save-sign-up-name", Button).press()
                await pilot.pause()
                await app.workers.wait_for_complete()
            save.assert_called_once_with("Cora Vale")
            self.assertEqual((app.sign_up_name, app.email_controls.sign_up_name), ("Cora Vale", "Cora Vale"))


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class SignUpStoreDatabaseTests(unittest.TestCase):
    setUpClass = classmethod(summary_tests.SummaryDatabaseTests.setUpClass.__func__)

    def setUp(self):
        # Only this disposable test database is cleared, never Cora's database.
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM public.sign_ups"))
        self.store = SignUpStore(self.engine)
        self.now = datetime(2026, 11, 1, 9, tzinfo=timezone.utc)

    def test_one_account_per_site_and_how_it_went(self):
        declined = self.store.record("birdforum.net", SITE_URL, "Forum", "Owls", "declined", self.now)
        self.assertIsNotNone(declined.finished_at)
        self.assertIsNone(self.store.holding("birdforum.net", self.now))  # a no holds nothing
        started = self.store.record("birdforum.net", SITE_URL, "Forum", "Owls", SIGNING_UP, self.now)
        self.assertEqual(self.store.holding("birdforum.net", self.now).id, started.id)
        with self.assertRaises(IntegrityError):
            self.store.record("birdforum.net", SITE_URL, "Forum", "Owls", SIGNING_UP, self.now)
        self.store.finish(started.id, CONFIRM_EMAIL, self.now, note="  Check   your mail ")
        held = self.store.holding("birdforum.net", self.now)
        self.assertEqual((held.status, held.note, held.finished_at), (CONFIRM_EMAIL, "Check your mail", None))
        with self.assertRaises(ValueError):
            self.store.finish(started.id, BLOCKED, self.now)  # blocked says by what
        self.assertEqual(self.store.asked_today(self.now), 2)
        self.assertEqual(self.store.asked_today(self.now + timedelta(days=1, minutes=1)), 0)
        # The person's no isn't listed on the dashboard, nor in her conversations.
        self.assertEqual([sign_up.status for sign_up in self.store.recent()], [CONFIRM_EMAIL])
        self.assertEqual(json.loads(self.store.context()), [{
            "site": "birdforum.net", "what": "Forum", "status": CONFIRM_EMAIL, "asked": "2026-11-01",
            "note": "Check your mail",
        }])

    def test_a_blocked_sign_up_frees_the_site_and_a_stale_one_fails(self):
        started = self.store.record("birdforum.net", SITE_URL, "Forum", "Owls", SIGNING_UP, self.now)
        self.store.finish(started.id, BLOCKED, self.now, blocked_by="captcha", note="Puzzle")
        self.assertIsNone(self.store.holding("birdforum.net", self.now))
        asked = self.now + timedelta(minutes=1)
        stuck = self.store.record("birdforum.net", SITE_URL, "Forum", "Owls", SIGNING_UP, asked)
        later = asked + STALE_AFTER + timedelta(minutes=1)
        self.assertIsNone(self.store.holding("birdforum.net", later))
        self.assertEqual(self.store.recent()[0].id, stuck.id)
        self.assertEqual(self.store.recent()[0].status, FAILED)


if __name__ == "__main__":
    unittest.main()
