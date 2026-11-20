"""Every setting the store saves must be allowed by the database's app_settings_key_check.

The check lives only in the migrations, and the store's own tests mock the database, so a new
setting without a migration failed only when saved for real ("Could not save ...")."""

from pathlib import Path
import re
import unittest
from unittest.mock import MagicMock, patch

from storage.app_settings_store import AppSettingsStore

ROOT = Path(__file__).resolve().parents[1]


def allowed_keys() -> set[str]:
    """The key list in the newest migration that sets app_settings_key_check."""
    latest = None
    for path in sorted((ROOT / "migrations").glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        if "ADD CONSTRAINT app_settings_key_check" in text:
            latest = text
    match = re.search(r"app_settings_key_check CHECK \(\s*key IN \((.*?)\)\s*\)", latest, re.S)
    return set(re.findall(r"'([a-z_]+)'", match.group(1)))


def used_keys() -> set[str]:
    source = (ROOT / "storage" / "app_settings_store.py").read_text(encoding="utf-8")
    return set(re.findall(r"""_(?:get|set|switch)\(\s*"([a-z_]+)\"""", source)) | set(
        re.findall(r"""AppSettingRecord\.key == "([a-z_]+)\"""", source))


class AllowedKeysTests(unittest.TestCase):
    def test_every_setting_the_store_saves_is_allowed(self):
        used = used_keys()
        self.assertIn("her_room", used)
        self.assertGreater(len(used), 20)
        self.assertEqual(used - allowed_keys(), set(), "add a migration for these app_settings keys")


class HomeSettingsTests(unittest.TestCase):
    def test_sensibo_is_off_and_her_room_unset_unless_chosen(self):
        store = AppSettingsStore(MagicMock())
        for saved, expected in ((None, False), ("off", False), ("on", True)):
            with self.subTest(saved=saved), patch.object(store, "_get", return_value=saved):
                self.assertIs(store.sensibo_enabled(), expected)
        for saved, expected in ((None, ""), ("Jyt7jM8c", "Jyt7jM8c"), ("bad id!", ""), ("x" * 40, "")):
            with self.subTest(saved=saved), patch.object(store, "_get", return_value=saved):
                self.assertEqual(store.her_room(), expected)

    def test_her_room_is_saved_or_cleared_never_stored_empty(self):
        store = AppSettingsStore(MagicMock())
        with patch.object(store, "_set") as saved:
            store.set_her_room("Jyt7jM8c")
            saved.assert_called_once_with("her_room", "Jyt7jM8c")
            with self.assertRaises(ValueError):
                store.set_her_room("../x")
        with patch.object(store, "_set") as saved, patch("storage.app_settings_store.Session") as session:
            store.set_her_room("")
            saved.assert_not_called()
            session.return_value.__enter__.return_value.execute.assert_called_once()


if __name__ == "__main__":
    unittest.main()
