"""Starting in standby or active at launch, from Settings > General. Mocked APIs only."""

import asyncio
from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from textual.widgets import Select

from app import CoraApp
from storage.app_settings_store import AppSettingsStore
from ui.model_settings import ModelSettingsScreen
import test_keyboard_persistence as keyboard_tests
from test_model_settings_screen import MODELS

# The real launch connect: the test app replaces it with a mock.
REAL_CONNECT = CoraApp.connect_to_cora.__wrapped__


class StoreTests(unittest.TestCase):
    def test_active_unless_standby_is_saved_and_only_the_two_are_accepted(self):
        store = AppSettingsStore(MagicMock())
        for saved, expected in ((None, "active"), ("standby", "standby"), ("active", "active"), ("odd", "active")):
            with self.subTest(saved=saved), patch.object(store, "_get", return_value=saved):
                self.assertEqual(store.start_mode(), expected)
        with patch.object(store, "_set") as saved:
            store.set_start_mode("standby")
            saved.assert_called_once_with("start_mode", "standby")
            with self.assertRaises(ValueError):
                store.set_start_mode("asleep")


class LaunchTests(unittest.IsolatedAsyncioTestCase):
    async def launch(self, app, mode):
        """Run the real launch connect with its startup steps mocked."""
        app.persistence.load_start_mode.return_value = mode
        if isinstance(mode, Exception):
            app.persistence.load_start_mode.side_effect = mode
        app.startup_complete = False
        with ExitStack() as stack:
            enter = lambda name, mock: stack.enter_context(patch.object(app, name, mock))
            enter("check_interrupted_conversations", MagicMock(return_value=SimpleNamespace(wait=AsyncMock())))
            for name in ("wait_for_services",):
                enter(name, AsyncMock())
            for name in ("start_signal_if_enabled", "check_email_account", "restart_webhooks", "daily_backup"):
                enter(name, MagicMock())
            listen = enter("start_wake_listener", MagicMock())
            run = stack.enter_context(patch.object(app.live, "run", AsyncMock()))
            await REAL_CONNECT(app)
        return run, listen

    async def test_in_standby_she_starts_without_connecting_and_listens_for_her_wake_word(self):
        async with keyboard_tests.running_app() as (app, pilot):
            run, listen = await self.launch(app, "standby")
            run.assert_not_awaited()
            listen.assert_called_once()
            self.assertTrue(app.standby)
            self.assertTrue(app.startup_complete)
            self.assertIn("Started in standby", "\n".join(app.activity.entries))
            # Waking her connects as after any standby.
            with patch.object(app, "connect_to_cora") as connect:
                app.action_reconnect()
                await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            connect.assert_called_once()
            self.assertFalse(app.standby)

    async def test_active_or_unreadable_she_connects_as_before(self):
        for mode in ("active", RuntimeError("private SQL")):
            with self.subTest(mode=mode):
                async with keyboard_tests.running_app() as (app, pilot):
                    run, listen = await self.launch(app, mode)
                    run.assert_awaited_once()
                    self.assertFalse(app.standby)


class SettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_choice_is_shown_and_saved_for_the_next_launch(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.persistence.load_models.return_value = MODELS
            app.persistence.load_start_mode.return_value = "active"
            await pilot.click("#settings-button")
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            await pilot.pause()
            self.assertIsInstance(app.screen, ModelSettingsScreen)
            choice = app.screen.query_one("#start-mode", Select)
            self.assertEqual(choice.value, "active")
            app.persistence.save_start_mode.assert_not_called()
            choice.value = "standby"
            await pilot.pause()
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            app.persistence.save_start_mode.assert_called_once_with("standby")
            self.assertIn("start in standby from the next launch", "\n".join(app.activity.entries))


if __name__ == "__main__":
    unittest.main()
