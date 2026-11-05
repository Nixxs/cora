"""Cora's voice on the web page: Live's speech, relayed to the devices listening.

Live sends her speech as one continuous stream of 24 kHz mono PCM16, silence included, and
handles interruptions itself, so a page just plays what arrives. Silent chunks aren't sent: they
cost bandwidth and carry nothing. Each listening page has its own queue; one that falls too far
behind is dropped (it can listen again), so a slow phone never holds up the desk.

The relay also keeps track of how long what it has sent takes to play, so the desk can tell
she's still speaking on a phone when its own speaker is quiet (standby waits for it).
"""

import asyncio
import base64
from dataclasses import dataclass, field
from struct import iter_unpack
from time import monotonic

SAMPLE_RATE = 24000
BYTES_PER_SECOND = SAMPLE_RATE * 2
# A listener this far behind is dropped rather than left to drift ever later.
BACKLOG_SECONDS = 8
# A little extra after the last audio, for the page's own buffering.
PLAYBACK_MARGIN_SECONDS = 0.3


def audible(chunk: bytes) -> bool:
    return any(sample for (sample,) in iter_unpack("<h", chunk[: len(chunk) - len(chunk) % 2]))


# What a talking page's microphone may get ahead of Live by before its audio is dropped.
MIC_BACKLOG_CHUNKS = 200


@dataclass(eq=False)
class Listener:
    """One page listening: its device's name and what waits to go to it (her audio as bytes, or
    a small message for the page as a dict). While it talks, mic holds its microphone's audio."""

    device: str
    audio: asyncio.Queue = field(default_factory=asyncio.Queue)
    queued_bytes: int = 0
    mic: asyncio.Queue | None = None


class VoiceRelay:
    def __init__(self, clock=monotonic) -> None:
        self.clock = clock
        self.listeners: set[Listener] = set()
        # When what has been sent finishes playing, by the clock.
        self.playing_until = 0.0
        # The one page talking to her, if any: Live hears its microphone instead of the desk's.
        self.talker: Listener | None = None

    @property
    def active(self) -> bool:
        return bool(self.listeners)

    def listen(self, device: str) -> Listener:
        listener = Listener(device)
        self.listeners.add(listener)
        return listener

    def stop(self, listener: Listener) -> None:
        self.listeners.discard(listener)

    def start_talking(self, listener: Listener) -> Listener | None:
        """This page talks now, with a fresh queue for its microphone; returns the page that was
        talking before (it's taken over), if any."""
        previous = self.talker if self.talker is not listener else None
        if previous is not None:
            previous.mic = None
        listener.mic = asyncio.Queue()
        self.talker = listener
        return previous

    def stop_talking(self, listener: Listener) -> bool:
        """Whether this page was the one talking (and now isn't)."""
        if self.talker is not listener:
            return False
        self.talker = None
        listener.mic = None
        return True

    def hear(self, listener: Listener, chunk: bytes) -> None:
        """A piece of a page's microphone audio: for Live, if that page is the one talking."""
        if self.talker is listener and listener.mic is not None and listener.mic.qsize() < MIC_BACKLOG_CHUNKS:
            listener.mic.put_nowait(chunk)

    def tell(self, listener: Listener, message: dict) -> None:
        """A small message for one page, in order with its audio."""
        listener.audio.put_nowait(message)

    def play(self, delta: str) -> None:
        """One of Live's base64 audio chunks, for every listener; silence is skipped."""
        if not self.listeners:
            return
        chunk = base64.b64decode(delta)
        if not chunk or not audible(chunk):
            return
        now = self.clock()
        self.playing_until = max(self.playing_until, now) + len(chunk) / BYTES_PER_SECOND
        for listener in list(self.listeners):
            if listener.queued_bytes > BACKLOG_SECONDS * BYTES_PER_SECOND:
                # Too far behind: dropped, and told so (None), rather than playing ever later.
                self.listeners.discard(listener)
                listener.audio.put_nowait(None)
                continue
            listener.queued_bytes += len(chunk)
            listener.audio.put_nowait(chunk)

    async def next_chunk(self, listener: Listener, timeout: float) -> bytes | dict | None:
        """The next audio chunk or message for this listener; None if it was dropped.
        TimeoutError if nothing came."""
        chunk = await asyncio.wait_for(listener.audio.get(), timeout)
        if isinstance(chunk, bytes):
            listener.queued_bytes -= len(chunk)
        return chunk

    def is_playback_busy(self) -> bool:
        """Whether what's been sent is still playing on a page (as far as timing tells)."""
        return bool(self.listeners) and self.clock() < self.playing_until + PLAYBACK_MARGIN_SECONDS
