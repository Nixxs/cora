"""The Twilio region for calls she places, from Settings > Services > Phone calls, replacing .env's
CORA_TWILIO_API. Mocked APIs only."""

import asyncio
import unittest
from unittest.mock import MagicMock, patch

import httpx2 as httpx
from textual.widgets import Select

from phone import twilio
from storage.app_settings_store import TWILIO_REGIONS, AppSettingsStore
from ui.model_settings import ENV_REGION, ModelSettingsScreen, region_options
import test_keyboard_persistence as keyboard_tests
from test_model_settings_screen import MODELS
import test_start_mode


def keep_region(test) -> None:
    """The address is the app's own module setting: put it back after each test."""
    test.enterContext(patch.object(twilio, "API_BASE", twilio.API_BASE))


class RegionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        keep_region(self)

    def test_the_store_keeps_only_known_regions(self):
        self.assertEqual(set(TWILIO_REGIONS), set(twilio.REGIONS))
        store = AppSettingsStore(MagicMock())
        for saved, expected in ((None, None), ("us1", "us1"), ("mars1", None)):
            with self.subTest(saved=saved), patch.object(store, "_get", return_value=saved):
                self.assertEqual(store.twilio_region(), expected)
        with patch.object(store, "_set") as saved:
            store.set_twilio_region("ie1")
            saved.assert_called_once_with("twilio_region", "ie1")
            with self.assertRaises(ValueError):
                store.set_twilio_region("https://example.com")

    async def test_calls_go_through_the_chosen_region(self):
        twilio.set_region("us1")
        self.assertEqual(twilio.region(), "us1")
        seen = []

        def answer(request):
            seen.append(str(request.url))
            return httpx.Response(201, json={"sid": "CA1"})

        await twilio.place_call("AC1", "token", "+61400000000", "+61411111111", "<Response/>",
                                "https://cora.example.com/phone/status", 600, transport=httpx.MockTransport(answer))
        self.assertTrue(seen[0].startswith("https://api.twilio.com/2010-04-01/Accounts/AC1/"))

    def test_an_address_of_its_own_from_env_is_shown_as_such(self):
        twilio.API_BASE = "https://api.example.com"
        self.assertIsNone(twilio.region())
        self.assertEqual(region_options(ENV_REGION, twilio.API_BASE)[-1],
                         ("From .env: https://api.example.com", ENV_REGION))
        self.assertEqual(len(region_options("au1", "")), len(twilio.REGIONS))

    async def test_the_saved_region_replaces_env_at_launch(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.persistence.load_twilio_region.return_value = "ie1"
            await test_start_mode.LaunchTests.launch(self, app, "active")
            self.assertEqual(twilio.API_BASE, "https://api.dublin.ie1.twilio.com")

    async def test_unset_keeps_env(self):
        before = twilio.API_BASE
        async with keyboard_tests.running_app() as (app, pilot):
            await test_start_mode.LaunchTests.launch(self, app, "active")
        self.assertEqual(twilio.API_BASE, before)

    async def test_a_change_in_settings_applies_now_and_is_saved(self):
        twilio.set_region("au1")
        async with keyboard_tests.running_app() as (app, pilot):
            app.persistence.load_models.return_value = MODELS
            await pilot.click("#settings-button")
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            await pilot.pause()
            self.assertIsInstance(app.screen, ModelSettingsScreen)
            # The Services tab is built in the background after the screen shows.
            await asyncio.wait_for(app.screen.all_built.wait(), 5)
            choice = app.screen.query_one("#phone-region", Select)
            self.assertEqual(choice.value, "au1")
            choice.value = "us1"
            await pilot.pause()
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            await pilot.pause()
            self.assertEqual(twilio.API_BASE, "https://api.twilio.com")
            app.persistence.save_twilio_region.assert_called_once_with("us1")
            self.assertIn("United States (US1) region", "\n".join(app.activity.entries))


if __name__ == "__main__":
    unittest.main()
