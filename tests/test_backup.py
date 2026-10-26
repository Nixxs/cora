"""Backing up and restoring Cora. Docker is never called: the commands are checked, not run."""

import os
from datetime import datetime, timedelta
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import backup


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, True)

    def touch(self, name, age=timedelta()):
        path = self.folder / name
        path.write_bytes(b"x")
        when = (datetime.now() - age).timestamp()
        os.utime(path, (when, when))
        return path

    def test_prune_keeps_the_newest_of_each_kind(self):
        for day in range(1, 18):
            self.touch(f"cora-database-2026-10-{day:02d}_120000.dump")
        self.touch("cora-signal-2026-10-01_120000.tar")
        removed = backup.prune(self.folder, backup.DATABASE_PATTERN)
        self.assertEqual([p.name for p in removed],
                         [f"cora-database-2026-10-{day:02d}_120000.dump" for day in (1, 2, 3)])
        self.assertEqual(len(list(self.folder.glob(backup.DATABASE_PATTERN))), backup.KEEP)
        self.assertTrue((self.folder / "cora-signal-2026-10-01_120000.tar").exists())

    def test_a_daily_backup_is_due_when_there_is_none_or_it_is_a_day_old(self):
        now = datetime.now()
        self.assertTrue(backup.daily_backup_due(now, self.folder))
        self.touch("cora-database-2026-10-26_090000.dump", age=timedelta(hours=3))
        self.assertFalse(backup.daily_backup_due(now, self.folder))
        self.touch("cora-database-2026-10-25_090000.dump", age=timedelta(hours=30))
        self.assertFalse(backup.daily_backup_due(now, self.folder))  # the newest counts
        (self.folder / "cora-database-2026-10-26_090000.dump").unlink()
        self.assertTrue(backup.daily_backup_due(now, self.folder))

    def test_the_database_dump_uses_pg_dump_and_never_leaves_a_partial_file(self):
        def compose(*args, stdout=None, **options):
            self.assertIn("pg_dump", args[-1])
            stdout.write(b"PGDMP...")
            return subprocess.CompletedProcess(args, 0)

        with patch.object(backup, "_compose", side_effect=compose):
            path = backup.backup_database(datetime(2026, 10, 26, 21, 5, 1), self.folder)
        self.assertEqual(path.name, "cora-database-2026-10-26_210501.dump")
        self.assertEqual(path.read_bytes(), b"PGDMP...")
        self.assertEqual(list(self.folder.glob("*.partial")), [])

        with patch.object(backup, "_compose", side_effect=backup.BackupError("database is down")), \
                self.assertRaisesRegex(backup.BackupError, "database is down"):
            backup.backup_database(datetime(2026, 10, 27), self.folder)
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ["cora-database-2026-10-26_210501.dump"])

    def test_signal_is_paused_for_its_backup_and_started_again_even_if_it_fails(self):
        calls = []
        with patch.object(backup, "_run") as run, patch.object(backup, "_compose") as compose, \
                patch.object(backup, "signal_running", return_value=True):
            run.side_effect = lambda command, **options: (
                calls.append(command[:3]) or MagicMock(stdout="cora_signal-data\n")
                if command[:3] == ["docker", "volume", "ls"] else
                (_ for _ in ()).throw(backup.BackupError("tar failed"))
            )
            with self.assertRaises(backup.BackupError):
                backup.backup_signal(datetime(2026, 10, 26), self.folder)
            self.assertEqual([call.args[:2] for call in compose.call_args_list], [("stop", "signal"), ("start", "signal")])

    def test_no_signal_volume_means_no_signal_backup(self):
        with patch.object(backup, "_run", return_value=MagicMock(stdout="")), patch.object(backup, "_compose") as compose:
            self.assertIsNone(backup.backup_signal(datetime.now(), self.folder))
        compose.assert_not_called()

    def test_backups_are_copied_to_the_second_folder_when_set(self):
        made = [self.touch("cora-database-2026-10-26_210501.dump")]
        with patch.object(backup, "copy_folder", return_value=None):
            self.assertIsNone(backup.copy_elsewhere(made))
        second = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, second, True)
        with patch.object(backup, "copy_folder", return_value=second):
            self.assertEqual(backup.copy_elsewhere(made), second)
        self.assertTrue((second / made[0].name).exists())
        with patch.object(backup, "copy_folder", return_value=second / "missing"), \
                self.assertRaisesRegex(backup.BackupError, "not a folder"):
            backup.copy_elsewhere(made)

    def test_restore_feeds_the_backup_to_pg_restore(self):
        dump = self.touch("cora-database-2026-10-26_210501.dump")
        with patch.object(backup, "_compose") as compose:
            backup.restore_database(dump)
        self.assertIn("pg_restore", compose.call_args.args[-1])
        self.assertIn("--clean", compose.call_args.args[-1])
        with self.assertRaisesRegex(backup.BackupError, "No such backup"):
            backup.restore_database(self.folder / "nope.dump")



class DailyBackupAppTests(unittest.TestCase):
    def app(self):
        from app import CoraApp
        return CoraApp.__new__(CoraApp)  # back_up_if_due needs no running app

    def test_nothing_happens_when_no_backup_is_due(self):
        with patch("app.daily_backup_due", return_value=False), patch("app.run_backup") as run:
            self.assertIsNone(self.app().back_up_if_due())
        run.assert_not_called()

    def test_a_due_backup_is_the_database_only_and_says_where_it_went(self):
        with patch("app.daily_backup_due", return_value=True),                 patch("app.run_backup", return_value=([Path("cora-database-x.dump")], Path("E:/Backups"))) as run:
            message, summary = self.app().back_up_if_due()
        run.assert_called_once_with(include_signal=False)
        self.assertEqual(summary, "Backup saved")
        self.assertIn("cora-database-x.dump", message)
        self.assertIn("copied to", message)

    def test_failures_say_why_without_anything_private(self):
        with patch("app.daily_backup_due", return_value=True),                 patch("app.run_backup", side_effect=backup.BackupError("Docker is not installed or not on PATH")):
            self.assertEqual(self.app().back_up_if_due(),
                             ("Daily backup failed: Docker is not installed or not on PATH", "Backup failed"))
        with patch("app.daily_backup_due", return_value=True),                 patch("app.run_backup", side_effect=OSError("C:/private/path")):
            self.assertEqual(self.app().back_up_if_due(), ("Daily backup failed (OSError).", "Backup failed"))
