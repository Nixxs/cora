"""Start and stop the signal-cli container from the project's compose file."""

import asyncio
import re
import subprocess
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env.database"
DEFAULT_PORT = 8090
# signal-cli keeps every received attachment here, in the signal-data volume that backups include.
ATTACHMENTS = "/var/lib/signal-cli/attachments"
# Its names are a random ID and an extension; anything else is never passed to the shell's rm.
ATTACHMENT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}(\.[A-Za-z0-9]{1,10})?")


class ContainerError(Exception):
    """Docker could not start or stop the container."""


def signal_port() -> int:
    """The local port the daemon is published on; CORA_SIGNAL_PORT in .env.database changes it."""
    value = dotenv_values(ENV_FILE).get("CORA_SIGNAL_PORT") if ENV_FILE.exists() else None
    return int(value) if value else DEFAULT_PORT


def _compose(*args: str) -> None:
    if not ENV_FILE.exists():
        raise ContainerError("No .env.database file; set up the database first")
    try:
        result = subprocess.run(
            ["docker", "compose", "--env-file", str(ENV_FILE), "--profile", "signal", *args],
            cwd=ROOT, capture_output=True, text=True, timeout=180,
        )
    except FileNotFoundError:
        raise ContainerError("Docker is not installed or not on PATH") from None
    except subprocess.TimeoutExpired:
        raise ContainerError("Docker took too long") from None
    if result.returncode != 0:
        lines = [line for line in result.stderr.strip().splitlines() if line.strip()]
        raise ContainerError(lines[-1][:200] if lines else f"docker compose exited with {result.returncode}")


async def start_container() -> None:
    await asyncio.to_thread(_compose, "up", "-d", "signal")


async def stop_container() -> None:
    await asyncio.to_thread(_compose, "stop", "signal")


async def delete_attachments(ids) -> None:
    """Delete signal-cli's stored copies once handled: signal-cli has no call for it, so rm in the container."""
    paths = [f"{ATTACHMENTS}/{attachment_id}" for attachment_id in ids if ATTACHMENT_ID.fullmatch(attachment_id)]
    if paths:
        await asyncio.to_thread(_compose, "exec", "-T", "signal", "rm", "-f", "--", *paths)


async def clear_attachments() -> None:
    """Delete any left from an earlier run (or from before attachments were deleted), before listening starts."""
    await asyncio.to_thread(_compose, "exec", "-T", "signal", "find", ATTACHMENTS, "-type", "f", "-delete")
