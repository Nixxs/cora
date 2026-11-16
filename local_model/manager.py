"""The local model's container: started when something needs it, stopped once it's been idle.

Whatever uses it holds it with `async with local_model.use() as ollama:`; while anything does, it
stays up, and it's stopped IDLE_MINUTES after the last use (checked by the app every so often),
when Cora quits, or (one left running by a Cora that didn't close properly) on her first check.
A stopped container holds none of the GPU's memory.
"""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from time import monotonic

from messaging.container import compose

from .ollama import OllamaClient

PROFILE = "local-model"
SERVICE = "local-model"
OFF, STARTING, READY, STOPPING = "off", "starting", "ready", "stopping"
# How long it stays up after its last use (Settings > Local model chooses).
IDLE_MINUTES = 15
IDLE_CHOICES = (5, 15, 30, 60, 120)
# Docker starting it, then Ollama answering.
READY_SECONDS = 90
FIRST_START_SECONDS = 1200
POLL_SECONDS = 0.5


class LocalModelError(Exception):
    """The local model couldn't be started."""


async def start_container() -> None:
    # The first start downloads Ollama's image (a few GB, with the GPU's libraries).
    await asyncio.to_thread(compose, PROFILE, "up", "-d", SERVICE, timeout=FIRST_START_SECONDS)


async def stop_container() -> None:
    await asyncio.to_thread(compose, PROFILE, "stop", SERVICE)


class LocalModel:
    def __init__(self, client: OllamaClient, write_activity: Callable[[str, str], None],
                 start: Callable[[], Awaitable[None]] | None = None,
                 stop: Callable[[], Awaitable[None]] | None = None,
                 clock: Callable[[], float] = monotonic, ready_seconds: float = READY_SECONDS,
                 poll_seconds: float = POLL_SECONDS) -> None:
        self.client = client
        self.write_activity = write_activity
        # Docker's, unless given (looked up now, so tests can stand in for them).
        self.start = start or start_container
        self.stop = stop or stop_container
        self.clock = clock
        self.ready_seconds = ready_seconds
        self.poll_seconds = poll_seconds
        self.idle_minutes = IDLE_MINUTES
        self.state = OFF
        self.in_use = 0
        self.last_used = 0.0
        self.checked_leftover = False
        self.lock = asyncio.Lock()

    @asynccontextmanager
    async def use(self):
        """Up (started if need be) while held; LocalModelError if it can't be started."""
        self.in_use += 1
        try:
            await self.ensure_up()
            yield self.client
        finally:
            self.in_use -= 1
            self.last_used = self.clock()

    async def ensure_up(self) -> None:
        async with self.lock:
            if self.state == READY:
                return
            self.state = STARTING
            self.write_activity("Starting the local model.", "Local model")
            started = self.clock()
            try:
                await self.start()
                version = await self.wait_ready()
            except Exception as error:
                self.state = OFF
                self.write_activity(f"The local model didn't start ({describe(error)}).", "Local model error")
                await self.quietly_stop()
                raise LocalModelError(describe(error)) from error
            self.state = READY
            self.last_used = self.clock()
            self.write_activity(f"The local model is up (Ollama {version}, {self.clock() - started:.0f} s).",
                                "Local model")

    async def wait_ready(self) -> str:
        deadline = self.clock() + self.ready_seconds
        while True:
            try:
                return await self.client.version()
            except Exception:
                if self.clock() >= deadline:
                    raise TimeoutError(f"Ollama didn't answer within {self.ready_seconds:.0f} seconds") from None
            await asyncio.sleep(self.poll_seconds)

    async def stop_if_idle(self) -> None:
        """Stopped once it's been idle long enough; on the first check, one left running before is too."""
        if not self.checked_leftover:
            self.checked_leftover = True
            async with self.lock:
                if self.state == OFF:
                    await self.quietly_stop()
            return
        if self.state != READY or self.in_use or self.clock() - self.last_used < self.idle_minutes * 60:
            return
        await self.shut_down(f"idle for {self.idle_minutes} minutes", only_if_unused=True)

    async def shut_down(self, why: str, only_if_unused: bool = False) -> None:
        async with self.lock:
            # Checked again: something may have started using it while this waited.
            if self.state != READY or (only_if_unused and self.in_use):
                return
            self.state = STOPPING
            try:
                await self.stop()
                self.write_activity(f"Stopped the local model ({why}).", "Local model")
            except Exception as error:
                self.write_activity(f"The local model didn't stop ({describe(error)}).", "Local model error")
            finally:
                self.state = OFF

    async def quietly_stop(self) -> None:
        try:
            await self.stop()
        except Exception:
            pass


def describe(error: Exception) -> str:
    return str(error)[:200] or type(error).__name__
