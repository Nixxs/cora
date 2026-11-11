import base64
from math import sqrt
from queue import Empty, Full, Queue
from struct import iter_unpack
from time import monotonic

import sounddevice as sd

from devices.audio_bands import audio_bands


# Live's output is one continuous stream, silence included, on its own clock. Whatever playback
# loses to it (a stall, a late start, a clock a little faster than the speaker's) would otherwise
# stay queued for good, and her voice would come later and later after her words. So silence that
# arrives while this much audio is still waiting is skipped, which catches playback up between
# her sentences without touching her speech.
MAX_WAITING_SECONDS = 0.3
BYTES_PER_SECOND = 24000 * 2


class Speaker:
    """Play Live's mono 24 kHz PCM16 audio without blocking its event loop."""

    def __init__(self):
        self._bytes_queued = 0
        self._bytes_played = 0
        # How much silence has been skipped to keep up, for diagnosis.
        self.skipped_seconds = 0.0
        # Counted since the last timing() call, to find where a stutter comes from.
        self._timing = self.fresh_timing()
        self._last_arrival: float | None = None
        self._last_callback: float | None = None
        self._last_block_audible = False
        self.stream = None
        self.chunks = Queue(maxsize=250)
        self.pending = bytearray()
        self._playback_level = (0.0, 0.0)
        self._playback_bands = ((), 0.0)
        self._audible_samples_queued = 0
        self._audible_samples_played = 0

    @staticmethod
    def fresh_timing() -> dict:
        return {"arrivals": 0, "max_arrival_gap_ms": 0.0, "callbacks": 0, "max_callback_gap_ms": 0.0,
                "device_underflows": 0, "dry_mid_speech": 0, "dry_ms": 0.0}

    def timing(self) -> dict:
        """What playback has seen since the last call: how late audio arrived (max_arrival_gap_ms),
        how late the device asked for it (max_callback_gap_ms, device_underflows), and how often
        her speech ran out mid-word because the next of it hadn't arrived (dry_mid_speech, dry_ms)."""
        timing, self._timing = self._timing, self.fresh_timing()
        timing["waiting_ms"] = round(self.waiting_seconds() * 1000)
        timing["skipped_s"] = round(self.skipped_seconds, 1)
        return {name: round(value, 1) if isinstance(value, float) else value for name, value in timing.items()}

    def play(self, audio: str) -> bool:
        """Queue a base64 chunk; return True when playback first starts."""
        chunk = base64.b64decode(audio, validate=True)
        if not chunk:
            return False
        if len(chunk) % 2:
            raise ValueError("Speaker received an incomplete PCM16 sample.")
        audible_samples = sum(sample != 0 for (sample,) in iter_unpack("<h", chunk))
        now = monotonic()
        if self._last_arrival is not None:
            self._timing["max_arrival_gap_ms"] = max(self._timing["max_arrival_gap_ms"], (now - self._last_arrival) * 1000)
        self._last_arrival = now
        self._timing["arrivals"] += 1
        if not audible_samples and self.stream is not None and self.waiting_seconds() > MAX_WAITING_SECONDS:
            self.skipped_seconds += len(chunk) / BYTES_PER_SECOND
            return False
        # Publish before the queue item so the audio callback cannot get ahead.
        self._audible_samples_queued += audible_samples
        self._bytes_queued += len(chunk)
        try:
            self.chunks.put_nowait(chunk)
        except Full as error:
            self._audible_samples_queued -= audible_samples
            self._bytes_queued -= len(chunk)
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
        now = monotonic()
        timing = self._timing
        timing["callbacks"] += 1
        if self._last_callback is not None:
            timing["max_callback_gap_ms"] = max(timing["max_callback_gap_ms"], (now - self._last_callback) * 1000)
        self._last_callback = now
        if status and status.output_underflow:
            timing["device_underflows"] += 1
        if count < needed and self._last_block_audible:
            # Her speech was playing and the next of it isn't here.
            timing["dry_mid_speech"] += 1
            timing["dry_ms"] += (needed - count) / BYTES_PER_SECOND * 1000
        audio = bytes(self.pending[:count]) + bytes(needed - count)
        outdata[:] = audio
        del self.pending[:count]
        self._bytes_played += count
        self._audible_samples_played += sum(sample != 0 for (sample,) in iter_unpack("<h", audio))
        # Publish one immutable snapshot; never call UI code on the audio thread.
        level = (
            sqrt(sum(sample * sample for (sample,) in iter_unpack("<h", audio)) / frames) / 32768
            if frames else 0.0
        )
        self._last_block_audible = level > 0
        self._playback_level = (level, monotonic())
        self._playback_bands = (audio_bands(audio), monotonic())

    def waiting_seconds(self) -> float:
        """How much audio has been received but not yet played."""
        return (self._bytes_queued - self._bytes_played) / BYTES_PER_SECOND

    def get_playback_level(self) -> float:
        """Read the latest normalized RMS level, ignoring stopped/stale callbacks."""
        level, sampled_at = self._playback_level
        if self.stream is None or not self.stream.active or monotonic() - sampled_at > 0.15:
            return 0.0
        return level

    def get_playback_bands(self) -> tuple[float, ...]:
        """Read the latest loudness bands, ignoring stopped/stale callbacks."""
        bands, sampled_at = self._playback_bands
        if self.stream is None or not self.stream.active or monotonic() - sampled_at > 0.15:
            return ()
        return bands

    def is_playback_busy(self) -> bool:
        """Non-silent samples queued or playing; continuous zero PCM is idle."""
        return (
            self._audible_samples_queued > self._audible_samples_played
            or self.get_playback_level() > 0
        )

    def close(self) -> None:
        # Abort queued speech on shutdown rather than speaking after exit.
        stream = self.stream
        self.stream = None
        self._playback_level = (0.0, 0.0)
        self._playback_bands = ((), 0.0)
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
        # Reset only after abort/close has stopped the callback thread.
        self._audible_samples_queued = 0
        self._audible_samples_played = 0
        self._bytes_queued = 0
        self._bytes_played = 0