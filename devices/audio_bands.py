"""Split PCM16 audio into loudness bands for the dashboard's voice bars."""

import numpy as np


BAND_COUNT = 24
# Speech sits roughly between 100 Hz and 8 kHz; bands are spaced evenly in pitch.
LOW_HZ, HIGH_HZ = 100, 8000
# Quieter than this shows as an empty bar, which hides ordinary room noise.
FLOOR_DB = -60


def audio_bands(chunk: bytes, sample_rate: int = 24000, count: int = BAND_COUNT) -> tuple[float, ...]:
    """Each band's loudness, from 0.0 at FLOOR_DB to 1.0 for a full-scale tone."""
    samples = np.frombuffer(chunk, dtype="<i2") / 32768
    # A window over 2 or fewer samples is all zeros, so there is nothing to measure.
    if len(samples) < 3:
        return (0.0,) * count
    window = np.hanning(len(samples))
    # Scaled so a full-scale sine peaks at 1.0.
    magnitudes = np.abs(np.fft.rfft(samples * window)) / (window.sum() / 2)
    edges = np.geomspace(LOW_HZ, HIGH_HZ, count + 1) / (sample_rate / len(samples))
    bands = []
    for low, high in zip(edges[:-1], edges[1:]):
        start = min(int(low), len(magnitudes) - 1)
        peak = magnitudes[start:max(int(high), start + 1)].max()
        decibels = 20 * np.log10(max(peak, 1e-12))
        bands.append(float(min(max(1 - decibels / FLOOR_DB, 0.0), 1.0)))
    return tuple(bands)
