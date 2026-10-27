"""Start and stop the signal-cli container from the project's compose file."""

import asyncio
import subprocess
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env.database"
DEFAULT_PORT = 8090


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
