"""Tools left out until they can be used (every tool offered costs tokens on every request): her email
and calendar without her email, the Signal tools without Signal, and World of Warcraft's unless it's in use."""

from types import SimpleNamespace
import unittest

from app_areas.web_interface import EMAIL_TOOLS, SIGNAL_TOOLS
from backend_chat import GAME_TOOLS
from mail.account import EmailStatus, FAILED, NO_APP, READY, SIGNED_OUT
import test_keyboard_persistence as keyboard_tests


class GateTests(unittest.IsolatedAsyncioTestCase):
    async def test_email_tools_only_once_her_mailbox_is_known_to_be_missing(self):
        async with keyboard_tests.running_app() as (app, pilot):
            # Not checked yet at launch: still offered, so the first session isn't without them.
            app.email_checked, app.email_status = False, EmailStatus(NO_APP)
            self.assertFalse(EMAIL_TOOLS & app.unavailable_tools())
            app.email_checked = True
            for state, missing in ((NO_APP, True), (SIGNED_OUT, True), (READY, False), (FAILED, False)):
                app.email_status = EmailStatus(state)
                self.assertEqual(EMAIL_TOOLS <= app.unavailable_tools(), missing, state)

    async def test_signal_tools_with_signal_and_messaging_the_person_with_their_number(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.signal_settings = SimpleNamespace(enabled=False, account=None, person_recipient=None)
            self.assertTrue(SIGNAL_TOOLS <= app.unavailable_tools())
            app.signal_settings = SimpleNamespace(enabled=True, account="+61400000000", person_recipient=None)
            self.assertEqual(SIGNAL_TOOLS & app.unavailable_tools(), {"message_person_on_signal"})
            app.signal_settings = SimpleNamespace(enabled=True, account="+61400000000", person_recipient="+61411111111")
            self.assertFalse(SIGNAL_TOOLS & app.unavailable_tools())

    async def test_game_tools_while_its_features_are_on_or_its_running(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.assertTrue(GAME_TOOLS <= app.unavailable_tools())
            app.game_watch = True
            self.assertFalse(GAME_TOOLS & app.unavailable_tools())
            app.game_watch = False
            app.wow_running = lambda: True
            self.assertFalse(GAME_TOOLS & app.unavailable_tools())


if __name__ == "__main__":
    unittest.main()
