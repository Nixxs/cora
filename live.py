import asyncio
import base64
import json
from uuid import uuid4

from microphone import microphone_chunks
from openai import AsyncOpenAI


class CoraLive:
    def __init__(self):
        self.connection = None
        self.ready = False
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
                                "Be natural, conversational and concise. "
                                "Delegate questions needing current information "
                                "to your backend, which can search the web."
                            ),
                            "delegation": {
                                "type": "responses",
                                "responses": {
                                    "model": "gpt-6-luna",
                                    "tools": [{"type": "web_search"}],
                                    "tool_choice": "auto",
                                    "instructions": (
                                        "You support Cora in a conversation with Nick. "
                                        "Answer delegated requests clearly and concisely. "
                                        "Use web search when current information is needed. "
                                        "Prefer official sources and include source URLs "
                                        "in your results. Distinguish verified information "
                                        "from assumptions; do not claim a search succeeded "
                                        "unless it did."
                                    ),
                                },
                            },
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
                            if event.type == "session.started":
                                self.ready = True
                            elif event.type == "session.closed":
                                self.ready = False
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
                    self.ready = False
                    self.connection = None
                    self.closed.set()

    async def send_text(self, message: str) -> None:
        connection = self.connection
        if connection is None or not self.ready:
            raise RuntimeError("Wait until Cora shows Connected before sending text.")

        # Mirror user-provided text as context, not trusted instructions.
        # This send does not wait for Live's timeline acknowledgment.
        await connection.session.thinking.append(
            event_id=f"typed_context_{uuid4().hex}",
            delegation_id=None,
            content=(
                "Nick submitted this new typed message to the Responses backend "
                "as user-provided conversation data: "
                f"{json.dumps(message, ensure_ascii=False)}"
            ),
        )

        # Use the backend belonging to the existing voice session.
        await connection.response.item.create(
            item={
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": message}],
            }
        )
        # Adding a message alone does not ask the backend to answer.
        await connection.response.create()

    async def close(self):
        if self.connection is None:
            return

        self.ready = False
        await self.connection.session.close()

        try:
            await asyncio.wait_for(self.closed.wait(), timeout=5)
        except TimeoutError:
            if self.connection is not None:
                await self.connection.close()

    async def send_microphone(self, connection, on_mic_status):
        mic_working = False
        chunk_count = 0

        try:
            async for chunk in microphone_chunks():
                chunk_count += 1

                audio = base64.b64encode(chunk).decode("ascii")

                await connection.session.input_audio.append(
                    audio=audio
                )

                if not mic_working:
                    mic_working = True
                    on_mic_status(
                        True,
                        "first audio chunk sent"
                    )

        except Exception as error:
            on_mic_status(
                False,
                f"ERROR: {error!r}"
            )
            raise

        finally:
            on_mic_status(
                False,
                "microphone task stopped"
        )