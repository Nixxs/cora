"""Watching the game: a screenshot every couple of seconds, asked a fixed set of typed questions
through OpenAI's Decisions API. The answers are recorded, and game/moments.py picks out the
moments worth Cora reacting to.

Frames are taken only while the game is the window in front, are never saved, and one that
hasn't changed since the last one sent is skipped (with one sent anyway every so often)."""

import asyncio
import base64
import io
from collections.abc import Callable
from dataclasses import dataclass, field
from time import monotonic

import numpy as np

MODEL = "gpt-6-luna"
# Uncached input; output is free.
DOLLARS_PER_MILLION_INPUT = 0.10
INTERVAL_SECONDS = 2.0
# A frame that changed less than this (mean difference on a tiny greyscale copy, 0-255) is skipped,
# but one is sent at least this often.
UNCHANGED_THRESHOLD = 2.0
MAX_UNCHANGED_SECONDS = 30.0
# 1024 px wide reads the game well at about 1,200 tokens a frame.
FRAME_WIDTH = 1024
JPEG_QUALITY = 70
TIMEOUT_SECONDS = 5.0

CONTEXT = ("A frame from the player's screen while they play World of Warcraft. Their own unit frame "
           "shows their name, level and health; the target's frame shows what they're fighting.")

QUESTIONS = [
    {"type": "choice", "name": "scene", "instructions": "What is the player doing right now?", "choices": [
        {"value": "combat", "description": "Fighting: an enemy targeted and in a fight with the player."},
        {"value": "exploring", "description": "Travelling, questing or gathering in the world, not fighting."},
        {"value": "town", "description": "In a town or camp, near vendors, trainers or quest givers."},
        {"value": "dead", "description": "Dead or a ghost: a release spirit prompt or a grey world."},
        {"value": "menu", "description": "A loading screen, full-screen map, menu, character select or not the game."},
    ]},
    {"type": "score", "name": "health", "instructions": "How full is the player's own health bar (not the target's)?",
     "levels": [
         {"label": "critical", "description": "Under about 20%."},
         {"label": "low", "description": "About 20% to 50%."},
         {"label": "fair", "description": "About 50% to 90%."},
         {"label": "full", "description": "Over about 90%."},
     ]},
    {"type": "predicate", "name": "close_call",
     "instructions": "Is the player in real danger of dying: their own health under about a quarter while fighting?"},
    {"type": "predicate", "name": "dead",
     "instructions": "Is the player dead or a ghost (a release spirit prompt, or the grey ghost world)?"},
    {"type": "predicate", "name": "level_up",
     "instructions": "Is a level-up announcement on screen, such as a 'You have reached level' banner?"},
    {"type": "predicate", "name": "new_area",
     "instructions": "Is a zone or subzone name shown in large text near the top middle of the screen (the "
                     "banner WoW shows on entering a new area)?"},
    {"type": "predicate", "name": "quest_moment",
     "instructions": "Is a quest being accepted or handed in, or did an objective just progress (a quest "
                     "window open, or quest progress text in the middle of the screen)?"},
    {"type": "predicate", "name": "chat_worth_reacting",
     "instructions": "Does the chat box show a message from another player that's funny, asks the player "
                     "something, or invites a reply? System messages such as loot and skill-ups don't count."},
    {"type": "predicate", "name": "surprising",
     "instructions": "Is something unusual or exciting happening: an elite, rare or boss enemy, several "
                     "enemies at once, rare loot, or an unusual event?"},
    {"type": "score", "name": "worth_mentioning",
     "instructions": "How much would a friend watching over the player's shoulder want to comment right now?",
     "levels": [
         {"label": "nothing", "description": "Routine: nothing to say."},
         {"label": "mild", "description": "A passing remark at most."},
         {"label": "interesting", "description": "Worth a comment."},
         {"label": "exciting", "description": "Hard not to react."},
     ]},
]


@dataclass
class Frame:
    """One frame's answers: a predicate's probability, a choice's value or a score's level index;
    None for a refused question."""

    answers: dict[str, float | str | None]
    input_tokens: int
    seconds: float
    # The frame itself, in memory only, for describing a moment worth reacting to.
    jpeg: bytes = field(default=b"", repr=False)

    @property
    def dollars(self) -> float:
        return self.input_tokens * DOLLARS_PER_MILLION_INPUT / 1_000_000


@dataclass
class WatchTotals:
    """What the watcher has done since it started."""

    sent: int = 0
    skipped: int = 0
    failed: int = 0
    input_tokens: int = 0
    seconds: float = 0.0
    started: float = field(default_factory=monotonic)

    @property
    def dollars(self) -> float:
        return self.input_tokens * DOLLARS_PER_MILLION_INPUT / 1_000_000

    def summary(self) -> str:
        average = f", {self.seconds / self.sent:.2f}s each" if self.sent else ""
        hours = max(monotonic() - self.started, 1.0) / 3600
        return (f"{self.sent} frames sent ({self.skipped} unchanged skipped, {self.failed} failed){average}; "
                f"{self.input_tokens:,} tokens, ${self.dollars:.4f} so far (about ${self.dollars / hours:.2f} an hour)")


def frame_image(screenshot) -> tuple[bytes, np.ndarray]:
    """The frame as a JPEG, and a tiny greyscale copy for spotting changes."""
    image = screenshot.convert("RGB")
    image.thumbnail((FRAME_WIDTH, FRAME_WIDTH))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=JPEG_QUALITY)
    tiny = np.asarray(image.convert("L").resize((64, 36)), dtype=np.float32)
    return buffer.getvalue(), tiny


