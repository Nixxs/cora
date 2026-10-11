import asyncio

import sounddevice as sd


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
        error = str(status) if status else None

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