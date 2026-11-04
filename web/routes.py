"""The web interface's routes, for the listener's build_app (server/listener.py).

For now only the page's static files, served at the root. Anything that reaches Cora herself
(sending a message, the live conversation) will need a paired device.
"""

from pathlib import Path

from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

STATIC_DIR = Path(__file__).resolve().parent / "static"


def web_routes() -> list[Mount]:
    # At the root, so it would take any path: the listener puts it after the other groups.
    return [Mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="web")]
