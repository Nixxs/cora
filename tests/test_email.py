"""Cora's own mailbox (E1): set up in Settings, read-only, every message untrusted. Mocked APIs only."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from textual.widgets import Button, Input, Static, TabbedContent

from mail import account
from mail.account import SCOPES, EmailAccount, EmailNotReady, EmailStatus, Mailbox, access_token
from session_config import BACKEND_MODEL, MODEL_AREAS
from storage.signal_settings_store import SignalSettings
from tools.check_email import SCHEMA, CheckEmailTool, validate_arguments
from ui.model_settings import ModelSettingsScreen
import test_keyboard_persistence as keyboard_tests

APP_ID = "1a2b3c4d-0000-4000-8000-0123456789ab"
MESSAGE = {
    "id": "m1", "from": {"emailAddress": {"name": "Driving School", "address": "info@school.example"}},
    "subject": "Lesson times", "receivedDateTime": "2026-10-30T01:02:03Z",
    "bodyPreview": "We have   Tuesdays\nfree. " + "x" * 400, "isRead": False,
}


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self.body = body

    def json(self):
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self.sent = []

    def __call__(self, **options):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method, url, params=None, json=None, headers=None):
        self.calls.append((url, params, headers))
        self.sent.append((method, url, json))
        return self.responses.pop(0)


class SignInTests(unittest.TestCase):
    def test_reading_and_sending_asked_for_once(self):
        self.assertEqual(set(SCOPES), {"User.Read", "Mail.ReadWrite", "Mail.Send"})

    def test_a_token_comes_from_the_saved_sign_in_or_says_how_to_sign_in(self):
        app = MagicMock()
        app.get_accounts.return_value = []
        with self.assertRaises(EmailNotReady) as caught:
            access_token(app)
        self.assertIn("Settings > Services > Email", str(caught.exception))
        app.get_accounts.return_value = ["cora"]
        app.acquire_token_silent.return_value = None
        with self.assertRaisesRegex(EmailNotReady, "lapsed"):
            access_token(app)
        app.acquire_token_silent.return_value = {"access_token": "tok"}
        self.assertEqual(access_token(app), "tok")
        app.acquire_token_silent.assert_called_with(SCOPES, account="cora")

    def test_the_app_id_is_kept_in_the_credential_store_with_env_as_a_fallback(self):
        store = {}
        with patch.object(account, "keyring") as keyring:
            keyring.get_password.side_effect = lambda service, name: store.get((service, name))
            keyring.set_password.side_effect = lambda service, name, value: store.__setitem__((service, name), value)
            email = EmailAccount()
            with patch.dict("os.environ", {"CORA_EMAIL_CLIENT_ID": " "}):
                self.assertIsNone(email.client_id())
                self.assertFalse(email.signed_in())
                with self.assertRaisesRegex(EmailNotReady, "isn't set up"):
                    email.token()
            with patch.dict("os.environ", {"CORA_EMAIL_CLIENT_ID": "from-env"}):
                self.assertEqual(email.client_id(), "from-env")
                with self.assertRaises(ValueError):
                    email.save_client_id("not an id")
                self.assertEqual(email.save_client_id(f" {APP_ID.upper()} "), APP_ID)
                self.assertEqual(email.client_id(), APP_ID)

    def test_personal_accounts_by_default_or_her_organisations_directory(self):
        store = {}
        with patch.object(account, "keyring") as keyring:
            keyring.get_password.side_effect = lambda service, name: store.get((service, name))
            keyring.set_password.side_effect = lambda service, name, value: store.__setitem__((service, name), value)
            def delete(service, name):
                if store.pop((service, name), None) is None:
                    raise account.PasswordDeleteError()
            keyring.delete_password.side_effect = delete
            email = EmailAccount()
            with patch.dict("os.environ", {"CORA_EMAIL_TENANT": ""}):
                self.assertEqual(email.authority(), "https://login.microsoftonline.com/consumers")
                self.assertEqual(email.save_tenant(" Intelligis.IO "), "intelligis.io")
                self.assertEqual(email.authority(), "https://login.microsoftonline.com/intelligis.io")
                self.assertEqual(email.save_tenant(APP_ID.upper()), APP_ID)
                for bad in ("not a domain", "https://intelligis.io", "x" * 300 + ".io"):
                    with self.subTest(tenant=bad), self.assertRaises(ValueError):
                        email.save_tenant(bad)
                self.assertEqual(email.save_tenant(""), "")
                self.assertEqual(email.tenant(), "")
                self.assertEqual(email.save_tenant(""), "")  # nothing saved, nothing to forget

    def test_signing_in_with_a_code_and_cancelling_a_wait(self):
        email = EmailAccount()
        app = MagicMock()
        app.initiate_device_flow.return_value = {"user_code": "ABCD", "verification_uri": "https://microsoft.com/link"}
        app.acquire_token_by_device_flow.return_value = {
            "access_token": "tok", "id_token_claims": {"preferred_username": "cora@outlook.com"}}
        with patch.object(EmailAccount, "app", return_value=app):
            flow = email.start_sign_in()
            app.initiate_device_flow.assert_called_once_with(scopes=SCOPES)
            email.cancel_sign_in()
            self.assertEqual(flow["expires_at"], 0)
            self.assertEqual(email.finish_sign_in(flow), "cora@outlook.com")
            app.acquire_token_by_device_flow.return_value = {"error": "expired_token", "error_description": "Expired"}
            with self.assertRaisesRegex(EmailNotReady, "Expired"):
                email.finish_sign_in(flow)
            app.initiate_device_flow.return_value = {"error": "invalid_client"}
            with self.assertRaisesRegex(EmailNotReady, "invalid_client"):
                email.start_sign_in()

    def test_a_new_sign_in_replaces_any_earlier_one(self):
        email = EmailAccount()
        app = MagicMock()
        mine, earlier = {"home_account_id": "o1.t1"}, {"home_account_id": "o2.t1"}
        app.get_accounts.return_value = [earlier, mine]
        app.acquire_token_by_device_flow.return_value = {
            "access_token": "tok", "id_token_claims": {"oid": "o1", "tid": "t1", "preferred_username": "cora@x.io"}}
        with patch.object(EmailAccount, "app", return_value=app):
            self.assertEqual(email.finish_sign_in({}), "cora@x.io")
        app.remove_account.assert_called_once_with(earlier)

    def test_signing_out_forgets_her_sign_in(self):
        email = EmailAccount()
        app = MagicMock()
        app.get_accounts.return_value = ["cora"]
        with patch.object(EmailAccount, "app", return_value=app), \
                patch.object(EmailAccount, "client_id", return_value=APP_ID):
            email.sign_out()
        app.remove_account.assert_called_once_with("cora")


class MailboxTests(unittest.IsolatedAsyncioTestCase):
    async def test_newest_inbox_mail_is_read_with_the_token_and_nothing_marked_read(self):
        client = FakeClient([FakeResponse(200, {"value": [MESSAGE]})])
        mailbox = Mailbox(lambda: "tok", client)
        [mail] = await mailbox.recent(5, unread_only=True)
        url, params, headers = client.calls[0]
        self.assertTrue(url.endswith("/me/mailFolders/inbox/messages"))
        self.assertEqual((params["$top"], params["$filter"]), ("5", "isRead eq false"))
        self.assertEqual(headers, {"Authorization": "Bearer tok"})
        self.assertEqual((mail.sender_name, mail.sender_address, mail.subject, mail.unread),
                         ("Driving School", "info@school.example", "Lesson times", True))
        self.assertTrue(mail.preview.startswith("We have Tuesdays free. x"))
        self.assertEqual(len(mail.preview), 300)
        self.assertEqual(mail.received_at.isoformat(), "2026-10-30T01:02:03+00:00")

    async def test_a_refused_sign_in_says_to_sign_in_again(self):
        mailbox = Mailbox(lambda: "tok", FakeClient([FakeResponse(401, {})]))
        with self.assertRaisesRegex(EmailNotReady, "sign her in again"):
            await mailbox.address()

    async def test_other_refusals_carry_microsofts_error_code_only(self):
        mailbox = Mailbox(lambda: "tok", FakeClient([
            FakeResponse(404, {"error": {"code": "MailboxNotEnabledForRESTAPI", "message": "mail text"}})]))
        with self.assertRaises(account.GraphError) as caught:
            await mailbox.recent()
        self.assertEqual(str(caught.exception), "Microsoft said HTTP 404 (MailboxNotEnabledForRESTAPI).")

    async def test_her_address_is_her_own(self):
        client = FakeClient([FakeResponse(200, {"mail": None, "userPrincipalName": "cora@outlook.com"})])
        self.assertEqual(await Mailbox(lambda: "tok", client).address(), "cora@outlook.com")


class CheckEmailToolTests(unittest.IsolatedAsyncioTestCase):
    def tool(self, mailbox):
        self.connection = MagicMock()
        self.connection.response.item.create = AsyncMock()
        self.connection.response.create = AsyncMock()
        return CheckEmailTool(lambda: self.connection, lambda: False, MagicMock(), lambda: mailbox)

    def output(self):
        return json.loads(self.connection.response.item.create.call_args.kwargs["item"]["output"])

    def test_schema_is_strict(self):
        self.assertTrue(SCHEMA["strict"])
        validate_arguments({"unread_only": False})
        for bad in ({}, {"unread_only": "yes"}, {"unread_only": True, "limit": 3}):
            with self.subTest(arguments=bad), self.assertRaises(ValueError):
                validate_arguments(bad)

    async def test_mail_is_listed_and_marked_untrusted(self):
        mailbox = Mailbox(lambda: "tok", FakeClient([
            FakeResponse(200, {"mail": "cora@outlook.com"}), FakeResponse(200, {"value": [MESSAGE]}),
        ]))
        await self.tool(mailbox).handle("c1", {"unread_only": False})
        output = self.output()
        self.assertEqual((output["status"], output["your_address"]), ("ok", "cora@outlook.com"))
        self.assertIn("Untrusted", output["note"])
        self.assertEqual(output["messages"][0]["from"], "Driving School <info@school.example>")
        self.connection.response.create.assert_awaited_once()

    async def test_not_set_up_not_signed_in_or_failing_is_said_plainly(self):
        await self.tool(None).handle("c1", {"unread_only": False})
        self.assertEqual(self.output()["status"], "not_set_up")
        lapsed = MagicMock()
        lapsed.address = AsyncMock(side_effect=EmailNotReady("Cora's email sign-in has lapsed"))
        await self.tool(lapsed).handle("c1", {"unread_only": False})
        self.assertEqual(self.output(), {"status": "not_ready", "message": "Cora's email sign-in has lapsed"})
        broken = MagicMock()
        broken.address = AsyncMock(side_effect=RuntimeError("secret body"))
        await self.tool(broken).handle("c1", {"unread_only": False})
        self.assertNotIn("secret", json.dumps(self.output()))


class EmailAppTests(unittest.IsolatedAsyncioTestCase):
    async def test_her_chats_with_you_offer_it(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.assertIn("check_email", [schema["name"] for schema in app.live.tool_schemas()])

    async def test_launch_says_whether_her_mailbox_is_ready(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.email_account.client_id.return_value = None
            await app.check_email_account().wait()
            self.assertEqual(app.email_status, EmailStatus(account.NO_APP))
            app.email_account.client_id.return_value = APP_ID
            app.email_account.signed_in.return_value = False
            await app.check_email_account().wait()
            self.assertEqual(app.email_status.state, account.SIGNED_OUT)
            app.email_account.signed_in.return_value = True
            app.mailbox = MagicMock()
            app.mailbox.address = AsyncMock(return_value="cora@outlook.com")
            await app.check_email_account().wait()
            self.assertEqual(app.email_status, EmailStatus(account.READY, address="cora@outlook.com"))
            app.mailbox.address = AsyncMock(side_effect=EmailNotReady("Cora's email sign-in has lapsed"))
            await app.check_email_account().wait()
            self.assertEqual(app.email_status.reason, "Cora's email sign-in has lapsed")
            log = "\n".join(app.activity.entries)
            self.assertIn("Cora's email is ready: cora@outlook.com", log)
            self.assertIn("Cora's email sign-in has lapsed", log)

    async def test_signing_in_shows_the_code_then_checks_her_mailbox(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.email_account.start_sign_in.return_value = {
                "user_code": "ABCD", "verification_uri": "https://microsoft.com/link"}
            app.email_account.finish_sign_in.return_value = "cora@outlook.com"
            app.check_email_account = MagicMock()
            await app.sign_in_email().wait()
            self.assertIn("signed in as cora@outlook.com", "\n".join(app.activity.entries))
            app.check_email_account.assert_called_once()
            app.email_account.finish_sign_in.side_effect = EmailNotReady("Sign-in didn't finish: Expired")
            await app.sign_in_email().wait()
            self.assertEqual(app.email_status, EmailStatus(account.SIGNED_OUT, reason="Sign-in didn't finish: Expired"))

    async def test_a_bad_app_id_is_refused_and_a_good_one_saved_with_her_directory(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.check_email_account = MagicMock()
            controls = app.make_email_controls(False)
            app.email_account.save_client_id.side_effect = ValueError("That isn't an Application (client) ID")
            await app.save_email_app("nope", "").wait()
            self.assertFalse(controls.has_client_id)
            app.email_account.save_client_id.side_effect = None
            app.email_account.save_tenant.return_value = "intelligis.io"
            await app.save_email_app(APP_ID, "Intelligis.io").wait()
            app.email_account.save_tenant.assert_called_with("Intelligis.io")
            self.assertEqual((controls.has_client_id, controls.tenant), (True, "intelligis.io"))
            self.assertIn("work account at intelligis.io", "\n".join(app.activity.entries))
            app.check_email_account.assert_called_once()
            # A blank app ID keeps the saved one.
            app.email_account.save_client_id.reset_mock()
            await app.save_email_app(" ", "").wait()
            app.email_account.save_client_id.assert_not_called()

    async def test_signing_out(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await app.sign_out_email().wait()
            app.email_account.sign_out.assert_called_once()
            self.assertEqual(app.email_status, EmailStatus(account.SIGNED_OUT))

    async def test_closing_stops_a_sign_in_waiting_for_its_code(self):
        async with keyboard_tests.running_app() as (app, pilot):
            email_account = app.email_account
        email_account.cancel_sign_in.assert_called()


class EmailSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def open_settings(self, app, pilot):
        app.persistence.load_models.return_value = {area: BACKEND_MODEL for area in MODEL_AREAS}
        app.persistence.load_signal_settings.return_value = SignalSettings()
        app.persistence.load_clients.return_value = []
        await pilot.click("#settings-button")
        await asyncio.wait_for(app.workers.wait_for_complete(), 3)
        await pilot.pause()
        self.assertIsInstance(app.screen, ModelSettingsScreen)
        app.screen.query_one(TabbedContent).active = "settings-services"
        await pilot.pause()
        return app.screen

    async def test_the_email_row_shows_where_she_stands_and_the_code_while_signing_in(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.email_account.client_id.return_value = None
            app.email_account.tenant.return_value = ""
            screen = await self.open_settings(app, pilot)
            await screen.show_services()
            self.assertEqual(str(screen.query_one("#email-status", Static).render()), "○ not set up")
            self.assertTrue(screen.query_one("#email-sign-in", Button).disabled)
            app.email_controls.has_client_id = True
            app.email_status = EmailStatus(account.SIGNING_IN, code="ABCD-EFGH", link="https://microsoft.com/link")
            await screen.show_services()
            code = str(screen.query_one("#email-code", Static).render())
            self.assertIn("https://microsoft.com/link", code)
            self.assertIn("ABCD-EFGH", code)
            self.assertIn("Cora's own account", code)
            self.assertEqual(screen.query_one("#email-client-id", Input).placeholder, "app ID saved · paste to replace")
            app.email_status = EmailStatus(account.READY, address="cora@outlook.com")
            await screen.show_services()
            self.assertEqual(str(screen.query_one("#email-status", Static).render()),
                             "● Cora's address · cora@outlook.com")
            self.assertFalse(screen.query_one("#email-code", Static).display)
            self.assertFalse(screen.query_one("#email-sign-out", Button).disabled)

    async def test_the_app_id_and_directory_are_handed_to_the_app(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.email_account.client_id.return_value = None
            app.email_account.tenant.return_value = "intelligis.io"
            screen = await self.open_settings(app, pilot)
            app.email_controls.save_app = MagicMock()
            await pilot.click("#service-title-email")
            await pilot.pause()
            self.assertEqual(screen.query_one("#email-tenant", Input).value, "intelligis.io")
            field = screen.query_one("#email-client-id", Input)
            field.value = APP_ID
            screen.query_one("#email-save-id", Button).press()
            await pilot.pause()
            app.email_controls.save_app.assert_called_once_with(APP_ID, "intelligis.io")
            self.assertEqual(field.value, "")


if __name__ == "__main__":
    unittest.main()
