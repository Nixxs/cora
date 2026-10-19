"""One-off check: does the Live session's Responses backend accept an image?

Run from the repository root: python examples/image_input_check/main.py
Opens a Live session without microphone or speaker, sends one typed message with
Screenshot.png from this folder (a screenshot of code), prints the backend's
answer or error, then closes. Costs one backend request.
"""

import asyncio
import base64
from pathlib import Path
import sys

from dotenv import load_dotenv
from openai import AsyncOpenAI

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from session_config import build_session_config  # noqa: E402

load_dotenv(ROOT / ".env")

IMAGE = Path(__file__).resolve().parent / "Screenshot.png"
QUESTION = (
    "What does this screenshot show? Name the function and quote its first line "
    "of code exactly."
)


async def check() -> None:
    image_url = "data:image/png;base64," + base64.b64encode(IMAGE.read_bytes()).decode("ascii")
    answer = []
    async with AsyncOpenAI() as client:
        async with client.live.connect() as connection:
            await connection.session.start(session=build_session_config([]))
            async for event in connection:
                if event.type == "session.started":
                    print("Session started; sending image...")
                    await connection.response.item.create(item={
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": QUESTION},
                            {"type": "input_image", "image_url": image_url, "detail": "high"},
                        ],
                    })
                    await connection.response.create(event_id="image_check")
                elif event.type == "error":
                    print(f"Error event: {event.error}")
                    break
                elif event.type == "response.event":
                    nested = event.event
                    kind = nested.get("type")
                    if kind == "response.output_text.delta":
                        answer.append(nested.get("delta", ""))
                    elif kind in ("response.completed", "response.failed", "response.incomplete"):
                        print(f"Backend {kind}")
                        error = (nested.get("response") or {}).get("error")
                        if error:
                            print(f"Backend error: {error}")
                        break
            print(f"Answer: {''.join(answer) or '(none)'}")
            await connection.session.close()


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(check(), timeout=90))
