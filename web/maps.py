"""Maps on the web page (WB7): a place, a route or an area search, shown with Google's Maps Embed API.

The embed needs a Google Cloud API key, set in Settings › Services › Web interface and kept in the
system credential store. The page has to show it (every embedded map does), so it should allow
only the Maps Embed API and only Cora's own address; then it's useless anywhere else. Google Maps
links ("Open in Google Maps", the TUI's line) need no key.

Blocking calls (the credential store): run them off the UI thread.
"""

from dataclasses import dataclass
import re
from urllib.parse import urlencode

import keyring
from keyring.errors import PasswordDeleteError

KEYRING_SERVICE = "cora"
KEY_ACCOUNT = "web:google-maps-key"
# Google's API keys are 39 characters ("AIza…"); a little room either way, in their alphabet only.
KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{30,60}")

PLACE, DIRECTIONS, SEARCH = "place", "directions", "search"
MODES = (PLACE, DIRECTIONS, SEARCH)
TRAVEL_MODES = ("driving", "walking", "bicycling", "transit")
EMBED_URL = "https://www.google.com/maps/embed/v1/"
LINK_URL = "https://www.google.com/maps/"


def maps_key() -> str:
    """The Google Maps key, or "" when none is set."""
    return keyring.get_password(KEYRING_SERVICE, KEY_ACCOUNT) or ""


def save_maps_key(value: str) -> str:
    """Saves it trimmed; empty forgets it. ValueError when it doesn't look like a Google API key."""
    value = value.strip()
    if not value:
        try:
            keyring.delete_password(KEYRING_SERVICE, KEY_ACCOUNT)
        except PasswordDeleteError:
            pass  # nothing was saved
        return ""
    if not KEY_PATTERN.fullmatch(value):
        raise ValueError("That doesn't look like a Google API key (they start with AIza).")
    keyring.set_password(KEYRING_SERVICE, KEY_ACCOUNT, value)
    return value


@dataclass(frozen=True)
class MapView:
    """What to map: a place (place), a route to place from origin (directions), or a search."""

    mode: str
    place: str
    origin: str = ""
    travel: str = "driving"

    def caption(self) -> str:
        if self.mode == DIRECTIONS:
            return f"Route from {self.origin} to {self.place} ({self.travel})"
        if self.mode == SEARCH:
            return f"Search: {self.place}"
        return self.place

    def embed_url(self, key: str) -> str:
        """The Maps Embed API address for the page's frame."""
        if self.mode == DIRECTIONS:
            query = {"key": key, "origin": self.origin, "destination": self.place, "mode": self.travel}
        else:
            query = {"key": key, "q": self.place}
        return f"{EMBED_URL}{self.mode}?{urlencode(query)}"

    def link(self) -> str:
        """A Google Maps link (no key): opens the Maps app on a phone, or Google Maps in a browser."""
        if self.mode == DIRECTIONS:
            query = {"api": 1, "origin": self.origin, "destination": self.place, "travelmode": self.travel}
            return f"{LINK_URL}dir/?{urlencode(query)}"
        return f"{LINK_URL}search/?{urlencode({'api': 1, 'query': self.place})}"
