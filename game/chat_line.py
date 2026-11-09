"""Turning what was said into a line for World of Warcraft's chat, and typing it in.

Typing happens in one SendInput batch (Enter, the line, Enter): Windows never mixes a batch with
the person's own keys, so a held W can't slip "wwww" into the message. Only when the game is the
window in front; Cora never sends anything to the game on her own."""

import ctypes
import os
import re
import sys

# The chat channels offered in Settings > Game: {setting: (slash command, words for Cora)}.
CHANNELS = {
    "say": ("/s", "said aloud in the game (/say, heard by players nearby)"),
    "party": ("/p", "said to their party in the game (/party)"),
    "guild": ("/g", "said to their guild in the game (/guild)"),
    "yell": ("/y", "yelled in the game (/yell, heard across the area)"),
}
DEFAULT_CHANNEL = "say"
# The game's processes, by file name in lower case.
GAME_PROCESSES = ("wow.exe",)
# WoW cuts chat messages at 255 characters.
MAX_MESSAGE_CHARS = 255


def chat_text(spoken: str) -> str:
    """What was said, ready for the chat box: one line, no "|" (WoW's escape character), and cut
    at a word to fit WoW's limit. "" when nothing is left."""
    text = re.sub(r"\s+", " ", spoken.replace("|", "")).strip()
    if len(text) <= MAX_MESSAGE_CHARS:
        return text
    window = text[:MAX_MESSAGE_CHARS + 1]
    cut = window.rsplit(" ", 1)[0] if " " in window else ""
    return (cut or text[:MAX_MESSAGE_CHARS]).rstrip()


def casual(text: str, names=()) -> str:
    """Typed the way players type, not like a bot: sentences start in lower case (except "I",
    acronyms such as LFG, and the given names) and lose their full stops; "?", "!" and "..." stay."""
    keep = {word.casefold() for name in names for word in re.findall(r"[A-Za-z']+", name) if word[:1].isupper()}

    def lower(match: re.Match) -> str:
        word = match.group(2)
        if word == "I" or word.startswith("I'") or (len(word) > 1 and word.isupper()) or word.casefold() in keep:
            return match.group(0)
        return match.group(1) + word[0].lower() + word[1:]

    text = re.sub(r"(^|[.!?]\s+)([A-Za-z][A-Za-z']*)", lower, text.strip())
    # A full stop straight after a word, ending a sentence; never one of "...".
    return re.sub(r"(?<=\w)\.(?=\s|$)", "", text)


def chat_line(text: str, channel: str) -> str:
    """The whole line typed into the chat box, such as "/s hello there"."""
    command, _ = CHANNELS.get(channel, CHANNELS[DEFAULT_CHANNEL])
    return f"{command} {text}"


# Windows' SendInput structures, sized for 32 and 64 bits alike.
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_RETURN = 0x0D
SCAN_RETURN = 0x1C

if sys.platform == "win32":
    from ctypes import wintypes

    ULONG_PTR = ctypes.c_size_t

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def key_events(line: str) -> list[tuple[int, int, int]]:
    """(virtual key, scan code, flags) for each event: Enter, the line's characters, Enter."""
    enter = [(VK_RETURN, SCAN_RETURN, 0), (VK_RETURN, SCAN_RETURN, KEYEVENTF_KEYUP)]
    events = list(enter)
    encoded = line.encode("utf-16-le")
    for index in range(0, len(encoded), 2):
        unit = int.from_bytes(encoded[index:index + 2], "little")
        events += [(0, unit, KEYEVENTF_UNICODE), (0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)]
    return events + enter


def foreground_process() -> str:
    """The file name (lower case) of the program whose window is in front, or ""."""
    if sys.platform != "win32":
        return ""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    kernel32.OpenProcess.restype = wintypes.HANDLE
    window = user32.GetForegroundWindow()
    if not window:
        return ""
    process_id = wintypes.DWORD()
    user32.GetWindowThreadProcessId(window, ctypes.byref(process_id))
    # PROCESS_QUERY_LIMITED_INFORMATION is enough for the file name.
    handle = kernel32.OpenProcess(0x1000, False, process_id.value)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        name = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)):
            return ""
        return os.path.basename(name.value).lower()
    finally:
        kernel32.CloseHandle(handle)


def game_in_front() -> bool:
    return foreground_process() in GAME_PROCESSES


def type_into_game(line: str) -> None:
    """Open the chat box, type the line and send it, all in one batch. Raises OSError when Windows
    took fewer events than sent (such as another program blocking input)."""
    if sys.platform != "win32":
        raise OSError("Typing into a game works on Windows only.")
    events = key_events(line)
    inputs = (INPUT * len(events))()
    for slot, (key, scan, flags) in zip(inputs, events):
        slot.type = INPUT_KEYBOARD
        slot.ki = KEYBDINPUT(key, scan, flags, 0, 0)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendInput.argtypes = (wintypes.UINT, ctypes.c_void_p, ctypes.c_int)
    sent = user32.SendInput(len(events), inputs, ctypes.sizeof(INPUT))
    if sent != len(events):
        raise OSError(f"Windows took {sent} of {len(events)} key events (error {ctypes.get_last_error()}).")
