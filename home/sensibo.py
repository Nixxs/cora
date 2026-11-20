"""The house's air conditioners, through Sensibo's API (https://support.sensibo.com/api/).

Each unit ("pod") has a room, its state (on, mode, target temperature, fan, swing), what it supports
(`remoteCapabilities`) and the room's own readings (temperature, humidity, feels-like), about a
minute old. Sensibo's Python SDK isn't used: a 50-line example with no timeouts.

Sensibo limits requests tightly (about ten in a few seconds were enough to be refused), so requests
are at least MIN_GAP_SECONDS apart, every unit is read in one request, an answer is reused for
FRESH_SECONDS, and when Sensibo refuses one the last answer is used, with its age.

The API key comes from the Sensibo account and is kept in the system credential store.
Blocking calls (the credential store): run them off the UI thread.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
import json
import re
from time import monotonic

import httpx2
import keyring
from keyring.errors import PasswordDeleteError

KEYRING_SERVICE = "cora"
KEY_ACCOUNT = "home:sensibo-key"
# Sensibo's keys are letters and digits, about 30 of them; a little room either way.
KEY_PATTERN = re.compile(r"[A-Za-z0-9]{20,64}")
API = "https://home.sensibo.com/api/v2"
FIELDS = "id,room,acState,measurements,connectionStatus,remoteCapabilities"
TIMEOUT_SECONDS = 15.0
MIN_GAP_SECONDS = 2.0
FRESH_SECONDS = 60.0
ROOM_NAME_MAX_CHARS = 60


def sensibo_key() -> str:
    """The Sensibo API key, or "" when none is set."""
    return keyring.get_password(KEYRING_SERVICE, KEY_ACCOUNT) or ""


def save_sensibo_key(value: str) -> str:
    """Saves it trimmed; empty forgets it. ValueError when it doesn't look like a Sensibo key."""
    value = value.strip()
    if not value:
        try:
            keyring.delete_password(KEYRING_SERVICE, KEY_ACCOUNT)
        except PasswordDeleteError:
            pass  # nothing was saved
        return ""
    if not KEY_PATTERN.fullmatch(value):
        raise ValueError("That doesn't look like a Sensibo API key (letters and digits, from home.sensibo.com/me/api).")
    keyring.set_password(KEYRING_SERVICE, KEY_ACCOUNT, value)
    return value


class SensiboError(Exception):
    """Sensibo couldn't be read; the message is fit to show."""


class BadKey(SensiboError):
    pass


class RateLimited(SensiboError):
    pass


@dataclass(frozen=True)
class Room:
    """One unit and the room it's in, as Sensibo last reported them."""

    id: str
    name: str
    online: bool
    on: bool | None = None
    mode: str | None = None
    target: int | None = None
    unit: str = "C"
    fan: str | None = None
    swing: str | None = None
    temperature: float | None = None
    humidity: float | None = None
    feels_like: float | None = None
    # When the readings were taken (UTC), or None without them.
    measured_at: datetime | None = None
    # What it supports, by mode: {"cool": {"temperatures": [18, ...], "fan": [...], "swing": [...]}}.
    modes: dict = field(default_factory=dict, compare=False)


