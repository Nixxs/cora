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
(CoraMapPins/tests/bridge_lines.txt) that tests/test_game_map_pins.py builds.

The addon's other commands (clear, done, tracker and so on, for wow_map_pins_command) are built by
command_line: one of COMMANDS, with an argument checked and rebuilt for that command, as one
"/cmp <command> <argument>" line. Anything else, such as chat, another slash command or a second
command, is refused rather than cleaned."""

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


# The addon's own commands Cora may type, one line each: {command: what its argument is}.
#   none: nothing after the command; waypoint: [zone or #mapID] x y [label]; label: plain text,
#   may be empty; route: a route name, may be empty (the active one, or every one for list);
#   clear: a route name or "all"; id: a pin id; step: a step number, may be empty (the next one);
#   tracker: on, off or reset; switch: on or off.
# Left out on purpose: "import" and "export" open a window for the person to paste into or copy
# from, which takes the keyboard; "cora" is place_wow_map_pins' bridge; the aliases (rm, ls, go...).
COMMANDS = {
    "add": "waypoint", "here": "label", "list": "route", "route": "route", "remove": "id", "clear": "clear",
    "next": "none", "show": "step", "done": "step", "undo": "step", "reset": "route", "way": "step",
    "tracker": "tracker", "labels": "switch", "status": "none", "mapdebug": "none", "help": "none",
}
CLEAR_ALL = "all"
SWITCHES = {"tracker": ("on", "off", "reset"), "switch": ("on", "off")}
# Pin ids and step numbers: whole numbers, as the addon's /cmp list shows them.
MAX_NUMBER = 999999
NUMBER = re.compile(r"[1-9][0-9]{0,5}")
# A waypoint as /cmp add reads it: an optional zone (or #mapID), x and y, then an optional label.
WAYPOINT = re.compile(
    r"(?:(?P<zone>#[0-9]{1,6}|[A-Za-z][A-Za-z' -]*?)\s+)?"
    r"(?P<x>[0-9]{1,3}(?:\.[0-9]+)?)\s*,?\s+(?P<y>[0-9]{1,3}(?:\.[0-9]+)?)(?:\s+(?P<label>.*))?",
    re.DOTALL,
)


def one_line(value, what: str) -> str:
    """The argument as given, spacing tidied; ValueError for anything that isn't one plain line."""
    if not isinstance(value, str):
        raise ValueError(f"{what} must be text")
    # Control characters include line breaks: a second line would be a second command.
    if "|" in value or any(unicodedata.category(ch).startswith("C") for ch in value):
        raise ValueError(f"{what} must be one line of plain text, without '|'")
    return " ".join(value.translate(PLAIN).split())


def number(text: str, what: str) -> str:
    if not NUMBER.fullmatch(text):
        raise ValueError(f"{what} must be a whole number from 1 to {MAX_NUMBER}")
    return text


def waypoint(text: str) -> str:
    match = WAYPOINT.fullmatch(text)
    if not match:
        raise ValueError("add needs '[zone] <x> <y> [label]', such as 'Westfall 56.3 47.5 Sentinel Hill'")
    zone = match["zone"]
    if zone and not zone.startswith("#"):
        zone = zone_name(zone)
    x, y = coordinate(float(match["x"]), "x"), coordinate(float(match["y"]), "y")
    label = plain_text(match["label"] or "", MAX_LABEL_CHARS, "label")
    return " ".join(part for part in (zone, f"{x:.1f}", f"{y:.1f}", label) if part)


def command_line(command, argument) -> str:
    """The one line to type for the addon's `command` with `argument` ("" for none), such as
    "/cmp clear all"; ValueError for any command or argument the addon's command doesn't take."""
    if not isinstance(command, str) or command not in COMMANDS:
        raise ValueError(f"command must be one of: {', '.join(COMMANDS)}")
    kind = COMMANDS[command]
    text = one_line(argument, "argument")
    if kind == "none":
        if text:
            raise ValueError(f"{command} takes no argument")
    elif kind == "waypoint":
        text = waypoint(text)
    elif kind == "label":
        text = plain_text(text, MAX_LABEL_CHARS, "label")
    elif kind == "route":
        text = plain_text(text, MAX_ROUTE_CHARS, "route") if text else ""
    elif kind == "clear":
        if not text:
            raise ValueError("clear needs a route name, or 'all' for every pin")
        text = CLEAR_ALL if text.lower() == CLEAR_ALL else route_name(text)
    elif kind == "id":
        text = number(text, "remove's pin id")
    elif kind == "step":
        text = number(text, f"{command}'s step") if text else ""
    else:
        text = text.lower()
        if text not in SWITCHES[kind]:
            raise ValueError(f"{command} needs one of: {', '.join(SWITCHES[kind])}")
    line = f"/cmp {command} {text}".rstrip()
    if len(line) > MAX_MESSAGE_CHARS:  # can't happen with the limits above; kept as a guard
        raise ValueError("the command is too long for WoW's chat box")
    return line


# Every line Cora types for the addon: "/cmp", a command word, and plain one-line text.
ADDON_LINE = re.compile(r"/cmp [a-z]+(?: [^|]*[^|\s])?")


def is_addon_line(line) -> bool:
    """A last check before typing: one of the addon's lines, never chat or another command."""
    return (isinstance(line, str) and len(line) <= MAX_MESSAGE_CHARS and ADDON_LINE.fullmatch(line) is not None
            and line.split(" ")[1] in (*COMMANDS, "cora")
            and not any(unicodedata.category(ch).startswith("C") for ch in line))
