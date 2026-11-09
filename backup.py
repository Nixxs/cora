"""Back up Cora: her database (everything she is) and her Signal keys, into backups/.

    python backup.py                 # back up now: database and Signal keys
    python backup.py --list          # show the backups there are
    python backup.py restore FILE    # replace the database with a backup (quit Cora first)

Database backups are pg_dump snapshots, consistent while the database runs, restorable onto
any fresh install. Signal keys are archived with signal-cli paused for a moment. The newest
KEEP of each are kept. Choose a second folder in Settings > General (or, until then, set
CORA_BACKUP_COPY_DIR in .env) to also copy each backup there, such as another disk. Backups hold
every private conversation: keep them private.
"""

import argparse
from datetime import datetime, timedelta
import os
from pathlib import Path
import shutil
import subprocess
import sys

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
BACKUPS = ROOT / "backups"
ENV_FILE = ROOT / ".env.database"
KEEP = 14
# Cora backs her database up by herself at launch when the newest backup is older than this.
DAILY = timedelta(hours=24)
DATABASE_PATTERN = "cora-database-*.dump"
SIGNAL_PATTERN = "cora-signal-*.tar"
# Matches compose.yaml: the project is "cora", and the Signal service's image.
SIGNAL_VOLUME = "cora_signal-data"
SIGNAL_IMAGE = "ghcr.io/asamk/signal-cli:0.14.8"


class BackupError(Exception):
    """A backup or restore step failed; the message says which and why."""


def _run(command: list[str], **options) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(command, cwd=ROOT, timeout=600, **options)
    except FileNotFoundError:
        raise BackupError("Docker is not installed or not on PATH") from None
    except subprocess.TimeoutExpired:
        raise BackupError(f"{command[0]} took too long") from None
    if result.returncode != 0:
        error = result.stderr.decode(errors="replace") if isinstance(result.stderr, bytes) else (result.stderr or "")
        lines = [line for line in error.strip().splitlines() if line.strip()]
        raise BackupError(lines[-1][:200] if lines else f"{command[0]} exited with {result.returncode}")
    return result


def _compose(*args: str, **options) -> subprocess.CompletedProcess:
    if not ENV_FILE.exists():
        raise BackupError("No .env.database file; set up the database first")
    return _run(["docker", "compose", "--env-file", str(ENV_FILE), "--profile", "signal", *args], **options)


def stamp(now: datetime) -> str:
    return now.strftime("%Y-%m-%d_%H%M%S")


def backup_database(now: datetime, folder: Path = BACKUPS) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"cora-database-{stamp(now)}.dump"
    partial = target.with_suffix(".partial")
    try:
        with partial.open("wb") as out:
            _compose("exec", "-T", "database", "sh", "-c",
                     'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc', stdout=out, stderr=subprocess.PIPE)
        if partial.stat().st_size == 0:
            raise BackupError("The database dump came back empty")
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
    return target


def signal_running() -> bool:
    return bool(_compose("ps", "-q", "signal", capture_output=True, text=True).stdout.strip())


def backup_signal(now: datetime, folder: Path = BACKUPS) -> Path | None:
    """Her Signal keys, with signal-cli paused so they're copied whole; None if Signal was never set up."""
    if not _run(["docker", "volume", "ls", "-q", "--filter", f"name=^{SIGNAL_VOLUME}$"],
                capture_output=True, text=True).stdout.strip():
        return None
    folder.mkdir(parents=True, exist_ok=True)
    name = f"cora-signal-{stamp(now)}.tar"
    running = signal_running()
    if running:
        _compose("stop", "signal", capture_output=True)
    try:
        _run(["docker", "run", "--rm", "-v", f"{SIGNAL_VOLUME}:/data:ro", "-v", f"{folder.resolve()}:/backup",
              "--entrypoint", "tar", SIGNAL_IMAGE, "cf", f"/backup/{name}", "-C", "/data", "."],
             capture_output=True)
    finally:
        if running:
            _compose("start", "signal", capture_output=True)
    return folder / name


def prune(folder: Path, pattern: str, keep: int = KEEP) -> list[Path]:
    """Remove all but the newest `keep` backups of one kind; returns what was removed."""
    removed = sorted(folder.glob(pattern), key=lambda path: path.name)[:-keep] if folder.exists() else []
    for path in removed:
        path.unlink()
    return removed