def _number(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _time(value) -> datetime | None:
    text = value.get("time") if isinstance(value, dict) else None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")) if isinstance(text, str) else None
    except ValueError:
        return None


def _modes(capabilities, unit: str) -> dict:
    modes = {}
    for mode, cap in ((capabilities or {}).get("modes") or {}).items():
        if not isinstance(cap, dict):
            continue
        temperatures = ((cap.get("temperatures") or {}).get(unit) or {}).get("values") or []
        modes[mode] = {"temperatures": [t for t in temperatures if isinstance(t, int)],
                       "fan": list(cap.get("fanLevels") or []), "swing": list(cap.get("swing") or [])}
    return modes


def parse_rooms(body) -> list[Room]:
    """Every unit from /users/me/pods; ValueError when the answer isn't the expected shape."""
    if not isinstance(body, dict) or not isinstance(body.get("result"), list):
        raise ValueError("Unexpected answer from Sensibo")
    rooms = []
    for pod in body["result"]:
        if not isinstance(pod, dict) or not isinstance(pod.get("id"), str):
            continue
        state = pod.get("acState") if isinstance(pod.get("acState"), dict) else {}
        readings = pod.get("measurements") if isinstance(pod.get("measurements"), dict) else {}
        name = str(((pod.get("room") or {}).get("name")) or pod["id"])[:ROOM_NAME_MAX_CHARS]
        unit = state.get("temperatureUnit") if state.get("temperatureUnit") in ("C", "F") else "C"
        target = state.get("targetTemperature")
        rooms.append(Room(
            id=pod["id"], name=name, online=bool((pod.get("connectionStatus") or {}).get("isAlive")),
            on=state.get("on") if isinstance(state.get("on"), bool) else None,
            mode=state.get("mode") if isinstance(state.get("mode"), str) else None,
            target=target if isinstance(target, int) and not isinstance(target, bool) else None, unit=unit,
            fan=state.get("fanLevel") if isinstance(state.get("fanLevel"), str) else None,
            swing=state.get("swing") if isinstance(state.get("swing"), str) else None,
            temperature=_number(readings.get("temperature")), humidity=_number(readings.get("humidity")),
            feels_like=_number(readings.get("feelsLike")), measured_at=_time(readings.get("time")),
            modes=_modes(pod.get("remoteCapabilities"), unit),
        ))
    return rooms


def same_name(a: str, b: str) -> bool:
    """Room names compared loosely: case, spaces and curly or straight apostrophes don't matter."""
    def plain(name: str) -> str:
        return " ".join(name.replace("’", "'").replace("‘", "'").casefold().split())
    return plain(a) == plain(b)


def find_room(rooms: list[Room], wanted: str) -> Room | None:
    """A room by name ("declan's room", or a unique start of one such as "declan")."""
    exact = [room for room in rooms if same_name(room.name, wanted)]
    if exact:
        return exact[0]
    start = " ".join(wanted.replace("’", "'").casefold().split())
    matches = [room for room in rooms if " ".join(room.name.replace("’", "'").casefold().split()).startswith(start)]
    return matches[0] if len(matches) == 1 and start else None


def reading(room: Room, now: datetime) -> dict:
    """A room as Cora is told it: its readings, how old they are, and its air conditioner."""
    shown = {"room": room.name}
    if room.temperature is not None:
        shown["temperature"] = f"{room.temperature:g}°{room.unit}"
    if room.feels_like is not None and room.feels_like != room.temperature:
        shown["feels_like"] = f"{room.feels_like:g}°{room.unit}"
    if room.humidity is not None:
        shown["humidity"] = f"{room.humidity:g}%"
    if room.measured_at is not None:
        minutes = max(0, round((now - room.measured_at).total_seconds() / 60))
        shown["measured"] = "just now" if minutes < 2 else f"{minutes} minutes ago"
    if not room.online:
        shown["aircon"] = "offline (its readings and state may be old)"
    elif room.on is False:
        shown["aircon"] = "off"
    elif room.on:
        parts = [room.mode or "on"]
        if room.target is not None and room.mode not in ("fan",):
            parts.append(f"set to {room.target}°{room.unit}")
        if room.fan:
            parts.append(f"fan {room.fan}")
        shown["aircon"] = ", ".join(parts)
    return shown


def room_context(room: Room, now: datetime, local_now: datetime) -> str:
    """Her own room, for her instructions: one short line of JSON."""
    shown = reading(room, now)
    shown["as_of"] = local_now.strftime("%Y-%m-%d %H:%M")
    return json.dumps(shown, ensure_ascii=False)


class Sensibo:
    """Reads every unit at once, paced and cached (see the module's notes)."""

    def __init__(self, key: str = "", *, clock: Callable[[], float] = monotonic, sleep=asyncio.sleep,
                 transport=None):
        self.key = key
        self.clock = clock
        self.sleep = sleep
        self.transport = transport
        self.lock = asyncio.Lock()
        self.last_request = -MIN_GAP_SECONDS
        self.cached: list[Room] | None = None
        self.cached_at = 0.0

    def set_key(self, key: str) -> None:
        """A new key (or "" for none): what was read with the old one is forgotten."""
        self.key = key
        self.cached = None

    async def rooms(self, max_age: float = FRESH_SECONDS) -> tuple[list[Room], float]:
        """Every unit, and how many seconds old the answer is. A refused or failed request falls back
        to the last answer when there is one; otherwise SensiboError (BadKey, RateLimited)."""
        if not self.key:
            raise BadKey("No Sensibo API key is saved (Settings > Services > Sensibo).")
        async with self.lock:
            # Strictly younger, so max_age=0 always asks again.
            if self.cached is not None and self.clock() - self.cached_at < max_age:
                return self.cached, self.clock() - self.cached_at
            wait = MIN_GAP_SECONDS - (self.clock() - self.last_request)
            if wait > 0:
                await self.sleep(wait)
            self.last_request = self.clock()
            try:
                rooms = await self._request()
            except BadKey:
                raise
            except SensiboError:
                if self.cached is not None:
                    return self.cached, self.clock() - self.cached_at
                raise
            self.cached, self.cached_at = rooms, self.clock()
            return rooms, 0.0

    async def _request(self) -> list[Room]:
        options = {"timeout": TIMEOUT_SECONDS, "headers": {"Accept-Encoding": "gzip"}}
        if self.transport is not None:
            options["transport"] = self.transport
        try:
            async with httpx2.AsyncClient(**options) as client:
                response = await client.get(f"{API}/users/me/pods", params={"apiKey": self.key, "fields": FIELDS})
        except httpx2.TimeoutException as error:
            raise SensiboError("Sensibo didn't answer in time.") from error
        except httpx2.HTTPError as error:
            raise SensiboError(f"Sensibo couldn't be reached ({type(error).__name__}).") from error
        if response.status_code in (401, 403):
            raise BadKey("Sensibo refused the API key; check it in Settings > Services > Sensibo.")
        if response.status_code == 429:
            raise RateLimited("Sensibo is limiting requests just now; try again in a minute.")
        if response.status_code != 200:
            raise SensiboError(f"Sensibo answered {response.status_code}.")
        try:
            return parse_rooms(response.json())
        except ValueError as error:
            raise SensiboError("Sensibo's answer wasn't understood.") from error
