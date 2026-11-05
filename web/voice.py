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


@dataclass(eq=False)
class Listener:
    """One page listening: its device's name and the audio waiting to go to it."""

    device: str
    audio: asyncio.Queue = field(default_factory=asyncio.Queue)
    queued_bytes: int = 0


class VoiceRelay:
    def __init__(self, clock=monotonic) -> None:
        self.clock = clock
        self.listeners: set[Listener] = set()
        # When what has been sent finishes playing, by the clock.
        self.playing_until = 0.0

    @property
    def active(self) -> bool:
        return bool(self.listeners)

    def listen(self, device: str) -> Listener:
        listener = Listener(device)
        self.listeners.add(listener)
        return listener

    def stop(self, listener: Listener) -> None:
        self.listeners.discard(listener)

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

    async def next_chunk(self, listener: Listener, timeout: float) -> bytes | None:
        """The next chunk for this listener; None if it was dropped. TimeoutError if none came."""
        chunk = await asyncio.wait_for(listener.audio.get(), timeout)
        if chunk is not None:
            listener.queued_bytes -= len(chunk)
        return chunk

    def is_playback_busy(self) -> bool:
        """Whether what's been sent is still playing on a page (as far as timing tells)."""
        return bool(self.listeners) and self.clock() < self.playing_until + PLAYBACK_MARGIN_SECONDS
