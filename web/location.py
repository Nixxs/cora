"""Where the person is, on demand, from their open web page (WB6).

Cora asks only when it matters to what they asked. Every open page is asked over its live feed
(see MessageFeed.ask_location); a page with "where" switched on answers with the browser's
position, one with it off says so. The first position wins, and OpenStreetMap's geocoder turns it
into a place name ("Leederville, near Oxford Street, Perth, …"), which is all Cora gets.

Nothing is saved: the last place is held in memory for KEEP_SECONDS, so she doesn't ask again
mid-conversation, and the coordinates themselves are dropped once named.
"""

import asyncio
from dataclasses import dataclass
import math
import secrets
from time import monotonic

import httpx2 as httpx

# How long the pages have to answer: a position fix outdoors takes a few seconds.
ANSWER_SECONDS = 20
# How long a place is reused rather than asking the page again.
KEEP_SECONDS = 10 * 60
# Why there's no place: no page open, every page has "where" off, the device refused the page its
# location, it couldn't get a fix (or no page answered in time), or the place couldn't be named.
NO_PAGE, OFF, DENIED, UNAVAILABLE, UNNAMED = "no_page", "off", "denied", "unavailable", "unnamed"
# When pages decline in different ways, the one most worth telling her about.
DECLINE_ORDER = (DENIED, UNAVAILABLE, OFF)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
# OpenStreetMap asks every app to say who it is; this names the app, not the person.
USER_AGENT = "Cora (self-hosted personal assistant)"
GEOCODER_SECONDS = 10


@dataclass(frozen=True)
class Where:
    place: str
    # Metres either way, as the device reported it.
    accuracy: int
    device: str
    at: float


def valid_fix(body: dict) -> tuple[float, float, float] | None:
    """(latitude, longitude, accuracy) from a page's answer, or None if it isn't a real position."""
    try:
        latitude, longitude, accuracy = (float(body[key]) for key in ("latitude", "longitude", "accuracy"))
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in (latitude, longitude, accuracy)):
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180 and accuracy >= 0):
        return None
    return latitude, longitude, accuracy


def zoom_for(accuracy: float) -> int:
    """How closely to name the place: a street only when the fix is good enough to be sure of it."""
    if accuracy <= 150:
        return 17
    if accuracy <= 2000:
        return 14
    return 10


def place_from(found: dict, zoom: int) -> str:
    """A short place name from the geocoder's address: area, nearby street, town, state, country.

    No house number: the street is "near", as the fix is never that exact."""
    address = found.get("address") or {}

    def first(*keys: str) -> str | None:
        return next((address[key] for key in keys if address.get(key)), None)

    road = first("road", "pedestrian", "footway", "path") if zoom >= 17 else None
    parts = [
        first("amenity", "shop", "tourism", "leisure", "building") if zoom >= 17 else None,
        first("suburb", "neighbourhood", "quarter", "hamlet"),
        f"near {road}" if road else None,
        first("city", "town", "village", "municipality", "county"),
        first("state", "region"),
        first("country"),
    ]
    named: list[str] = []
    for part in parts:
        if part and part not in named:
            named.append(part)
    return ", ".join(named) or str(found.get("display_name") or "")


async def name_place(latitude: float, longitude: float, accuracy: float,
                     client_factory=httpx.AsyncClient) -> str:
    """The place at a position, from OpenStreetMap's geocoder (Nominatim). Raises if it can't."""
    zoom = zoom_for(accuracy)
    async with client_factory(timeout=GEOCODER_SECONDS, headers={"User-Agent": USER_AGENT}) as client:
        response = await client.get(NOMINATIM_URL, params={
            "format": "jsonv2", "lat": f"{latitude:.6f}", "lon": f"{longitude:.6f}", "zoom": zoom,
            "addressdetails": 1, "accept-language": "en",
        })
        response.raise_for_status()
        found = response.json()
    if not isinstance(found, dict) or found.get("error"):
        raise ValueError("no place there")
    place = place_from(found, zoom)
    if not place:
        raise ValueError("no place name")
    return place


class Whereabouts:
    """Asks the open pages where the person is, and holds the answer for a while, in memory only."""

    def __init__(self, ask_pages, name=name_place, clock=monotonic,
                 answer_seconds: float = ANSWER_SECONDS, keep_seconds: float = KEEP_SECONDS) -> None:
        # ask_pages(request_id) -> how many pages were asked.
        self.ask_pages = ask_pages
        self.name = name
        self.clock = clock
        self.answer_seconds = answer_seconds
        self.keep_seconds = keep_seconds
        self.last: Where | None = None
        # Requests waiting on the pages, by id: (the answer, how many pages were asked, their declines).
        self.waiting: dict[str, tuple[asyncio.Future, int, list[str]]] = {}
        # One check at a time, so two questions at once share the page's answer.
        self.lock = asyncio.Lock()

    def recent(self) -> Where | None:
        if self.last is not None and self.clock() - self.last.at > self.keep_seconds:
            self.last = None
        return self.last

    async def check(self, refresh: bool = False) -> tuple[Where | None, str | None, bool]:
        """(where, None, fresh) or (None, why not, False). Held places are reused unless refresh."""
        async with self.lock:
            if not refresh and (held := self.recent()) is not None:
                return held, None, False
            request_id = secrets.token_hex(8)
            answer = asyncio.get_running_loop().create_future()
            asked = self.ask_pages(request_id)
            if asked == 0:
                return None, NO_PAGE, False
            self.waiting[request_id] = (answer, asked, [])
            try:
                result = await asyncio.wait_for(answer, self.answer_seconds)
            except TimeoutError:
                return None, UNAVAILABLE, False
            finally:
                self.waiting.pop(request_id, None)
            if isinstance(result, str):
                return None, result, False
            device, (latitude, longitude, accuracy) = result
            try:
                place = await self.name(latitude, longitude, accuracy)
            except Exception:
                return None, UNNAMED, False
            self.last = Where(place, round(accuracy), device, self.clock())
            return self.last, None, True

    def answer(self, device: str, body: dict) -> bool:
        """A page's answer, {"id", "latitude", "longitude", "accuracy"} or {"id", "error"}; False if
        that request isn't waiting (or the answer makes no sense)."""
        waiting = self.waiting.get(body.get("id")) if isinstance(body.get("id"), str) else None
        if waiting is None:
            return False
        answer, asked, declines = waiting
        if answer.done():
            return True
        if "error" in body:
            error = body["error"] if body["error"] in DECLINE_ORDER else UNAVAILABLE
            declines.append(error)
            if len(declines) >= asked:
                answer.set_result(next(reason for reason in DECLINE_ORDER if reason in declines))
            return True
        fix = valid_fix(body)
        if fix is None:
            return False
        answer.set_result((device, fix))
        return True

    def forget(self) -> None:
        """The conversation ended: the place goes with it."""
        self.last = None
