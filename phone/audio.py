"""A phone call's audio, to and from Live's.

Twilio's media stream carries 8 kHz G.711 μ-law (one byte per sample, base64 in JSON); Live hears
and speaks 24 kHz mono PCM16. The rates are exactly 3 apart, so a short low-pass filter does the
resampling both ways with well under a millisecond of delay (a general resampler buffers in
blocks, which a conversation would hear). Python 3.13 has no audioop, so μ-law is done with
numpy lookup tables.

Each call has its own CallAudio: the filters keep a little of the previous chunk, so chunks join
without clicks.
"""

import base64
from time import monotonic

import numpy as np

PHONE_RATE = 8000
LIVE_RATE = 24000
FACTOR = LIVE_RATE // PHONE_RATE
# Below the phone band's top (about 3.4 kHz) and the 4 kHz half of the phone rate.
CUTOFF_HZ = 3600
TAPS = 63

_BIAS = 0x84
_CLIP = 32635


def _decode_table() -> np.ndarray:
    u = ~np.arange(256, dtype=np.uint8)
    exponent = (u >> 4) & 0x07
    mantissa = (u & 0x0F).astype(np.int32)
    magnitude = (((mantissa << 3) + _BIAS) << exponent) - _BIAS
    return np.where(u & 0x80, -magnitude, magnitude).astype(np.int16)


def _encode_table() -> np.ndarray:
    samples = np.arange(-32768, 32768, dtype=np.int32)
    sign = (samples < 0).astype(np.int32) << 7
    magnitude = np.minimum(np.abs(samples), _CLIP) + _BIAS
    exponent = np.floor(np.log2(magnitude)).astype(np.int32) - 7
    mantissa = (magnitude >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa) & 0xFF).astype(np.uint8)


DECODE = _decode_table()
# Indexed by the sample + 32768.
ENCODE = _encode_table()


def mulaw_to_pcm(data: bytes) -> np.ndarray:
    return DECODE[np.frombuffer(data, dtype=np.uint8)]


def pcm_to_mulaw(samples: np.ndarray) -> bytes:
    return ENCODE[samples.astype(np.int32) + 32768].tobytes()


def low_pass(rate: int = LIVE_RATE, cutoff: float = CUTOFF_HZ, taps: int = TAPS) -> np.ndarray:
    """A windowed-sinc low-pass filter that passes a steady signal unchanged."""
    middle = (taps - 1) / 2
    n = np.arange(taps) - middle
    h = np.sinc(2 * cutoff / rate * n) * np.hamming(taps)
    return h / h.sum()


class Filter:
    """A low-pass filter run over a stream in pieces, keeping what it needs of the last piece."""

    def __init__(self, h: np.ndarray) -> None:
        self.h = h
        self.tail = np.zeros(len(h) - 1)

    def __call__(self, x: np.ndarray) -> np.ndarray:
        joined = np.concatenate((self.tail, x))
        self.tail = joined[len(joined) - len(self.tail):]
        return np.convolve(joined, self.h, mode="valid")


def to_int16(x: np.ndarray) -> np.ndarray:
    return np.clip(np.rint(x), -32768, 32767).astype(np.int16)


class CallAudio:
    """One call's conversion: what the caller says for Live, and what she says for the caller."""

    def __init__(self) -> None:
        self.up = Filter(low_pass())
        self.down = Filter(low_pass())
        # Which of the filtered 24 kHz samples is the next one kept for 8 kHz.
        self.phase = 0

    def for_live(self, payload: str) -> bytes:
        """A media message's base64 μ-law, as 24 kHz PCM16 bytes."""
        samples = mulaw_to_pcm(base64.b64decode(payload)).astype(np.float64)
        stuffed = np.zeros(len(samples) * FACTOR)
        stuffed[::FACTOR] = samples
        return to_int16(self.up(stuffed) * FACTOR).tobytes()

    def for_caller(self, pcm: bytes) -> str:
        """Her 24 kHz PCM16 bytes, as base64 μ-law for a media message ("" when too short to give any)."""
        samples = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype="<i2").astype(np.float64)
        filtered = self.down(samples)
        kept = filtered[self.phase::FACTOR]
        self.phase = (self.phase - len(filtered)) % FACTOR
        return base64.b64encode(pcm_to_mulaw(to_int16(kept))).decode("ascii") if len(kept) else ""


# A caller's 20 ms frame this loud (RMS of the 24 kHz PCM16) is them talking; a phone line's
# background, after the handset's own noise suppression, is well under it.
LOUD_RMS = 700
# Her audio this long after her last counts as a new reply starting.
REPLY_PAUSE_SECONDS = 0.5
# Gaps shorter than this are her talking over the caller (or the line), not a reply.
MIN_GAP_SECONDS = 0.1


def loudness(pcm: bytes) -> float:
    samples = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype="<i2").astype(np.float64)
    return float(np.sqrt(np.mean(samples * samples))) if len(samples) else 0.0


class ReplyGaps:
    """How long she takes to reply on a call: from the last moment the caller was loud on the line
    to her first audio of the reply going back down it. That's her side (Live and this PC); the
    line itself adds a little each way on top."""

    def __init__(self, clock=monotonic) -> None:
        self.clock = clock
        self.last_loud: float | None = None
        self.her_last: float | None = None
        self.waiting = False
        self.gaps: list[float] = []

    def caller(self, pcm: bytes) -> None:
        if loudness(pcm) >= LOUD_RMS:
            self.last_loud = self.clock()
            self.waiting = True

    def her(self) -> None:
        """One of her audible chunks is going down the line."""
        now = self.clock()
        starting = self.her_last is None or now - self.her_last > REPLY_PAUSE_SECONDS
        self.her_last = now
        if starting and self.waiting and self.last_loud is not None:
            gap = now - self.last_loud
            if gap >= MIN_GAP_SECONDS:
                self.gaps.append(gap)
                self.waiting = False

    def summary(self) -> str:
        """One line for Activity, or "" with no replies measured."""
        if not self.gaps:
            return ""
        gaps = sorted(self.gaps)
        middle = gaps[len(gaps) // 2]
        return (f"Her replies began {middle:.1f} s after you stopped talking, typically "
                f"({len(gaps)} replies, {gaps[0]:.1f}–{gaps[-1]:.1f} s; the line adds a little each way).")
