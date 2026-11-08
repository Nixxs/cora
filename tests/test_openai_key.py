"""Her OpenAI key from the app (first start or Settings > General) and her voice setting. Mocked APIs only."""

import asyncio
import os
import unittest
from unittest.mock import MagicMock, patch

import openai

import openai_key
from session_config import VOICE, VOICE_CHOICES, config_from_sections, contact_call_config
from storage.app_settings_store import AppSettingsStore
from ui.api_key_view import ApiKeyScreen
from ui.model_settings import ModelSettingsScreen
from textual.widgets import Input, Select, Static, TabbedContent
import test_keyboard_persistence as keyboard_tests
from test_model_settings_screen import MODELS


def api_error(kind):
    if kind is openai.APIConnectionError:
        return kind(request=MagicMock())
    return kind("no", response=MagicMock(status_code=401), body=None)


class KeyTests(unittest.TestCase):
    def test_a_saved_key_wins_over_env_and_none_means_missing(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-from-env-0000"}), patch.object(openai_key, "keyring") as ring:
            ring.get_password.return_value = "sk-saved-in-app-1234"
            self.assertEqual(openai_key.apply_api_key(), openai_key.SAVED)
            self.assertEqual(os.environ["OPENAI_API_KEY"], "sk-saved-in-app-1234")
            ring.get_password.return_value = None
            os.environ["OPENAI_API_KEY"] = "sk-from-env-0000"
            self.assertEqual(openai_key.apply_api_key(), openai_key.FROM_ENV)
            os.environ["OPENAI_API_KEY"] = " "
            self.assertEqual(openai_key.apply_api_key(), openai_key.MISSING)
            self.assertFalse(openai_key.has_key())

    def test_saving_keeps_it_in_the_credential_store_and_uses_it_now(self):
        with patch.dict(os.environ, {}, clear=False), patch.object(openai_key, "keyring") as ring:
            openai_key.save_key("  sk-new-key-5678 ")
            ring.set_password.assert_called_once_with("cora", "openai:api_key", "sk-new-key-5678")
            self.assertEqual(os.environ["OPENAI_API_KEY"], "sk-new-key-5678")
            self.assertEqual(openai_key.masked(), "…5678")
        with self.assertRaises(ValueError):
            openai_key.save_key(" ")

    def test_checking_says_why_a_key_was_refused_without_the_key(self):
        self.assertEqual(openai_key.check_key(" "), "Paste a key first.")
        self.assertIn("spaces", openai_key.check_key("sk-has a space"))
        cases = ((None, None), (openai.AuthenticationError, "didn't accept"),
                 (openai.APIConnectionError, "Couldn't reach OpenAI"))
        for error, expected in cases:
            with self.subTest(error=error), patch.object(openai_key.openai, "OpenAI") as client_type:
                client = client_type.return_value.__enter__.return_value
                if error is not None:
                    client.models.list.side_effect = api_error(error)
                problem = openai_key.check_key("sk-secret-key-9999")
                client_type.assert_called_once_with(api_key="sk-secret-key-9999", max_retries=0,
                                                    timeout=openai_key.CHECK_SECONDS)
                if expected is None:
                    self.assertIsNone(problem)
                else:
                    self.assertIn(expected, problem)
                    self.assertNotIn("9999", problem)


class VoiceTests(unittest.TestCase):
    def test_both_session_configs_use_the_chosen_voice(self):
        self.assertEqual(config_from_sections([], {})["audio"]["output"]["voice"], VOICE)
        self.assertEqual(config_from_sections([], {}, voice="willow")["audio"]["output"]["voice"], "willow")
        config = contact_call_config([], {}, [], "gpt-6-luna", "rules", voice="stone")
        self.assertEqual(config["audio"]["output"]["voice"], "stone")

    def test_the_store_falls_back_to_the_default_and_refuses_unknown_voices(self):
        store = AppSettingsStore(MagicMock())
        with patch.object(store, "_get", return_value=None):
            self.assertEqual(store.voice(), VOICE)
        with patch.object(store, "_get", return_value="retired-voice"):
            self.assertEqual(store.voice(), VOICE)
        with patch.object(store, "_get", return_value="willow"):
            self.assertEqual(store.voice(), "willow")
        with patch.object(store, "_set") as saved:
            store.set_voice("quartz")
            saved.assert_called_once_with("voice", "quartz")
            with self.assertRaises(ValueError):
                store.set_voice("nobody")
        self.assertIn(VOICE, VOICE_CHOICES)


class FirstStartTests(unittest.IsolatedAsyncioTestCase):
    async def test_without_a_key_she_asks_first_and_connects_once_saved(self):
        async with keyboard_tests.running_app(has_key=False) as (app, pilot):
            await pilot.pause()
            self.assertIsInstance(app.screen, ApiKeyScreen)
            app.connect_to_cora.assert_not_called()
            with patch.object(openai_key, "check_key", return_value="OpenAI didn't accept that key.") as check:
                app.screen.query_one("#api-key-input", Input).value = "sk-wrong-key-0000"
                await pilot.click("#api-key-save")
                await asyncio.wait_for(app.workers.wait_for_complete(), 3)
                await pilot.pause()
            check.assert_called_once_with("sk-wrong-key-0000")
            self.assertIsInstance(app.screen, ApiKeyScreen)
            self.assertIn("didn't accept", str(app.screen.query_one("#api-key-problem", Static).render()))
            app.connect_to_cora.assert_not_called()

            with patch.object(openai_key, "check_key", return_value=None), \
                    patch.object(openai_key, "save_key") as save:
                app.screen.query_one("#api-key-input", Input).value = "sk-right-key-1234"
                await pilot.click("#api-key-save")
                await asyncio.wait_for(app.workers.wait_for_complete(), 3)
                await pilot.pause()
            save.assert_called_once_with("sk-right-key-1234")
            self.assertNotIsInstance(app.screen, ApiKeyScreen)
            app.connect_to_cora.assert_called_once()

    async def test_later_leaves_her_waiting_and_says_where_to_add_one(self):
        async with keyboard_tests.running_app(has_key=False) as (app, pilot):
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, ApiKeyScreen)
            app.connect_to_cora.assert_not_called()
            self.assertIsNone(app.connection_worker)

    async def test_with_a_key_she_connects_straight_away(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await pilot.pause()
            self.assertNotIsInstance(app.screen, ApiKeyScreen)
            app.connect_to_cora.assert_called_once()


class SettingsTests(unittest.IsolatedAsyncioTestCase):
    async def open_settings(self, app, pilot):
        app.persistence.load_models.return_value = MODELS
        app.persistence.load_voice.return_value = "marin"
        await pilot.click("#settings-button")
        await asyncio.wait_for(app.workers.wait_for_complete(), 3)
        await pilot.pause()
        self.assertIsInstance(app.screen, ModelSettingsScreen)

    async def test_a_new_voice_is_saved_and_closing_settings_reconnects_with_it(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.open_settings(app, pilot)
            app.screen.query_one("#voice", Select).value = "willow"
            await pilot.pause()
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            app.persistence.save_voice.assert_called_once_with("willow")
            app.live.ready = True
            app.standby = False
            with patch.object(app, "action_reconnect") as reconnect:
                await pilot.press("escape")
                await pilot.pause()
            reconnect.assert_called_once()
            self.assertFalse(app.voice_changed)

    async def test_a_key_saved_in_settings_is_checked_and_its_status_shown_without_it(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.open_settings(app, pilot)
            with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-old-key-0000"}), \
                    patch.object(openai_key, "check_key", return_value=None), \
                    patch.object(openai_key, "save_key",
                                 side_effect=lambda key: os.environ.update(OPENAI_API_KEY=key)) as save, \
                    patch.object(openai_key, "saved_key", return_value="sk-new-key-4321"):
                app.screen.query_one(TabbedContent).active = "settings-general"
                await pilot.pause()
                app.screen.query_one("#openai-key", Input).value = "sk-new-key-4321"
                await pilot.click("#openai-key-save")
                await asyncio.wait_for(app.workers.wait_for_complete(), 3)
                await pilot.pause()
                save.assert_called_once_with("sk-new-key-4321")
                status = str(app.screen.query_one("#openai-key-status", Static).render())
            self.assertIn("saved in Cora (…4321)", status)
            self.assertNotIn("sk-new", status)
            self.assertEqual(app.screen.query_one("#openai-key", Input).value, "")


if __name__ == "__main__":
    unittest.main()
