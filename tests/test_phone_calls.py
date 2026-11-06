"""Phone calls to Cora's number (P1a): only Twilio gets an answer, and only the person gets through."""

from dataclasses import replace
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

import httpx2 as httpx

from phone import twilio
from phone.routes import PhoneAccess, phone_routes
from server.listener import build_app
from storage.webhook_settings_store import WebhookSettings
import test_keyboard_persistence as keyboard_tests

TOKEN = "0123456789abcdef0123456789abcdef"
PUBLIC = "https://cora.example.com"
YOU = "+61400000001"
CALL = [("CallSid", "CA123"), ("From", YOU), ("To", "+61200000000"), ("CallStatus", "ringing")]


class TwilioTests(unittest.TestCase):
    def test_the_signature_matches_twilios_own_example(self):
        # From Twilio's security documentation (and its own libraries' tests).
        params = [("CallSid", "CA1234567890ABCDE"), ("Caller", "+12349013030"), ("Digits", "1234"),
                  ("From", "+12349013030"), ("To", "+18005551212")]
        self.assertEqual(twilio.signature("12345", "https://mycompany.com/myapp.php?foo=1&bar=2", params),
                         "0/KCTR6DLpKmkAf8muzZqo1nDgQ=")

    def test_a_signature_covers_the_address_and_every_field(self):
        url = PUBLIC + twilio.INCOMING_PATH
        signed = twilio.signature(TOKEN, url, CALL)
        self.assertTrue(twilio.valid_signature(TOKEN, url, list(reversed(CALL)), signed))
        self.assertFalse(twilio.valid_signature(TOKEN, url, [*CALL[:1], ("From", "+61499999999"), *CALL[2:]], signed))
        self.assertFalse(twilio.valid_signature(TOKEN, "https://elsewhere.example.com/phone/incoming", CALL, signed))
        self.assertFalse(twilio.valid_signature("f" * 32, url, CALL, signed))
        self.assertFalse(twilio.valid_signature("", url, CALL, signed))
        self.assertFalse(twilio.valid_signature(TOKEN, url, CALL, None))

    def test_only_the_same_full_number_matches(self):
        self.assertTrue(twilio.same_number("+61 400 000 001", YOU))
        for caller in ("+61400000002", "0400000001", "", "anonymous", None):
            self.assertFalse(twilio.same_number(caller, YOU))
        self.assertFalse(twilio.same_number(YOU, None))

    def test_what_twilio_says_is_escaped(self):
        answer = twilio.say_and_hang_up("Fish & chips <now>")
        self.assertIn("<Say language=\"en-AU\">Fish &amp; chips &lt;now&gt;</Say><Hangup/>", answer)

    def test_only_a_twilio_auth_token_is_saved_and_empty_forgets_it(self):
        with patch.object(twilio.keyring, "set_password") as saved, \
                patch.object(twilio.keyring, "delete_password") as deleted:
            self.assertEqual(twilio.save_twilio_token(f" {TOKEN} "), TOKEN)
            saved.assert_called_once_with("cora", twilio.TOKEN_ACCOUNT, TOKEN)
            with self.assertRaises(ValueError):
                twilio.save_twilio_token("not-a-token")
            self.assertEqual(twilio.save_twilio_token(""), "")
            deleted.assert_called_once_with("cora", twilio.TOKEN_ACCOUNT)

    def test_the_listener_runs_for_phone_calls_alone(self):
        self.assertTrue(WebhookSettings(public_url=PUBLIC, phone_enabled=True).listening)
        self.assertFalse(WebhookSettings(phone_enabled=True).listening)


class IncomingRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []

        def on_call(caller, call_id):
            self.calls.append((caller, call_id))
            return twilio.say_and_hang_up("Hello")

        access = PhoneAccess(token=lambda: TOKEN, public_url=lambda: PUBLIC, on_call=on_call)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(phone_routes(access))),
                                        base_url="http://127.0.0.1:8000")

    async def asyncTearDown(self):
        await self.client.aclose()

    def post(self, params, signed_with=TOKEN, content_type="application/x-www-form-urlencoded"):
        headers = {"Content-Type": content_type,
                   "X-Twilio-Signature": twilio.signature(signed_with, PUBLIC + twilio.INCOMING_PATH, params)}
        return self.client.post(twilio.INCOMING_PATH, content=urlencode(params), headers=headers)

    async def test_twilio_signed_calls_get_twiml(self):
        answer = await self.post(CALL)
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(answer.headers["content-type"].split(";")[0], "text/xml")
        self.assertIn("<Say", answer.text)
        self.assertEqual(self.calls, [(YOU, "CA123")])

    async def test_anything_not_signed_by_twilio_is_refused_before_it_is_read(self):
        self.assertEqual((await self.post(CALL, signed_with="f" * 32)).status_code, 403)
        unsigned = await self.client.post(twilio.INCOMING_PATH, content=urlencode(CALL),
                                          headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(unsigned.status_code, 403)
        self.assertEqual((await self.post(CALL, content_type="application/json")).status_code, 415)
        self.assertEqual(self.calls, [])


class PhoneCallAppTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_your_number_gets_through_and_strangers_are_turned_away(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.webhook_settings = WebhookSettings(public_url=PUBLIC, phone_enabled=True)
            app.signal_settings = replace(app.signal_settings, allowed_number=YOU)
            app.activity.write = MagicMock()
            self.assertIn(twilio.TEST_LINE.split(".")[0], app.on_phone_call(YOU, "CA1"))
            stranger = app.on_phone_call("+61499999123", "CA2")
            self.assertIn("I can only take calls from people I already know", stranger)
            # Activity says a stranger called without keeping their whole number.
            logged = str(app.activity.write.call_args_list[-1])
            self.assertIn("…123", logged)
            self.assertNotIn("61499999123", logged)
            self.assertIn("I can only take calls", app.on_phone_call("", "CA3"))
            app.webhook_settings = replace(app.webhook_settings, phone_enabled=False)
            self.assertIn("can't take calls right now", app.on_phone_call(YOU, "CA4"))

    async def test_settings_say_what_is_still_needed(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.webhook_settings = WebhookSettings(public_url=PUBLIC, phone_enabled=True)
            self.assertEqual(app.phone_incoming_url(), "https://cora.example.com/phone/incoming")
            with patch.object(app, "webhook_listening", return_value=True):
                app.twilio_token = ""
                app.signal_settings = replace(app.signal_settings, allowed_number=None)
                line, missing = app.phone_status()
                self.assertEqual(line, "● on · not answering yet")
                self.assertIn("Twilio auth token", missing)
                self.assertIn("your number under Signal", missing)
                app.twilio_token = TOKEN
                app.signal_settings = replace(app.signal_settings, allowed_number=YOU)
                self.assertEqual(app.phone_status(), ("● answering calls", ""))
            app.webhook_settings = WebhookSettings(public_url=PUBLIC)
            self.assertEqual(app.phone_status(), ("", ""))


if __name__ == "__main__":
    unittest.main()
