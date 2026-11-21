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
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
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
# Timers (and schedules) are still on the older version.
API_V1 = "https://home.sensibo.com/api/v1"
FIELDS = "id,room,acState,measurements,connectionStatus,remoteCapabilities,timer,schedules,smartMode,filtersCleaning"
UNIT_ID = re.compile(r"[A-Za-z0-9]{4,32}")
SCHEDULE_ID = re.compile(r"[A-Za-z0-9]{4,32}")
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
# A schedule's time of day, in the person's own time zone: "16:00".
SCHEDULE_TIME = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
# Climate React keeps a room between two temperatures (in °C, as Sensibo takes them): within this
# range, at least this far apart.
REACT_LOWEST, REACT_HIGHEST = 5.0, 40.0
REACT_MIN_GAP = 1.0
MAX_TIMER_MINUTES = 24 * 60
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
    # Its timer, if one is set: when it goes off (UTC) and whether it turns the unit on or off.
    timer_at: datetime | None = None
    timer_on: bool | None = None
    # Its schedules, and its Climate React (None if it was never set up).
    schedules: tuple = field(default=(), compare=False)
    react: "ClimateReact | None" = field(default=None, compare=False)
    # Whether it says its filters are due for a clean, and the hours it has run since the last (None
    # when it doesn't say).
    filters_due: bool | None = None
    filter_hours: float | None = None


@dataclass(frozen=True)
class Schedule:
    """A standing change Sensibo makes at a time of day, on some days or once."""

    id: str
    # "16:00", in the time zone it was made in.
    time: str
    # Lowercase day names; none means once.
    days: tuple[str, ...]
    enabled: bool
    state: dict = field(compare=False)
    # When it next goes off (UTC), if Sensibo says.
    next_at: datetime | None = None


@dataclass(frozen=True)
class ClimateReact:
    """Sensibo keeping a room between two readings: each threshold's state, applied when it's crossed."""

    enabled: bool
    # What it watches: temperature, feelsLike or humidity.
    type: str
    low: float | None
    low_state: dict = field(compare=False)
    high: float | None = None
    high_state: dict = field(default_factory=dict, compare=False)


