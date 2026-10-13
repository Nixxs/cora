import base64
from math import sqrt
from queue import Empty, Full, Queue
from struct import iter_unpack
from time import monotonic

import sounddevice as sd


class Speaker:
    """Play Live's mono 24 kHz PCM16 audio without blocking its event loop."""

    def __init__(self):
        self.stream = None
        self.chunks = Queue(maxsize=250)
        self.pending = bytearray()
        self._playback_level = (0.0, 0.0)

    def play(self, audio: str) -> bool:
        """Queue a base64 chunk; return True when playback first starts."""
        chunk = base64.b64decode(audio, validate=True)
        if not chunk:
            return False
        if len(chunk) % 2:
            raise ValueError("Speaker received an incomplete PCM16 sample.")
        try:
            self.chunks.put_nowait(chunk)
        except Full as error:
            raise RuntimeError("Speaker audio is backing up.") from error

        if self.stream is not None:
            if not self.stream.active:
                raise RuntimeError("Speaker stream stopped unexpectedly.")
            return False

        self.stream = sd.RawOutputStream(
            samplerate=24000,
            blocksize=480,
            channels=1,
            dtype="int16",
            callback=self.callback,
        )
        self.stream.start()
        return True

    def callback(self, outdata, frames, time_info, status):
        # Only this audio-device thread consumes the queue and pending bytes.
        needed = frames * 2
        while len(self.pending) < needed:
            try:
                self.pending.extend(self.chunks.get_nowait())
            except Empty:
                break

        count = min(needed, len(self.pending))
        audio = bytes(self.pending[:count]) + bytes(needed - count)
        outdata[:] = audio
        del self.pending[:count]
        # Publish one immutable snapshot; never call UI code on the audio thread.
        level = (
            sqrt(sum(sample * sample for (sample,) in iter_unpack("<h", audio)) / frames) / 32768
            if frames else 0.0
        )
        self._playback_level = (level, monotonic())

    def get_playback_level(self) -> float:
        """Read the latest normalized RMS level, ignoring stopped/stale callbacks."""
        level, sampled_at = self._playback_level
        if self.stream is None or not self.stream.active or monotonic() - sampled_at > 0.15:
            return 0.0
        return level

    def close(self) -> None:
        # Abort queued speech on shutdown rather than speaking after exit.
        stream = self.stream
        self.stream = None
        self._playback_level = (0.0, 0.0)
        if stream is not None:
            try:
                stream.abort()
            finally:
                stream.close()
        self.pending.clear()
        while True:
            try:
                self.chunks.get_nowait()
            except Empty:
                break