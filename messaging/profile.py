"""Set Cora's Signal profile: her name, and her work picture as what people see when she messages.

    python -m messaging.profile           # name "Cora" and her saved 'work' image (Signal must be running)
"""

import asyncio
import io
import subprocess
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from PIL import Image

from storage.database import build_engine
from storage.self_image_store import SelfImageStore
from storage.signal_settings_store import SignalSettingsStore
from .container import ENV_FILE, ROOT, signal_port
from .signal_cli import SignalClient

NAME = "Cora"
# Inside the container's own volume, where signal-cli can read it.
AVATAR_IN_CONTAINER = "/var/lib/signal-cli/cora-avatar.jpg"
AVATAR_SIZE = 640


def avatar_jpeg(image: bytes) -> bytes:
    """A square avatar-sized JPEG, as Signal shows profile pictures small."""
    picture = Image.open(io.BytesIO(image)).convert("RGB")
    side = min(picture.size)
    left, top = (picture.width - side) // 2, (picture.height - side) // 2
    picture = picture.crop((left, top, left + side, top + side)).resize((AVATAR_SIZE, AVATAR_SIZE))
    out = io.BytesIO()
    picture.save(out, "JPEG", quality=88)
    return out.getvalue()


def copy_into_container(data: bytes) -> None:
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "cora-avatar.jpg"
        path.write_bytes(data)
        result = subprocess.run(
            ["docker", "compose", "--env-file", str(ENV_FILE), "--profile", "signal", "cp", str(path),
             f"signal:{AVATAR_IN_CONTAINER}"],
            cwd=ROOT, capture_output=True, text=True, timeout=60,
        )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "docker compose cp failed")


async def set_profile() -> str:
    engine = build_engine()
    try:
        account = SignalSettingsStore(engine).settings().account
        work = SelfImageStore(engine).get("work")
    finally:
        engine.dispose()
    if not account:
        raise RuntimeError("Cora's Signal account isn't linked")
    avatar = None
    if work is not None and work.image:
        copy_into_container(avatar_jpeg(work.image))
        avatar = AVATAR_IN_CONTAINER
    await SignalClient(f"http://127.0.0.1:{signal_port()}").update_profile(account, NAME, avatar)
    return f"Signal profile set: name {NAME}" + (", picture from her work image" if avatar else ", no picture saved")


def main() -> None:
    load_dotenv()
    try:
        print(asyncio.run(set_profile()))
    except Exception as error:
        sys.exit(f"Profile not set: {error}")


if __name__ == "__main__":
    main()
