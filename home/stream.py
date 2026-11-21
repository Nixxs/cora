"""Sensibo's live event stream: one open connection for every unit (Server-Sent Events).

Events (https://support.sensibo.com/api/): `hello` on connecting ({"heartbeatSeconds": 120, ...}),
after which everything is read once, since changes before it were missed; `measurements` ({"id",
"measurements"}) at most once a minute per unit, only when a value changed; `changed` ({"id",
"fields"}) when its state, connection or readings changed (an empty list: anything may have);
`resync`, after which the server closes the stream. Heartbeats are comment lines.

As Sensibo asks: reconnect after a growing delay (a few seconds up to 5 minutes, with jitter), wait at
least Retry-After on 429 or 503, and reconnect if nothing arrives for two heartbeats. The key goes in
the X-Api-Key header, not the address. The stream only tells; changes are still made with requests.
"""

import asyncio
from collections.abc import Awaitable, Callable
import json
import random

import httpx2

from .sensibo import API, BadKey

STREAM_URL = f"{API}/stream"
FIRST_DELAY_SECONDS = 3.0
MAX_DELAY_SECONDS = 300.0
DEFAULT_HEARTBEAT_SECONDS = 120.0
CONNECT_TIMEOUT_SECONDS = 15.0


class SseParser:
    """Server-Sent Events, a line at a time: feed() returns (event, data) once an event is complete."""

    def __init__(self):
        self.event = ""
        self.data: list[str] = []

    def feed(self, line: str) -> tuple[str, str] | None:
        if line == "":
            if not self.data and not self.event:
                return None
            event, data = self.event or "message", "\n".join(self.data)
            self.event, self.data = "", []
            return event, data
        if line.startswith(":"):
            return None  # a heartbeat or comment
        name, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if name == "event":
            self.event = value
        elif name == "data":
            self.data.append(value)
        return None


class StreamRefused(Exception):
    """Sensibo turned the stream away for now; wait at least `retry_after` seconds."""

    def __init__(self, status: int, retry_after: float):
        super().__init__(f"Sensibo's stream answered {status}")
        self.retry_after = retry_after


class SensiboStream:
    """Keeps the stream open and hands each event to on_event(event, data dict) until cancelled.
    A refused key ends it (BadKey); everything else is retried."""

    def __init__(self, key: str, on_event: Callable[[str, dict], Awaitable[None]], *,
                 on_problem: Callable[[str], None] = lambda problem: None, transport=None,
                 sleep=asyncio.sleep, jitter: Callable[[], float] = random.random):
        self.key = key
        self.on_event = on_event
        self.on_problem = on_problem
        self.transport = transport
        self.sleep = sleep
        self.jitter = jitter
        self.heartbeat = DEFAULT_HEARTBEAT_SECONDS

    async def run(self) -> None:
        delay = FIRST_DELAY_SECONDS
        while True:
            wait = None
            try:
                if await self.listen():
                    delay = FIRST_DELAY_SECONDS  # it was working: start the delays again
            except BadKey:
                raise
            except StreamRefused as refused:
                wait = refused.retry_after
                self.on_problem(str(refused))
            except (httpx2.HTTPError, asyncio.TimeoutError, OSError) as error:
                self.on_problem(f"Sensibo's stream dropped ({type(error).__name__})")
            pause = max(wait or 0.0, delay) + self.jitter() * delay / 2
            await self.sleep(pause)
            delay = min(delay * 2, MAX_DELAY_SECONDS)

    async def listen(self) -> bool:
        """One connection, until it ends; True if Sensibo said hello on it."""
        options = {"timeout": httpx2.Timeout(CONNECT_TIMEOUT_SECONDS, read=None)}
        if self.transport is not None:
            options["transport"] = self.transport
        greeted = False
        parser = SseParser()
        async with httpx2.AsyncClient(**options) as client:
            async with client.stream("GET", STREAM_URL, headers={"X-Api-Key": self.key,
                                                                  "Accept": "text/event-stream"}) as response:
                if response.status_code in (401, 403):
                    raise BadKey("Sensibo refused the API key; check it in Settings > Services > Sensibo.")
                if response.status_code in (429, 503):
                    retry = response.headers.get("Retry-After", "")
                    raise StreamRefused(response.status_code, float(retry) if retry.isdigit() else 0.0)
                if response.status_code != 200:
                    raise StreamRefused(response.status_code, 0.0)
                lines = response.aiter_lines()
                while True:
                    try:
                        # Nothing for two heartbeats: the connection is dead even if it looks open.
                        line = await asyncio.wait_for(anext(lines), 2 * self.heartbeat + 10)
                    except StopAsyncIteration:
                        return greeted
                    event = parser.feed(line.rstrip("\r"))
                    if event is None:
                        continue
                    name, raw = event
                    try:
                        data = json.loads(raw) if raw else {}
                    except ValueError:
                        continue
                    if not isinstance(data, dict):
                        continue
                    if name == "hello":
                        greeted = True
                        beat = data.get("heartbeatSeconds")
                        if isinstance(beat, (int, float)) and not isinstance(beat, bool) and beat > 0:
                            self.heartbeat = float(beat)
                    await self.on_event(name, data)
                    if name == "resync":
                        return greeted
