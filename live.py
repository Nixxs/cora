import asyncio
import base64

from microphone import microphone_chunks
from openai import AsyncOpenAI


class CoraLive:
    def __init__(self):
        self.connection = None
        self.closed = asyncio.Event()
        self.microphone_task = None

    async def run(self, on_event, on_mic_status):
        async with AsyncOpenAI() as client:
            async with client.live.connect() as connection:
                self.connection = connection

                try:
                    await connection.session.start(
                        session={
                            "model": "gpt-live-1",
                            "instructions": (
                                "You are Cora, speaking with Nick. "
                                "Be natural, conversational and concise."
                            ),
                            "audio": {
                                "format": {
                                    "type": "audio/pcm",
                                    "rate": 24000,
                                },
                                "output": {
                                    "voice": "marin",
                                },
                            },
                        }
                    )

                    self.microphone_task = asyncio.create_task(
                        self.send_microphone(connection, on_mic_status)
                    )

                    try:
                        async for event in connection:
                            on_event(event)

                            if event.type == "session.closed":
                                break
                        else:
                            raise RuntimeError(
                                "WebSocket closed without a session.closed event"
                            )

                    finally:
                        if self.microphone_task is not None:
                            self.microphone_task.cancel()

                            try:
                                await self.microphone_task
                            except asyncio.CancelledError:
                                pass

                            self.microphone_task = None

                finally:
                    self.connection = None
                    self.closed.set()

    async def close(self):
        if self.connection is None:
            return

        await self.connection.session.close()

        try:
            await asyncio.wait_for(self.closed.wait(), timeout=5)
        except TimeoutError:
            if self.connection is not None:
                await self.connection.close()

    async def send_microphone(self, connection, on_mic_status):
        mic_working = False

        try:
            async for chunk in microphone_chunks():
                audio = base64.b64encode(chunk).decode("ascii")

                await connection.session.input_audio.append(
                    audio=audio
                )

                if not mic_working:
                    mic_working = True
                    on_mic_status(True)

        except Exception:
            on_mic_status(False)
            raise

        finally:
            on_mic_status(False)