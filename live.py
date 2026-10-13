import asyncio
import base64
import json
from uuid import uuid4

from microphone import microphone_chunks
from openai import AsyncOpenAI
from session_config import build_session_config
from speaker import Speaker


class CoraLive:
    def __init__(self, tool_schemas=None):
        self.tool_schemas = tool_schemas if tool_schemas is not None else lambda: []
        self.connection = None
        self.ready = False
        self.closed = asyncio.Event()
        self.microphone_task = None
        self.memory_context = ""

    async def run(self, on_event, on_mic_status, on_audio_status=None):
        async with AsyncOpenAI() as client:
            async with client.live.connect() as connection:
                self.connection = connection
                speaker = Speaker()
                playback_failed = False

                try:
                    await connection.session.start(
                        session=build_session_config(self.tool_schemas(), self.memory_context)
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
                            elif event.type == "session.output_audio.delta" and not playback_failed:
                                try:
                                    started = speaker.play(event.delta)
                                except Exception as error:
                                    # Keep text/voice input usable if the output device fails.
                                    playback_failed = True
                                    try:
                                        speaker.close()
                                    except Exception as close_error:
                                        if on_audio_status is not None:
                                            on_audio_status(False, f"Speaker cleanup error: {close_error}")
                                    if on_audio_status is not None:
                                        on_audio_status(False, f"Audio playback error: {error}")
                                else:
                                    if started and on_audio_status is not None:
                                        on_audio_status(True, "Speaker stream started")
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
                    try:
                        speaker.close()
                    finally:
                        self.connection = None
                        self.closed.set()

    async def send_text(self, message: str) -> None:
        connection = self.connection
        if connection is None or not self.ready:
            raise RuntimeError("Wait until Cora shows Connected before sending text.")

        # A silent context mirror, not a second conversational request.
        # This send does not wait for Live's timeline acknowledgment.
        await connection.session.thinking.append(
            event_id=f"typed_context_{uuid4().hex}",
            delegation_id=None,
            content=(
                "Silent typed-context mirror: Nick submitted this typed message to the Responses backend "
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