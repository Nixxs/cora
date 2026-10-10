import asyncio

from openai import AsyncOpenAI


class CoraLive:
    def __init__(self):
        self.connection = None
        self.closed = asyncio.Event()

    async def run(self, on_event):
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

                    async for event in connection:
                        on_event(event)

                        if event.type == "session.closed":
                            break
                    else:
                        raise RuntimeError(
                            "WebSocket closed without a session.closed event"
                        )
                    
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