def changed(previous: np.ndarray | None, tiny: np.ndarray) -> bool:
    return previous is None or float(np.mean(np.abs(previous - tiny))) >= UNCHANGED_THRESHOLD


def read_answers(response) -> dict[str, float | str | None]:
    answers = {}
    for answer in response.answers:
        kind = getattr(answer, "type", None)
        value = {"predicate": "probability", "choice": "choice", "score": "score"}.get(kind)
        answers[answer.name] = getattr(answer, value, None) if value else None
    return answers


def ask(client, jpeg: bytes) -> Frame:
    """One Decisions request for one frame; raises on failure."""
    image_url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")
    started = monotonic()
    response = client.decisions.create(
        model=MODEL, questions=QUESTIONS, timeout=TIMEOUT_SECONDS,
        input=[{"role": "user", "content": [
            {"type": "input_text", "text": CONTEXT},
            {"type": "input_image", "image_url": image_url},
        ]}],
    )
    return Frame(read_answers(response), response.usage.input_tokens, monotonic() - started, jpeg)


def grab_screen():
    """The main screen as an image. On Windows it's copied through ctypes, which lets the rest of
    Cora run meanwhile: Pillow's own grab holds Python still for the whole copy, long enough to
    make her voice stutter."""
    import sys

    from PIL import Image

    if sys.platform != "win32":
        from PIL import ImageGrab

        return ImageGrab.grab()

    import ctypes
    from ctypes import wintypes

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    handle = ctypes.c_void_p
    user32.SetThreadDpiAwarenessContext.restype = handle
    user32.SetThreadDpiAwarenessContext.argtypes = [handle]
    user32.GetDC.restype = handle
    user32.GetDC.argtypes = [handle]
    user32.ReleaseDC.argtypes = [handle, handle]
    gdi32.CreateCompatibleDC.restype = handle
    gdi32.CreateCompatibleDC.argtypes = [handle]
    gdi32.CreateCompatibleBitmap.restype = handle
    gdi32.CreateCompatibleBitmap.argtypes = [handle, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.restype = handle
    gdi32.SelectObject.argtypes = [handle, handle]
    gdi32.BitBlt.argtypes = [handle, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             handle, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    gdi32.GetDIBits.argtypes = [handle, handle, wintypes.UINT, wintypes.UINT, handle,
                                ctypes.POINTER(BITMAPINFOHEADER), wintypes.UINT]
    gdi32.DeleteObject.argtypes = [handle]
    gdi32.DeleteDC.argtypes = [handle]

    # Real pixels rather than scaled ones, for this thread only (-4: per-monitor aware, v2).
    awareness = user32.SetThreadDpiAwarenessContext(handle(-4))
    screen = user32.GetDC(None)
    memory = bitmap = None
    try:
        width, height = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        memory = gdi32.CreateCompatibleDC(screen)
        bitmap = gdi32.CreateCompatibleBitmap(screen, width, height)
        if not screen or not memory or not bitmap:
            raise OSError("The screen couldn't be read.")
        previous = gdi32.SelectObject(memory, bitmap)
        copied = gdi32.BitBlt(memory, 0, 0, width, height, screen, 0, 0, 0x00CC0020)  # SRCCOPY
        gdi32.SelectObject(memory, previous)
        # A negative height asks for rows from the top down.
        header = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), width, -height, 1, 32, 0)
        pixels = ctypes.create_string_buffer(width * height * 4)
        if not copied or gdi32.GetDIBits(memory, bitmap, 0, height, pixels, ctypes.byref(header), 0) != height:
            raise OSError("The screen couldn't be read.")
    finally:
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if memory:
            gdi32.DeleteDC(memory)
        if screen:
            user32.ReleaseDC(None, screen)
        if awareness:
            user32.SetThreadDpiAwarenessContext(awareness)
    return Image.frombuffer("RGB", (width, height), pixels, "raw", "BGRX", 0, 1)


class GameWatcher:
    """run() watches until cancelled, calling on_frame(frame) for each answered frame and
    on_error(error) for each failed one."""

    def __init__(self, on_frame: Callable[[Frame], None], on_error: Callable[[Exception], None], *,
                 in_front: Callable[[], bool], grab=grab_screen, client=None, interval: float = INTERVAL_SECONDS):
        self.on_frame = on_frame
        self.on_error = on_error
        self.in_front = in_front
        self.grab = grab
        self.client = client
        self.interval = interval
        self.totals = WatchTotals()
        self.previous: np.ndarray | None = None
        self.last_sent = 0.0

    async def run(self) -> None:
        while True:
            started = monotonic()
            await self.look()
            await asyncio.sleep(max(0.0, self.interval - (monotonic() - started)))

    async def look(self) -> None:
        """One turn: a frame if the game's in front and it changed (or it's been a while)."""
        if not await asyncio.to_thread(self.in_front):
            return
        jpeg, tiny = await asyncio.to_thread(lambda: frame_image(self.grab()))
        if not changed(self.previous, tiny) and monotonic() - self.last_sent < MAX_UNCHANGED_SECONDS:
            self.totals.skipped += 1
            return
        self.previous = tiny
        self.last_sent = monotonic()
        if self.client is None:
            from openai import OpenAI

            self.client = OpenAI(max_retries=0)
        try:
            frame = await asyncio.to_thread(ask, self.client, jpeg)
        except Exception as error:
            self.totals.failed += 1
            self.on_error(error)
            return
        self.totals.sent += 1
        self.totals.input_tokens += frame.input_tokens
        self.totals.seconds += frame.seconds
        self.on_frame(frame)