def _number(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _time(value) -> datetime | None:
    """A time Sensibo gives as {"time": "..."}; one without a zone is UTC (as its timers and schedules
    give theirs)."""
    text = value.get("time") if isinstance(value, dict) else None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00")) if isinstance(text, str) else None
    except ValueError:
        return None
    return moment.replace(tzinfo=timezone.utc) if moment is not None and moment.tzinfo is None else moment


def _schedules(value) -> tuple:
    """A unit's schedules from its pod (listed with recurringDays; made with recurOnDaysOfWeek)."""
    schedules = []
    for item in value if isinstance(value, list) else ():
        if not isinstance(item, dict) or not SCHEDULE_ID.fullmatch(str(item.get("id") or "")):
            continue
        days = item.get("recurringDays", item.get("recurOnDaysOfWeek")) or []
        days = tuple(day.lower() for day in days if isinstance(day, str) and day.lower() in DAYS)
        when = item.get("targetTimeLocal")
        upcoming = item.get("nextTime")
        schedules.append(Schedule(
            id=item["id"], time=when[:5] if isinstance(when, str) else "", days=days,
            enabled=item.get("isEnabled") is not False,
            state=item.get("acState") if isinstance(item.get("acState"), dict) else {},
            next_at=_time(upcoming) if isinstance(upcoming, dict) else _time({"time": upcoming}),
        ))
    return tuple(schedules)


def _filters(value) -> tuple[bool | None, float | None]:
    """A unit's filter reminder from its pod: (due, hours run since the last clean)."""
    if not isinstance(value, dict):
        return None, None
    due = value.get("shouldCleanFilters")
    seconds = _number(value.get("acOnSecondsSinceLastFiltersClean"))
    return (due if isinstance(due, bool) else None), (round(seconds / 3600, 1) if seconds is not None else None)


def _climate_react(value) -> "ClimateReact | None":
    """A unit's Climate React from its pod; None when it was never set up (null or {})."""
    if not isinstance(value, dict) or not value:
        return None
    state = lambda key: value.get(key) if isinstance(value.get(key), dict) else {}
    return ClimateReact(
        enabled=value.get("enabled") is True,
        type=value.get("type") if value.get("type") in ("temperature", "feelsLike", "humidity") else "temperature",
        low=_number(value.get("lowTemperatureThreshold")), low_state=state("lowTemperatureState"),
        high=_number(value.get("highTemperatureThreshold")), high_state=state("highTemperatureState"),
    )


def _timer(value) -> tuple[datetime | None, bool | None]:
    """A unit's timer from its pod: (when, on or off), or (None, None) without one."""
    if not isinstance(value, dict):
        return None, None
    when = value.get("targetTime")
    when = _time(when) if isinstance(when, dict) else _time({"time": when})
    state = value.get("acState") if isinstance(value.get("acState"), dict) else {}
    return when, state.get("on") if isinstance(state.get("on"), bool) else None


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
        timer_at, timer_on = _timer(pod.get("timer"))
        rooms.append(Room(
            id=pod["id"], name=name, online=bool((pod.get("connectionStatus") or {}).get("isAlive")),
            on=state.get("on") if isinstance(state.get("on"), bool) else None,
            mode=state.get("mode") if isinstance(state.get("mode"), str) else None,
            target=target if isinstance(target, int) and not isinstance(target, bool) else None, unit=unit,
            fan=state.get("fanLevel") if isinstance(state.get("fanLevel"), str) else None,
            swing=state.get("swing") if isinstance(state.get("swing"), str) else None,
            temperature=_number(readings.get("temperature")), humidity=_number(readings.get("humidity")),
            feels_like=_number(readings.get("feelsLike")), measured_at=_time(readings.get("time")),
            modes=_modes(pod.get("remoteCapabilities"), unit), timer_at=timer_at, timer_on=timer_on,
            schedules=_schedules(pod.get("schedules")), react=_climate_react(pod.get("smartMode")),
            **dict(zip(("filters_due", "filter_hours"), _filters(pod.get("filtersCleaning")))),
        ))
    return rooms


def _nearest(value: int | None, allowed: list[int]) -> int:
    return allowed[len(allowed) // 2] if value is None else min(allowed, key=lambda option: (abs(option - value), option))


def plan_state(room: Room, *, on: bool | None = None, mode: str | None = None, temperature: int | None = None,
               fan: str | None = None, swing: str | None = None) -> tuple[dict, list[str]]:
    """The whole state to send for a change (None leaves a part as it is), checked against what the
    unit supports, and notes on anything adjusted to fit (a fan level the new mode doesn't have).
    ValueError, saying what's allowed, for a value it doesn't take."""
    if all(part is None for part in (on, mode, temperature, fan, swing)):
        raise ValueError("Say what to change: on or off, mode, temperature, fan or swing.")
    new_mode = mode or room.mode
    if new_mode is None:
        raise ValueError(f"{room.name}'s mode isn't known; say which mode.")
    supported = room.modes
    if supported and new_mode not in supported:
        raise ValueError(f"{room.name} has no {new_mode} mode; it has {', '.join(supported)}.")
    caps = supported.get(new_mode, {})
    notes = []
    temperatures = caps.get("temperatures", [])
    target = room.target
    if temperature is not None:
        if supported and not temperatures:
            raise ValueError(f"{new_mode} mode has no temperature to set.")
        if temperatures and temperature not in temperatures:
            raise ValueError(f"{room.name} takes {min(temperatures)} to {max(temperatures)}°{room.unit} in "
                             f"{new_mode} mode, in whole degrees.")
        target = temperature
    elif temperatures and target not in temperatures:
        target = _nearest(target, temperatures)
        notes.append(f"{room.name}'s temperature set to {target}°{room.unit}, the nearest {new_mode} mode takes.")

    def pick(part: str, wanted: str | None, current: str | None, allowed: list[str]) -> str | None:
        if wanted is not None:
            if allowed and wanted not in allowed:
                raise ValueError(f"{room.name}'s {part} in {new_mode} mode is one of {', '.join(allowed)}.")
            return wanted
        if not allowed or current in allowed:
            return current
        chosen = "auto" if "auto" in allowed else allowed[0]
        notes.append(f"{room.name}'s {part} set to {chosen}: {new_mode} mode has no {current}.")
        return chosen

    state = {"on": bool(room.on) if on is None else on, "mode": new_mode, "temperatureUnit": room.unit}
    chosen_fan = pick("fan", fan, room.fan, caps.get("fan", []))
    chosen_swing = pick("swing", swing, room.swing, caps.get("swing", []))
    if chosen_fan is not None:
        state["fanLevel"] = chosen_fan
    if chosen_swing is not None:
        state["swing"] = chosen_swing
    if target is not None and (temperatures or not supported):
        state["targetTemperature"] = target
    return state, notes


def plan_climate_react(room: Room, low: float, high: float, *, mode: str | None = None,
                       temperature: int | None = None, fan: str | None = None) -> tuple[dict, list[str]]:
    """Climate React keeping a room between low and high (in its own unit): cooling (or heating)
    on one side, the unit off on the other. The body to send, and notes on anything adjusted to fit;
    ValueError, saying what's allowed, otherwise."""
    mode = mode or "cool"
    if mode not in ("cool", "heat"):
        raise ValueError("Climate React here keeps a room in range by cooling or heating.")
    # Sensibo's thresholds are in °C.
    to_celsius = (lambda degrees: round((degrees - 32) * 5 / 9, 1)) if room.unit == "F" else (lambda degrees: degrees)
    bottom, top = to_celsius(low), to_celsius(high)
    if not REACT_LOWEST <= bottom < top <= REACT_HIGHEST or top - bottom < REACT_MIN_GAP:
        raise ValueError(f"Keep the range between {REACT_LOWEST:g} and {REACT_HIGHEST:g}°C, the top at least "
                         f"{REACT_MIN_GAP:g} degree above the bottom.")
    temperatures = room.modes.get(mode, {}).get("temperatures", [])
    if temperature is None and temperatures:
        # Without one, the unit aims at the middle of the range.
        temperature = _nearest(round((low + high) / 2), temperatures)
    working, notes = plan_state(room, on=True, mode=mode, temperature=temperature, fan=fan)
    idle = {**working, "on": False}
    on_low, on_high = (idle, working) if mode == "cool" else (working, idle)
    return {"enabled": True, "deviceUid": room.id, "type": "temperature",
            "lowTemperatureThreshold": bottom, "lowTemperatureState": on_low,
            "highTemperatureThreshold": top, "highTemperatureState": on_high}, notes


def describe(state: dict, unit: str = "C") -> str:
    """A state as she says it: "cool, 23°C, fan auto" or "off"."""
    if not state.get("on"):
        return "off"
    parts = [state.get("mode") or "on"]
    if state.get("targetTemperature") is not None:
        parts.append(f"{state['targetTemperature']}°{state.get('temperatureUnit') or unit}")
    if state.get("fanLevel"):
        parts.append(f"fan {state['fanLevel']}")
    return ", ".join(parts)


def with_measurements(room: Room, measurements) -> Room | None:
    """A room with newer readings from the stream's `measurements` event; None if they're unusable."""
    if not isinstance(measurements, dict):
        return None
    temperature, humidity = _number(measurements.get("temperature")), _number(measurements.get("humidity"))
    if temperature is None and humidity is None:
        return None
    return replace(
        room, temperature=temperature if temperature is not None else room.temperature,
        humidity=humidity if humidity is not None else room.humidity,
        feels_like=_number(measurements.get("feelsLike")) or room.feels_like,
        measured_at=_time(measurements.get("time")) or datetime.now(timezone.utc),
    )


# Her room changing this much since she was last told is worth a quiet note, at most this often.
NOTE_DEGREES = 2.0
NOTE_HUMIDITY = 15.0
NOTE_MIN_SECONDS = 15 * 60


def noticeable(told: tuple, room: Room, now: float) -> bool:
    """Whether her room has changed enough since she was told (temperature, humidity, when) to tell
    her again: 2 degrees or 15 points of humidity, and not within 15 minutes of the last note."""
    temperature, humidity, at = told
    if now - at < NOTE_MIN_SECONDS:
        return False
    warmer = room.temperature is not None and temperature is not None and abs(room.temperature - temperature) >= NOTE_DEGREES
    damper = room.humidity is not None and humidity is not None and abs(room.humidity - humidity) >= NOTE_HUMIDITY
    return warmer or damper


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
    if room.timer_at is not None and room.timer_at > now:
        minutes = max(1, round((room.timer_at - now).total_seconds() / 60))
        what = {True: "turns on", False: "turns off"}.get(room.timer_on, "changes")
        shown["timer"] = f"{what} in {minutes} minutes"
    if room.filters_due:
        shown["filters"] = "due for a clean" + filter_run(room)
    return shown


def filter_run(room: Room) -> str:
    """" (312 hours of running since the last clean)", or "" when the unit doesn't say."""
    return f" ({room.filter_hours:.0f} hours of running since the last clean)" if room.filter_hours is not None else ""


def days_words(days) -> str:
    """A schedule's days as said: "every day", "weekdays", "weekends", "mon, wed" or "once"."""
    chosen = set(days)
    if not chosen:
        return "once"
    if chosen == set(DAYS):
        return "every day"
    if chosen == set(DAYS[:5]):
        return "weekdays"
    if chosen == set(DAYS[5:]):
        return "weekends"
    return ", ".join(day[:3] for day in DAYS if day in chosen)


def schedule_words(schedule: Schedule, unit: str = "C") -> str:
    """"16:00 weekdays: cool, 24°C, fan auto", with "(paused)" when it is."""
    paused = "" if schedule.enabled else " (paused)"
    return f"{schedule.time} {days_words(schedule.days)}: {describe(schedule.state, unit)}{paused}"


def react_words(react: ClimateReact, unit: str = "C") -> str:
    """"keeps 22–25°C: cool, 23°C, fan auto above 25, off below 22", or "off" ahead of it when off."""
    watched = {"temperature": "°C", "feelsLike": "°C feels-like", "humidity": "% humidity"}[react.type]
    low, high = f"{react.low:g}" if react.low is not None else "?", f"{react.high:g}" if react.high is not None else "?"
    shown = (f"keeps {low}–{high}{watched}: {describe(react.high_state, unit)} above {high}, "
             f"{describe(react.low_state, unit)} below {low}")
    return shown if react.enabled else f"off (saved: {shown})"


def plans(room: Room) -> dict:
    """A room's schedules and Climate React as Cora is told them (each schedule with its id, which
    removing, pausing or resuming it needs); empty without any."""
    shown = {}
    if room.schedules:
        shown["schedules"] = [{"id": schedule.id, "when": schedule_words(schedule, room.unit)}
                              for schedule in room.schedules]
    if room.react is not None:
        shown["climate_react"] = react_words(room.react, room.unit)
    return shown


def room_context(room: Room, now: datetime, local_now: datetime) -> str:
    """Her own room, for her instructions: one short line of JSON (its schedules aren't in it)."""
    shown = reading(room, now)
    shown["as_of"] = local_now.strftime("%Y-%m-%d %H:%M")
    return json.dumps(shown, ensure_ascii=False)


def refusal(response) -> str:
    """Sensibo's reason for refusing a change, short, when it gives one: ": Bad temperature"."""
    try:
        body = response.json()
    except ValueError:
        return ""
    reason = body.get("reason") or body.get("message") if isinstance(body, dict) else None
    return f": {' '.join(str(reason).split())[:120]}" if reason else ""


def local_time_zone() -> str:
    """This PC's time zone as Sensibo takes it ("Australia/Perth"), from the Windows name through the
    ICU Windows ships; "" when it can't be worked out."""
    try:
        import ctypes
        from zoneinfo import ZoneInfo

        from mail.calendar_events import local_zone
        convert = ctypes.CDLL("icu.dll").ucal_getTimeZoneIDForWindowsID
        convert.restype = ctypes.c_int32
        convert.argtypes = [ctypes.c_wchar_p, ctypes.c_int32, ctypes.c_char_p, ctypes.c_wchar_p, ctypes.c_int32,
                            ctypes.POINTER(ctypes.c_int)]
        found, status = ctypes.create_unicode_buffer(64), ctypes.c_int(0)
        length = convert(local_zone(), -1, None, found, 64, ctypes.byref(status))
        name = found.value[:length] if status.value <= 0 else ""
        ZoneInfo(name)
        return name
    except Exception:
        return ""


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
            try:
                response = await self._call("GET", f"{API}/users/me/pods", params={"fields": FIELDS})
                if response.status_code != 200:
                    raise SensiboError(f"Sensibo answered {response.status_code}.")
                try:
                    rooms = parse_rooms(response.json())
                except ValueError as error:
                    raise SensiboError("Sensibo's answer wasn't understood.") from error
            except BadKey:
                raise
            except SensiboError:
                if self.cached is not None:
                    return self.cached, self.clock() - self.cached_at
                raise
            self.cached, self.cached_at = rooms, self.clock()
            return rooms, 0.0

    async def set_state(self, unit_id: str, state: dict) -> dict:
        """Sets a unit's whole state (see plan_state); Sensibo answers once the unit has it. The
        resulting state, or SensiboError with Sensibo's reason."""
        self._check_unit(unit_id)
        async with self.lock:
            response = await self._call("POST", f"{API}/pods/{unit_id}/acStates", body={"acState": state})
            # What was read before no longer holds.
            self.cached = None
        if response.status_code != 200:
            raise SensiboError(f"Sensibo answered {response.status_code}.")
        try:
            result = response.json().get("result") or {}
        except (ValueError, AttributeError):
            result = {}
        if isinstance(result, dict) and result.get("status") not in (None, "Success"):
            raise SensiboError(f"The unit didn't take it ({result.get('failureReason') or result.get('status')}).")
        resulting = result.get("acState") if isinstance(result, dict) else None
        return resulting if isinstance(resulting, dict) else state

    async def set_timer(self, unit_id: str, minutes: int, state: dict) -> None:
        """In `minutes`, the unit goes to `state` (one timer per unit; a new one replaces it)."""
        self._check_unit(unit_id)
        async with self.lock:
            response = await self._call("PUT", f"{API_V1}/pods/{unit_id}/timer/",
                                        body={"minutesFromNow": minutes, "acState": state})
            self.cached = None
        if response.status_code not in (200, 201):
            raise SensiboError(f"Sensibo answered {response.status_code}.")

    async def cancel_timer(self, unit_id: str) -> bool:
        """Cancels a unit's timer; False when it had none (Sensibo answers 404 then)."""
        self._check_unit(unit_id)
        async with self.lock:
            response = await self._call("DELETE", f"{API_V1}/pods/{unit_id}/timer/")
            self.cached = None
        if response.status_code == 404:
            return False
        if response.status_code not in (200, 204):
            raise SensiboError(f"Sensibo answered {response.status_code}.")
        return True

    async def home_location(self) -> tuple[float, float] | None:
        """The home's location from the Sensibo account, rounded to one decimal place (about 10 km) as
        soon as it's read; None when the account has none."""
        async with self.lock:
            response = await self._call("GET", f"{API}/users/me/pods", params={"fields": "id,location"})
        if response.status_code != 200:
            raise SensiboError(f"Sensibo answered {response.status_code}.")
        try:
            pods = response.json().get("result") or []
        except (ValueError, AttributeError) as error:
            raise SensiboError("Sensibo's answer wasn't understood.") from error
        for pod in pods:
            place = (pod.get("location") or {}).get("latLon") if isinstance(pod, dict) else None
            if (isinstance(place, list) and len(place) == 2
                    and all(isinstance(part, (int, float)) and not isinstance(part, bool) for part in place)
                    and -90 <= place[0] <= 90 and -180 <= place[1] <= 180):
                return round(place[0], 1), round(place[1], 1)
        return None

    async def add_schedule(self, unit_id: str, at: str, zone: str, days, state: dict) -> None:
        """At `at` ("16:00") in `zone` (such as "Australia/Perth"), on `days` (none: once), the unit
        goes to `state`."""
        self._check_unit(unit_id)
        await self._write("POST", f"{API_V1}/pods/{unit_id}/schedules/", {
            "targetTimeLocal": at, "timezone": zone, "acState": state, "recurOnDaysOfWeek": list(days)})

    async def remove_schedule(self, unit_id: str, schedule_id: str) -> bool:
        """Removes one; False when it was already gone (Sensibo answers 404 then)."""
        self._check_unit(unit_id)
        self._check_schedule(schedule_id)
        return await self._write("DELETE", f"{API_V1}/pods/{unit_id}/schedules/{schedule_id}", None, missing_ok=True)

    async def switch_schedule(self, unit_id: str, schedule_id: str, enabled: bool) -> None:
        """Pauses (False) or resumes (True) one, keeping it."""
        self._check_unit(unit_id)
        self._check_schedule(schedule_id)
        await self._write("PUT", f"{API_V1}/pods/{unit_id}/schedules/{schedule_id}", {"isEnabled": enabled})

    async def set_climate_react(self, unit_id: str, body: dict) -> None:
        """Sets up a unit's Climate React (see plan_climate_react)."""
        self._check_unit(unit_id)
        await self._write("POST", f"{API}/pods/{unit_id}/smartmode", body)

    async def switch_climate_react(self, unit_id: str, enabled: bool) -> None:
        """Turns a unit's Climate React off or back on, keeping its settings."""
        self._check_unit(unit_id)
        await self._write("PUT", f"{API}/pods/{unit_id}/smartmode", {"enabled": enabled})

    async def reset_filters(self, unit_id: str) -> None:
        """Clears a unit's filter reminder, once they're cleaned (not in Sensibo's reference; as Home
        Assistant's library does it)."""
        self._check_unit(unit_id)
        await self._write("DELETE", f"{API}/pods/{unit_id}/cleanFiltersNotification", None)

    async def _write(self, method: str, url: str, body: dict | None, missing_ok: bool = False) -> bool:
        """A change; what was read before no longer holds. False for a 404 when missing_ok; SensiboError
        with Sensibo's reason when it's refused."""
        async with self.lock:
            response = await self._call(method, url, body=body)
            self.cached = None
        if missing_ok and response.status_code == 404:
            return False
        if response.status_code not in (200, 201, 204):
            raise SensiboError(f"Sensibo answered {response.status_code}{refusal(response)}.")
        return True

    @staticmethod
    def _check_unit(unit_id: str) -> None:
        if not UNIT_ID.fullmatch(unit_id or ""):
            raise SensiboError("Unknown unit.")

    @staticmethod
    def _check_schedule(schedule_id: str) -> None:
        if not SCHEDULE_ID.fullmatch(schedule_id or ""):
            raise SensiboError("Unknown schedule.")

    async def _call(self, method: str, url: str, *, params: dict | None = None, body: dict | None = None):
        """One request, at least MIN_GAP_SECONDS after the last; the caller holds the lock. Raises
        BadKey or RateLimited for those answers, SensiboError when it fails; otherwise the response."""
        if not self.key:
            raise BadKey("No Sensibo API key is saved (Settings > Services > Sensibo).")
        wait = MIN_GAP_SECONDS - (self.clock() - self.last_request)
        if wait > 0:
            await self.sleep(wait)
        self.last_request = self.clock()
        options = {"timeout": TIMEOUT_SECONDS, "headers": {"Accept-Encoding": "gzip"}}
        if self.transport is not None:
            options["transport"] = self.transport
        try:
            async with httpx2.AsyncClient(**options) as client:
                response = await client.request(method, url, params={"apiKey": self.key, **(params or {})},
                                                json=body)
        except httpx2.TimeoutException as error:
            raise SensiboError("Sensibo didn't answer in time.") from error
        except httpx2.HTTPError as error:
            # Only the type: the key is in the request's address.
            raise SensiboError(f"Sensibo couldn't be reached ({type(error).__name__}).") from error
        if response.status_code in (401, 403):
            raise BadKey("Sensibo refused the API key; check it in Settings > Services > Sensibo.")
        if response.status_code == 429:
            raise RateLimited("Sensibo is limiting requests just now; try again in a minute.")
        return response
