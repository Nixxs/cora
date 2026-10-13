import asyncio
from collections import deque
from contextlib import aclosing, suppress
from time import monotonic

import numpy as np

from devices.microphone import microphone_chunks
from wake.detector import WakeDetector


class WakeListener:
    """One microphone owner, from local spotting through Live reconnection.

    Capture continues while Whisper decodes off-thread. A six-second ring holds
    the checked window and opening speech through inference. After a trigger,
    at most 15 seconds can queue. No audio is written to disk.
    """

    def __init__(self, on_wake, on_status, *, detector_factory=WakeDetector,
                 source=microphone_chunks, cooldown=0.0, on_debug=None,
                 check_chunks=50, heartbeat_seconds=5.0):
        self.on_wake = on_wake
        self.on_status = on_status
        self.on_debug = on_debug
        self.detector_factory = detector_factory
        self.source = source
        self.cooldown = cooldown
        self.task = None
        self.capture_task = None
        self.heartbeat_task = None
        self.changed = asyncio.Event()
        self.consumer_started = asyncio.Event()
        self.reconnect_timeout = 15.0
        self.check_chunks = check_chunks
        self.heartbeat_seconds = heartbeat_seconds
        self.chunk_count = 0
        self.check_count = 0
        self.last_audio_at = None
        self.peak_level = 0.0
        self.decoding_since = None
        self.trigger_deadline = None
        self.last_reported_text = ""
        self.triggered = False
        self.consuming = False
        self.stopped = False
        self.error = None
        self.audio = asyncio.Queue(maxsize=750)  # 20 ms chunks, 15 seconds
        self.pre_roll = deque(maxlen=300)  # 6 seconds, erased on stop

    def start(self):
        self.task = asyncio.create_task(self.run())

    async def run(self):
        try:
            if self.cooldown > 0:
                await asyncio.sleep(self.cooldown)
            detector = await asyncio.to_thread(self.detector_factory)
            self.capture_task = asyncio.create_task(self.capture())
            if self.on_debug is not None:
                self.heartbeat_task = asyncio.create_task(self.heartbeat())
            self.on_status(True, "Local Whisper wake listener ready (Hey Cora / Kora / Korra).")
            checked_count = 0
            while not self.stopped:
                await self.changed.wait()
                self.changed.clear()
                if self.capture_task.done():
                    self.capture_task.result()
                    raise RuntimeError("Wake microphone capture ended")
                if self.chunk_count - checked_count < self.check_chunks:
                    continue
                # Three-second overlapping windows, at most one check per second.
                snapshot = list(self.pre_roll)[-150:]
                checked_count = self.chunk_count
                self.decoding_since = monotonic()
                try:
                    matched = await asyncio.to_thread(detector.process, b"".join(snapshot))
                finally:
                    self.decoding_since = None
                self.check_count += 1
                if self.capture_task.done():
                    self.capture_task.result()
                    raise RuntimeError("Wake microphone capture ended")
                text = getattr(detector, "last_transcript", "")
                match = getattr(detector, "last_match", "")
                if isinstance(text, str) and text and (text != self.last_reported_text or matched):
                    if self.on_debug is not None:
                        self.on_debug(f"Whisper heard (approximate): {text[:240]!r}; wake match: {match!r}")
                    self.last_reported_text = text
                elif not text:
                    self.last_reported_text = ""
                if not matched:
                    continue
                needed = len(snapshot) + self.chunk_count - checked_count
                if needed > len(self.pre_roll):
                    if self.on_debug is not None:
                        self.on_debug("Whisper result too old; not waking with truncated opening speech. Try again.")
                    continue
                self.triggered = True
                self.trigger_deadline = monotonic() + self.reconnect_timeout
                for buffered in list(self.pre_roll)[-needed:]:
                    self.audio.put_nowait(buffered)
                self.pre_roll.clear()
                if not self.on_wake(self):
                    return
                # Also bound setup when the input device stops delivering chunks.
                await asyncio.wait_for(self.consumer_started.wait(), timeout=self.reconnect_timeout)
                await self.capture_task
                raise RuntimeError("Wake microphone capture ended")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.error = error
            self.on_status(False, f"Local wake listener stopped ({type(error).__name__}).")
        finally:
            self.stopped = True
            for task in (self.capture_task, self.heartbeat_task):
                if task is not None:
                    task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await task
            self.pre_roll.clear()
            self.clear_audio()
            # Release an awaiting Live consumer even when capture fails/stops.
            self.audio.put_nowait(None)

    async def capture(self):
        try:
            async with aclosing(self.source()) as chunks:
                while not self.stopped:
                    timeout = None
                    if self.triggered and not self.consuming:
                        timeout = max(0, self.trigger_deadline - monotonic())
                    chunk = await asyncio.wait_for(anext(chunks), timeout=timeout)
                    self.chunk_count += 1
                    self.last_audio_at = monotonic()
                    samples = np.frombuffer(chunk, dtype="<i2").astype(np.float32) / 32768.0
                    if samples.size:
                        self.peak_level = max(self.peak_level, float(np.sqrt(np.mean(samples * samples))))
                    if self.triggered:
                        self.audio.put_nowait(chunk)
                    else:
                        self.pre_roll.append(chunk)
                    self.changed.set()
        finally:
            self.changed.set()
            self.consumer_started.set()

    def report_health(self):
        if self.on_debug is None or self.stopped or self.triggered:
            return
        age = "no audio yet" if self.last_audio_at is None else f"last audio {monotonic() - self.last_audio_at:.1f}s ago"
        inference = "idle" if self.decoding_since is None else f"decoding {monotonic() - self.decoding_since:.1f}s"
        self.on_debug(
            f"Listening locally: chunks={self.chunk_count}, checks={self.check_count}, "
            f"audio level={self.peak_level:.4f}, {age}; Whisper {inference}."
        )
        self.peak_level = 0.0

    async def heartbeat(self):
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            self.report_health()

    async def chunks(self):
        self.consuming = True
        self.consumer_started.set()
        try:
            while True:
                chunk = await self.audio.get()
                if chunk is None:
                    if self.error is not None:
                        raise RuntimeError("Wake microphone capture failed") from self.error
                    return
                yield chunk
        finally:
            await self.stop()

    def clear_audio(self):
        while not self.audio.empty():
            self.audio.get_nowait()

    async def stop(self):
        self.stopped = True
        if self.task is not None and self.task is not asyncio.current_task():
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        self.pre_roll.clear()
        self.clear_audio()
        self.audio.put_nowait(None)