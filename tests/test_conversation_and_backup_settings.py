"""When a conversation ends and the second backup folder, from Settings > General, replacing .env.
Mocked APIs only."""

import asyncio
from datetime import timedelta
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from textual.widgets import Input, Select, Static, TabbedContent

from app_areas.settings import backup_copy_status
from app_areas.shared import conversation_end_standby_seconds
from storage import client_store
from storage.app_settings_store import BACKUP_COPY_MAX_CHARS, MAX_GAP_MINUTES, AppSettingsStore
from ui.model_settings import ModelSettingsScreen, gap_options, gap_words
import test_keyboard_persistence as keyboard_tests
from test_model_settings_screen import MODELS
import test_start_mode


def keep_gap(test) -> None:
    """The gap is the app's own module setting: put it back after each test."""
    test.enterContext(patch.object(client_store, "CONVERSATION_GAP", client_store.CONVERSATION_GAP))


class StoreTests(unittest.TestCase):
    def test_the_gap_is_whole_minutes_and_unset_is_none(self):
        store = AppSettingsStore(MagicMock())
        for saved, expected in ((None, None), ("90", 90), ("0", None), ("abc", None), (str(MAX_GAP_MINUTES + 1), None)):
            with self.subTest(saved=saved), patch.object(store, "_get", return_value=saved):
                self.assertEqual(store.conversation_gap(), expected)
        with patch.object(store, "_set") as saved:
            store.set_conversation_gap(30)
            saved.assert_called_once_with("conversation_gap", "30")
            for bad in (0, MAX_GAP_MINUTES + 1, 1.5, True):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    store.set_conversation_gap(bad)

    def test_the_backup_folder_is_a_full_path_or_none(self):
        store = AppSettingsStore(MagicMock())
        for saved, expected in ((None, None), ("none", ""), ("E:\\Cora backups", "E:\\Cora backups")):
            with self.subTest(saved=saved), patch.object(store, "_get", return_value=saved):
                self.assertEqual(store.backup_copy_dir(), expected)
        folder = str(Path(tempfile.gettempdir()).resolve())
        with patch.object(store, "_set") as saved:
            store.set_backup_copy_dir("  ")
            store.set_backup_copy_dir(folder)
            self.assertEqual([call.args for call in saved.call_args_list],
                             [("backup_copy_dir", "none"), ("backup_copy_dir", folder)])
            for bad in ("relative\\folder", "C:\\" + "x" * BACKUP_COPY_MAX_CHARS):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    store.set_backup_copy_dir(bad)


class WordsTests(unittest.TestCase):
    def test_choices_read_naturally_and_keep_a_value_from_env(self):
        self.assertEqual([gap_words(m) for m in (1, 15, 60, 90, 120, 75)],
                         ["1 minute", "15 minutes", "an hour", "1.5 hours", "2 hours", "75 minutes"])
        self.assertIn(("After an hour", 60), gap_options(60))
        self.assertIn(("After 45 minutes", 45), gap_options(45))
        self.assertEqual(len(gap_options(45)), len(gap_options(60)) + 1)

    def test_the_folder_status(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        self.assertTrue(backup_copy_status(None).startswith("No second copy"))
        self.assertEqual(backup_copy_status(folder), f"In use: {folder}")
        self.assertTrue(backup_copy_status(folder / "missing").startswith("Not found"))


class GapTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        keep_gap(self)

    def test_a_new_gap_applies_to_contacts_chats_and_standby(self):
        client_store.set_conversation_gap(20)
        self.assertEqual(client_store.conversation_gap(), timedelta(minutes=20))
        self.assertEqual(conversation_end_standby_seconds(), 1200.0)

    async def test_the_saved_gap_replaces_env_at_launch(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.persistence.load_conversation_gap.return_value = 30
            await test_start_mode.LaunchTests.launch(self, app, "active")
            self.assertEqual(client_store.conversation_gap(), timedelta(minutes=30))

    async def test_unset_keeps_env(self):
        before = client_store.conversation_gap()
        async with keyboard_tests.running_app() as (app, pilot):
            await test_start_mode.LaunchTests.launch(self, app, "active")
        self.assertEqual(client_store.conversation_gap(), before)


class SettingsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        keep_gap(self)
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, True)

    async def open_settings(self, app, pilot):
        app.persistence.load_models.return_value = MODELS
        await pilot.click("#settings-button")
        await asyncio.wait_for(app.workers.wait_for_complete(), 3)
        await pilot.pause()
        self.assertIsInstance(app.screen, ModelSettingsScreen)
        app.screen.query_one(TabbedContent).active = "settings-general"
        await pilot.pause()

    async def settle(self, app, pilot):
        await pilot.pause()
        await asyncio.wait_for(app.workers.wait_for_complete(), 3)
        await pilot.pause()

    async def press(self, app, pilot, button):
        # General is taller than the test window: scroll to it, as you would.
        app.screen.query_one(button).scroll_visible(animate=False)
        await pilot.pause()
        await pilot.click(button)
        await self.settle(app, pilot)

    async def test_the_gap_applies_now_and_is_saved(self):
        client_store.set_conversation_gap(60)
        async with keyboard_tests.running_app() as (app, pilot):
            await self.open_settings(app, pilot)
            gap = app.screen.query_one("#conversation-gap", Select)
            self.assertEqual(gap.value, 60)
            app.persistence.save_conversation_gap.assert_not_called()
            gap.value = 120
            await self.settle(app, pilot)
            self.assertEqual(client_store.conversation_gap(), timedelta(minutes=120))
            app.persistence.save_conversation_gap.assert_called_once_with(120)
            self.assertIn("ends after 2 hours of quiet", "\n".join(app.activity.entries))

    async def test_a_folder_that_exists_is_saved_and_removing_it_saves_none(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.persistence.load_backup_copy_dir.return_value = ""
            await self.open_settings(app, pilot)
            status = app.screen.query_one("#backup-copy-status", Static)
            self.assertIn("No second copy", str(status.render()))
            app.screen.query_one("#backup-copy-dir", Input).value = str(self.folder)
            await self.press(app, pilot, "#backup-copy-save")
            app.persistence.save_backup_copy_dir.assert_called_once_with(str(self.folder))
            self.assertIn(f"In use: {self.folder}", str(status.render()))

            await self.press(app, pilot, "#backup-copy-forget")
            app.persistence.save_backup_copy_dir.assert_called_with("")
            self.assertIn("No second copy", str(status.render()))
            self.assertEqual(app.screen.query_one("#backup-copy-dir", Input).value, "")

    async def test_a_folder_that_isnt_there_or_isnt_a_full_path_is_refused(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.persistence.load_backup_copy_dir.return_value = ""
            await self.open_settings(app, pilot)
            status = app.screen.query_one("#backup-copy-status", Static)
            for folder, says in ((str(self.folder / "missing"), "Not found"), ("Cora backups", "full path")):
                with self.subTest(folder=folder):
                    app.screen.query_one("#backup-copy-dir", Input).value = folder
                    await self.press(app, pilot, "#backup-copy-save")
                    self.assertIn(says, str(status.render()))
            app.persistence.save_backup_copy_dir.assert_not_called()


if __name__ == "__main__":
    unittest.main()