def saved_copy_folder() -> str | None:
    """The folder chosen in Settings > General ("" for none), or None if unset or unreadable."""
    try:
        from storage.app_settings_store import AppSettingsStore
        from storage.database import build_engine

        engine = build_engine()
        try:
            return AppSettingsStore(engine).backup_copy_dir()
        finally:
            engine.dispose()
    except Exception:
        # Without the database (or the setting), .env still applies.
        return None


def copy_folder(saved: str | None = None) -> Path | None:
    """The second folder: Settings' choice ("" for none), else CORA_BACKUP_COPY_DIR from .env."""
    if saved is None:
        load_dotenv(ROOT / ".env")
        saved = os.getenv("CORA_BACKUP_COPY_DIR", "")
    saved = saved.strip()
    return Path(saved) if saved else None


def copy_elsewhere(paths: list[Path], saved: str | None = None) -> Path | None:
    """Copy the new backups to the second folder, if one is set, and prune there too."""
    folder = copy_folder(saved)
    if folder is None:
        return None
    if not folder.is_dir():
        raise BackupError(f"The second backup folder doesn't exist: {folder}")
    for path in paths:
        shutil.copy2(path, folder / path.name)
    prune(folder, DATABASE_PATTERN)
    prune(folder, SIGNAL_PATTERN)
    return folder


def latest_database_backup(folder: Path = BACKUPS) -> Path | None:
    backups = sorted(folder.glob(DATABASE_PATTERN), key=lambda path: path.name) if folder.exists() else []
    return backups[-1] if backups else None


def daily_backup_due(now: datetime, folder: Path = BACKUPS) -> bool:
    latest = latest_database_backup(folder)
    return latest is None or now - datetime.fromtimestamp(latest.stat().st_mtime) >= DAILY


def run_backup(
    include_signal: bool = True, now: datetime | None = None, copy_to: str | None = None,
) -> tuple[list[Path], Path | None]:
    """Back up, prune old ones, and copy to the second folder; returns (new backups, where copied).

    copy_to is Settings' second folder ("" for none); None uses .env's."""
    now = now or datetime.now()
    made = [backup_database(now)]
    if include_signal and (signal := backup_signal(now)) is not None:
        made.append(signal)
    prune(BACKUPS, DATABASE_PATTERN)
    prune(BACKUPS, SIGNAL_PATTERN)
    return made, copy_elsewhere(made, copy_to)


def restore_database(path: Path) -> None:
    """Replace the database's contents with a backup. Quit Cora first."""
    if not path.is_file():
        raise BackupError(f"No such backup: {path}")
    with path.open("rb") as dump:
        _compose("exec", "-T", "database", "sh", "-c",
                 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner',
                 stdin=dump, stderr=subprocess.PIPE)


def main() -> None:
    parser = argparse.ArgumentParser(description="Back up or restore Cora's database and Signal keys.")
    parser.add_argument("--list", action="store_true", help="show the backups in backups/")
    parser.add_argument("--no-signal", action="store_true", help="back up the database only")
    sub = parser.add_subparsers(dest="command")
    restore = sub.add_parser("restore", help="replace the database with a backup (quit Cora first)")
    restore.add_argument("file", type=Path)
    restore.add_argument("--yes", action="store_true", help="skip the confirmation")
    args = parser.parse_args()
    try:
        if args.list:
            for path in sorted(BACKUPS.glob("cora-*"), key=lambda p: p.name) if BACKUPS.exists() else []:
                print(f"{path.name}  {path.stat().st_size / 1_000_000:.1f} MB")
            return
        if args.command == "restore":
            print(f"This replaces everything in Cora's database with {args.file.name}.")
            print("Quit Cora first. Anything since that backup is lost.")
            if not args.yes and input("Type restore to continue: ").strip() != "restore":
                print("Nothing changed.")
                return
            # A safety copy of what's there now, in case the wrong file was chosen.
            safety = backup_database(datetime.now())
            print(f"Saved the current database first: {safety.name}")
            restore_database(args.file)
            print("Restored. Start Cora as usual.")
            return
        made, copied = run_backup(include_signal=not args.no_signal, copy_to=saved_copy_folder())
        for path in made:
            print(f"Saved {path.relative_to(ROOT)}  ({path.stat().st_size / 1_000_000:.1f} MB)")
        if copied:
            print(f"Copied to {copied}")
    except BackupError as error:
        sys.exit(f"Backup failed: {error}")


if __name__ == "__main__":
    main()
