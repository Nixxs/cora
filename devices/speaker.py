import base64
from queue import Empty, Full, Queue

import sounddevice as sd


class Speaker:
    """Play Live's mono 24 kHz PCM16 audio without blocking its event loop."""

    def __init__(self):
        self.stream = None
        self.chunks = Queue(maxsize=250)
        self.pending = bytearray()

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
        outdata[:] = bytes(self.pending[:count]) + bytes(needed - count)
        del self.pending[:count]

    def close(self) -> None:
        # Abort queued speech on shutdown rather than speaking after exit.
        stream = self.stream
        self.stream = None
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