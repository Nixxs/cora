"""Standby settings from Settings > General (automatic standby, her goodbye) and the remembered
Wake button, replacing .env. Mocked APIs only."""

import asyncio
import unittest
from unittest.mock import MagicMock, patch

from textual.widgets import Button, Select, Switch

from storage.app_settings_store import MAX_IDLE_MINUTES, AppSettingsStore
from ui.model_settings import ModelSettingsScreen, idle_options
import test_keyboard_persistence as keyboard_tests
from test_model_settings_screen import MODELS
import test_start_mode


class StoreTests(unittest.TestCase):
    def test_unset_is_none_so_env_applies_and_bad_values_are_refused(self):
        store = AppSettingsStore(MagicMock())
        with patch.object(store, "_get", return_value=None):
            self.assertEqual((store.idle_minutes(), store.standby_farewell(), store.wake_word()), (None, None, None))
        with patch.object(store, "_get", return_value="15"):
            self.assertEqual(store.idle_minutes(), 15)
        with patch.object(store, "_get", return_value="on"):
            self.assertTrue(store.standby_farewell())
            self.assertTrue(store.wake_word())
        with patch.object(store, "_get", return_value="off"):
            self.assertFalse(store.wake_word())
        with patch.object(store, "_set") as saved:
            store.set_idle_minutes(0)
            store.set_standby_farewell(True)
            store.set_wake_word(False)
            self.assertEqual(saved.call_args_list[0].args, ("idle_minutes", "0"))
            self.assertEqual(saved.call_args_list[1].args, ("standby_farewell", "on"))
            self.assertEqual(saved.call_args_list[2].args, ("wake_word", "off"))
            for bad in (-1, MAX_IDLE_MINUTES + 1, 2.5, True):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    store.set_idle_minutes(bad)

    def test_choices_keep_a_value_from_env(self):
        self.assertEqual(idle_options(5)[0], ("Off", 0))
        self.assertIn(("After 7 minutes", 7), idle_options(7))
        self.assertEqual(len(idle_options(5)), len(idle_options(7)) - 1)


class LaunchSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_settings_replace_env_at_launch(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.persistence.load_standby_settings.return_value = {
                "idle_minutes": 0, "farewell": True, "wake_word": True}
            await test_start_mode.LaunchTests.launch(self, app, "active")
            self.assertEqual(app.inactivity.timeout_seconds, 0)
            self.assertTrue(app.inactivity.farewell_enabled)
            self.assertTrue(app.wake.enabled)

    async def test_unset_or_unreadable_keeps_env(self):
        for saved in ({"idle_minutes": None, "farewell": None, "wake_word": None}, RuntimeError("private SQL")):
            with self.subTest(saved=saved):
                async with keyboard_tests.running_app() as (app, pilot):
                    before = (app.inactivity.timeout_seconds, app.inactivity.farewell_enabled, app.wake.enabled)
                    if isinstance(saved, Exception):
                        app.persistence.load_standby_settings.side_effect = saved
                    else:
                        app.persistence.load_standby_settings.return_value = saved
                    await test_start_mode.LaunchTests.launch(self, app, "active")
                    self.assertEqual(
                        (app.inactivity.timeout_seconds, app.inactivity.farewell_enabled, app.wake.enabled), before)

    async def test_the_wake_button_is_remembered(self):
        async with keyboard_tests.running_app() as (app, pilot):
            was = app.wake.enabled
            with patch.object(app, "start_wake_listener"):
                await pilot.click("#wake-status")
                await pilot.pause()
                await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            app.persistence.save_wake_word.assert_called_once_with(not was)


class SettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_changes_apply_now_and_are_saved(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.persistence.load_models.return_value = MODELS
            app.inactivity.timeout_seconds, app.inactivity.farewell_enabled = 300.0, False
            await pilot.click("#settings-button")
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            await pilot.pause()
            self.assertIsInstance(app.screen, ModelSettingsScreen)
            idle = app.screen.query_one("#idle-minutes", Select)
            self.assertEqual(idle.value, 5)
            idle.value = 15
            app.screen.query_one("#standby-farewell", Switch).value = True
            await pilot.pause()
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            self.assertEqual(app.inactivity.timeout_seconds, 900.0)
            self.assertTrue(app.inactivity.farewell_enabled)
            app.persistence.save_idle_minutes.assert_called_once_with(15)
            app.persistence.save_standby_farewell.assert_called_once_with(True)

            idle.value = 0
            await pilot.pause()
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            self.assertEqual(app.inactivity.timeout_seconds, 0)
            self.assertIn("Automatic standby is off.", "\n".join(app.activity.entries))


if __name__ == "__main__":
    unittest.main()
