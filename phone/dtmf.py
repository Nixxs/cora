"""Phone keypad tones (DTMF), for pressing keys on a call she placed ("press 3 for new purchases").

A key is two tones played together, one from its row and one from its column. They go down the
line as audio, as a phone's own keypad sends them (in-band), so any menu that hears the caller
hears them. Made at Live's rate (24 kHz PCM16) to go through the call's usual audio path; every
key's frequencies are well inside the phone band that path keeps.
"""

import numpy as np

RATE = 24000
ROWS = (697, 770, 852, 941)
COLUMNS = (1209, 1336, 1477, 1633)
KEYPAD = ("123A", "456B", "789C", "*0#D")
FREQUENCIES = {key: (ROWS[row], COLUMNS[column])
               for row, keys in enumerate(KEYPAD) for column, key in enumerate(keys)}
# Each key sounds this long, with this much quiet after it: comfortably over what menus need
# (40 ms each), and like a person pressing them steadily.
TONE_SECONDS = 0.15
GAP_SECONDS = 0.1
# A short quiet first, so the first key isn't clipped by whatever was playing.
LEAD_SECONDS = 0.1
# Each of the pair at this fraction of full scale: loud and clear, never clipping together.
LEVEL = 0.35


def tones(keys: str) -> bytes:
    """The keys' tones, in order, as 24 kHz mono PCM16 (KeyError for a key not on a keypad)."""
    t = np.arange(int(TONE_SECONDS * RATE)) / RATE
    # A few milliseconds' fade at each end, so a tone doesn't start or stop with a click.
    fade = np.minimum(1.0, np.minimum(t, TONE_SECONDS - t) / 0.005)
    parts = [np.zeros(int(LEAD_SECONDS * RATE))]
    for key in keys.upper():
        low, high = FREQUENCIES[key]
        parts.append(LEVEL * fade * (np.sin(2 * np.pi * low * t) + np.sin(2 * np.pi * high * t)))
        parts.append(np.zeros(int(GAP_SECONDS * RATE)))
    return (np.concatenate(parts) * 32767).astype("<i2").tobytes()
