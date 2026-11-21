"""Today's forecast for the house, from Open-Meteo (https://open-meteo.com): free, no key, and for
Australia drawn from the Bureau of Meteorology's models among others.

Only the home's rough location goes: the coordinates on the Sensibo account, rounded to one decimal
place (about 10 km), so it doesn't pinpoint the house. Asked once a day, for the hot-day offer.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx2

URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT_SECONDS = 15.0
# How far ahead of the hottest hour to start cooling a room.
LEAD_HOURS = 2


@dataclass(frozen=True)
class HotDay:
    """Today's peak: how hot, when (local), and when to start cooling ahead of it."""

    peak: float
    peak_at: datetime
    start_at: datetime


def rounded(latitude: float, longitude: float) -> tuple[float, float]:
    """About 10 km: enough for the weather, not enough to find the house."""
    return round(latitude, 1), round(longitude, 1)


def hot_day(body, threshold: float, now: datetime) -> HotDay | None:
    """From Open-Meteo's hourly forecast (local times): today's peak if it reaches threshold and is
    still ahead, with when to start cooling (LEAD_HOURS before it, not before now); else None."""
    hourly = body.get("hourly") if isinstance(body, dict) else None
    if not isinstance(hourly, dict):
        raise ValueError("Unexpected answer from Open-Meteo")
    hours, temperatures = hourly.get("time") or [], hourly.get("temperature_2m") or []
    today = [(datetime.fromisoformat(at), temperature) for at, temperature in zip(hours, temperatures)
             if isinstance(at, str) and isinstance(temperature, (int, float))
             and datetime.fromisoformat(at).date() == now.date()]
    if not today:
        return None
    peak_at, peak = max(today, key=lambda hour: hour[1])
    if peak < threshold or peak_at <= now:
        return None
    start = peak_at - timedelta(hours=LEAD_HOURS)
    # The next whole hour, if the lead time has already begun.
    soonest = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    return HotDay(peak=float(peak), peak_at=peak_at, start_at=max(start, soonest))


async def forecast(latitude: float, longitude: float, *, transport=None) -> dict:
    """Today's hourly temperatures where the house is (rounded), in its own local time."""
    latitude, longitude = rounded(latitude, longitude)
    options = {"timeout": TIMEOUT_SECONDS}
    if transport is not None:
        options["transport"] = transport
    async with httpx2.AsyncClient(**options) as client:
        response = await client.get(URL, params={"latitude": latitude, "longitude": longitude,
                                                 "hourly": "temperature_2m", "timezone": "auto",
                                                 "forecast_days": 1})
    response.raise_for_status()
    return response.json()
