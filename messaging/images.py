"""Images sent to Cora on Signal, read the way pasted ones are: a separate one-off look describes
each, and only that description reaches her. The image itself is never kept."""

import asyncio
import io

from PIL import Image

from tools.look_at_screen import PASTE_QUESTION, SIGNAL_IMAGE_INSTRUCTIONS, look
from .receiver import Attachment, IncomingMessage

# Types Pillow opens and the model reads; anything else (HEIC, video, documents) stays unopened.
IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})
IMAGE_MAX_BYTES = 20_000_000
IMAGE_MAX_PIXELS = 40_000_000
# Each image is a model call: a message with an album is read up to this many.
IMAGES_PER_MESSAGE = 4
# As for pasted images: the backend scales high-detail images to fit 2048x2048.
IMAGE_MAX_SIDE = 2048
JPEG_QUALITY = 85


def readable_images(message: IncomingMessage) -> list[Attachment]:
    return [
        attachment for attachment in message.attachments
        if attachment.content_type in IMAGE_TYPES and attachment.id and 0 <= attachment.size <= IMAGE_MAX_BYTES
    ][:IMAGES_PER_MESSAGE]


def scaled_jpeg(data: bytes) -> bytes:
    if len(data) > IMAGE_MAX_BYTES:
        raise ValueError("Image too large")
    image = Image.open(io.BytesIO(data))
    if image.width * image.height > IMAGE_MAX_PIXELS:
        raise ValueError("Image has too many pixels")
    image = image.convert("RGB")
    image.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY)
    return buffer.getvalue()


def read_image(question: str, data: bytes) -> str:
    return look(question, scaled_jpeg(data), mime="image/jpeg", instructions=SIGNAL_IMAGE_INSTRUCTIONS)


async def read_images(message: IncomingMessage, fetch) -> tuple[tuple[str, ...], int]:
    """What was seen in each of the message's images, and how many couldn't be read.

    `fetch(attachment_id)` returns the bytes. Failures are counted, never raised: the message is
    still answered, with that image noted as not opened.
    """
    seen, failed = [], 0
    for attachment in readable_images(message):
        try:
            data = await fetch(attachment.id)
            seen.append(await asyncio.to_thread(read_image, message.text or PASTE_QUESTION, data))
        except Exception:
            failed += 1
    return tuple(seen), failed
