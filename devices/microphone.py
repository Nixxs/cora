import asyncio
from array import array
from math import sqrt

import sounddevice as sd


def chunk_level(chunk: bytes) -> float:
    """Normalized RMS of one PCM16 chunk, from 0.0 to 1.0."""
    samples = array("h", chunk)
    if not samples:
        return 0.0
    return sqrt(sum(sample * sample for sample in samples) / len(samples)) / 32768


async def microphone_chunks():
    loop = asyncio.get_running_loop()
    chunks = asyncio.Queue(maxsize=50)
    failed = False

    def enqueue(audio, error):
        nonlocal failed

        if failed:
            return

        if chunks.full():
            error = "Microphone audio is backing up."
            chunks.get_nowait()

        if error:
            failed = True
            chunks.put_nowait(RuntimeError(error))
        else:
            chunks.put_nowait(audio)

    def callback(indata, frames, time_info, status):
        audio = bytes(indata)
        # An overflow only means PortAudio dropped a little input during a stall; keep listening.
        error = str(status) if status and not status.input_overflow else None

        loop.call_soon_threadsafe(enqueue, audio, error)

    with sd.RawInputStream(
        samplerate=24000,
        blocksize=480,
        channels=1,
        dtype="int16",
        callback=callback,
    ):
        while True:
            chunk = await chunks.get()

            if isinstance(chunk, Exception):
                raise chunk

            yield chunk