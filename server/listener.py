"""Cora's HTTP listener: one Starlette app on 127.0.0.1, made of route groups.

It listens on 127.0.0.1 only; the person routes public HTTPS to it however they like (a reverse
proxy, a tunnel). Each group brings its own routes and checks: Microsoft Graph notifications
(mail/webhook.py) and the web interface (web/routes.py).
"""

from collections.abc import Iterable
from contextlib import contextmanager
import logging
import socket

from starlette.applications import Starlette
from starlette.routing import BaseRoute
import uvicorn

HOST = "127.0.0.1"


def build_app(*route_groups: Iterable[BaseRoute]) -> Starlette:
    """One app from the groups, matched in the order given: the first route that fits a request wins.

    Put the web interface last: it serves files at the root, so it would claim paths meant for
    groups after it.
    """
    return Starlette(routes=[route for group in route_groups for route in group])


class QuietServer(uvicorn.Server):
    """Textual owns the process's signals; uvicorn mustn't take Ctrl+C from it."""

    @contextmanager
    def capture_signals(self):
        yield


def silence_uvicorn() -> None:
    # Anything uvicorn printed would land on top of the TUI.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.propagate = False
        if not any(isinstance(handler, logging.NullHandler) for handler in logger.handlers):
            logger.addHandler(logging.NullHandler())


class Listener:
    """Serves the app on 127.0.0.1:port until stopped. open() raises OSError if the port is taken."""

    def __init__(self, app, port: int) -> None:
        self.app = app
        self.port = port
        self.socket: socket.socket | None = None
        self.server: QuietServer | None = None

    def open(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind((HOST, self.port))
        except OSError:
            listener.close()
            raise
        self.socket = listener

    async def serve(self) -> None:
        silence_uvicorn()
        self.server = QuietServer(uvicorn.Config(
            self.app, log_config=None, access_log=False, lifespan="off", log_level="warning",
        ))
        await self.server.serve(sockets=[self.socket])

    def stop(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
        elif self.socket is not None:
            self.socket.close()
