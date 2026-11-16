"""Map pins in the person's World of Warcraft game, placed through their CoraMapPins addon.

There's no other way into the game than the chat box (see chat_line.py), so a route becomes a few
slash commands for the addon, each typed as its own line:

    /cmp cora begin <count> <route>
    /cmp cora pin <zone> <x> <y> <label>      (one per step)
    /cmp cora end

The addon places nothing until "end", so a batch cut off part-way changes nothing. Everything typed
is built here from checked values: a zone name of letters, coordinates as numbers, and a label and
route name cleaned to plain one-line text. Cora's words can only ever become those three commands,
never a chat message or another command. The addon's tests read the same example lines
(CoraMapPins/tests/bridge_lines.txt) that tests/test_game_map_pins.py builds."""

import math
import re
import unicodedata

from game.chat_line import MAX_MESSAGE_CHARS

COMMAND = "/cmp cora"
MAX_STEPS = 10
MAX_ROUTE_CHARS = 40
MAX_ZONE_CHARS = 40
MAX_LABEL_CHARS = 80
# Zone names as the world map shows them: "Elwynn Forest", "Un'Goro Crater", "Hillsbrad Foothills".
ZONE = re.compile(r"[A-Za-z](?:[A-Za-z' -]*[A-Za-z])?")
# Typographic punctuation a model likes, as the plain characters WoW's font is sure to have.
PLAIN = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-",
                       "\u2014": "-", "\u2026": "...", "\u00a0": " "})


def plain_text(text, limit: int, what: str) -> str:
    """One line of plain text: no "|" (WoW's escape character), no control or emoji characters,
    single spaces, and cut at a word to `limit`. ValueError if it isn't text."""
    if not isinstance(text, str):
        raise ValueError(f"{what} must be text")
    text = " ".join(text.translate(PLAIN).replace("|", " ").split())
    # Control and format characters, and emoji and other pictographs, which WoW's font doesn't have.
    text = "".join(ch for ch in text
                   if ord(ch) <= 0xFFFF and not unicodedata.category(ch).startswith("C")
                   and unicodedata.category(ch) != "So")
    text = " ".join(text.split())
    if len(text) > limit:
        cut = text[:limit + 1].rsplit(" ", 1)[0]
        text = (cut if cut and len(cut) <= limit else text[:limit]).rstrip()
    return text


def zone_name(value) -> str:
    """Checked as given (only spacing and curly apostrophes tidied), never cleaned into another name."""
    if not isinstance(value, str):
        raise ValueError("zone must be text")
    zone = " ".join(value.translate(PLAIN).split())
    if not 2 <= len(zone) <= MAX_ZONE_CHARS or not ZONE.fullmatch(zone):
        raise ValueError("zone must be a zone name of letters, spaces, apostrophes or hyphens, such as "
                         "'Arathi Highlands'")
    return zone


def coordinate(value, what: str) -> float:
    """A map percentage, 0 to 100, to one decimal place."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{what} must be a number")
    if not 0 <= value <= 100:
        raise ValueError(f"{what} must be a map percentage from 0 to 100")
    return round(float(value), 1)


def route_name(value) -> str:
    name = plain_text(value, MAX_ROUTE_CHARS, "route")
    if not name:
        raise ValueError("route needs a name")
    return name


def pin_lines(route, steps) -> list[str]:
    """The lines to type for a route; ValueError for anything that isn't a valid route."""
    name = route_name(route)
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise ValueError(f"steps must be a list of 1 to {MAX_STEPS} steps")
    lines = [f"{COMMAND} begin {len(steps)} {name}"]
    for number, step in enumerate(steps, 1):
        if not isinstance(step, dict) or set(step) != {"zone", "x", "y", "label"}:
            raise ValueError(f"step {number} needs exactly zone, x, y and label")
        zone = zone_name(step["zone"])
        x, y = coordinate(step["x"], "x"), coordinate(step["y"], "y")
        label = plain_text(step["label"], MAX_LABEL_CHARS, "label")
        lines.append(f"{COMMAND} pin {zone} {x:.1f} {y:.1f} {label}".rstrip())
    lines.append(f"{COMMAND} end")
    for line in lines:
        if len(line) > MAX_MESSAGE_CHARS:  # can't happen with the limits above; kept as a guard
            raise ValueError("a line is too long for WoW's chat box")
    return lines
