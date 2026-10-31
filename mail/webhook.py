"""Cora's webhook listener: Microsoft Graph tells her mail has landed, and she checks for herself.

It listens on 127.0.0.1 only; the person routes public HTTPS to it however they like (a reverse
proxy, a tunnel). Every route sits under a secret path, and each notification must carry the
secret clientState Cora gave Microsoft, so only her own subscriptions count. Nothing in a request
is trusted or used: a notification only says "look", and her mailbox is read through Graph itself.
The secrets live in the system credential store, never in the database or logs.
"""

from collections.abc import Callable
from contextlib import contextmanager
import hmac
import json
import logging
import secrets
import socket

import keyring
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route
import uvicorn

from .account import KEYRING_SERVICE

HOST = "127.0.0.1"
PATH_ACCOUNT = "webhook:path"
CLIENT_STATE_ACCOUNT = "webhook:client-state"
# Graph's handshake token is short; anything longer isn't from it.
VALIDATION_TOKEN_MAX_CHARS = 1024
BODY_MAX_BYTES = 64 * 1024
ITEMS_MAX = 100
LIFECYCLE_EVENTS = {"reauthorizationRequired", "subscriptionRemoved", "missed"}


def webhook_secrets(rotate: bool = False) -> tuple[str, str]:
    """(path secret, clientState): made on first use, or new ones when rotating. Blocking (credential store)."""
    found = [None if rotate else keyring.get_password(KEYRING_SERVICE, account)
             for account in (PATH_ACCOUNT, CLIENT_STATE_ACCOUNT)]
    for index, account in enumerate((PATH_ACCOUNT, CLIENT_STATE_ACCOUNT)):
        if not found[index]:
            found[index] = secrets.token_urlsafe(32)
            keyring.set_password(KEYRING_SERVICE, account, found[index])
    return found[0], found[1]


def mail_urls(public_url: str, path: str) -> tuple[str, str]:
    """The notification and lifecycle URLs given to Microsoft."""
    base = f"{public_url.rstrip('/')}/webhooks/{path}/mail"
    return base, f"{base}/lifecycle"


def matches(given, expected: str) -> bool:
    return isinstance(given, str) and hmac.compare_digest(given.encode(), expected.encode())


async def read_items(request: Request) -> list[dict] | None:
    """The notification's items, or None when too large; a malformed body is no items."""
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > BODY_MAX_BYTES:
            return None
    try:
        items = json.loads(body).get("value")
    except (ValueError, AttributeError):
        return []
    return [item for item in items[:ITEMS_MAX] if isinstance(item, dict)] if isinstance(items, list) else []


def webhook_app(get_secrets: Callable[[], tuple[str, str]], on_mail: Callable[[], None],
                on_lifecycle: Callable[[str], None]) -> Starlette:
    """get_secrets() -> (path, clientState) as they are now, so new ones apply at once."""
    def handshake(request: Request) -> Response | None:
        # Graph proves the URL is ours before a subscription starts: echo its token, as plain text.
        token = request.query_params.get("validationToken")
        if token is None:
            return None
        if len(token) > VALIDATION_TOKEN_MAX_CHARS:
            return Response(status_code=400)
        return PlainTextResponse(token)

    async def notified(request: Request, lifecycle: bool) -> Response:
        path, client_state = get_secrets()
        if not matches(request.path_params["secret"], path):
            return Response(status_code=404)
        if (answer := handshake(request)) is not None:
            return answer
        items = await read_items(request)
        if items is None:
            return Response(status_code=413)
        ours = [item for item in items if matches(item.get("clientState"), client_state)]
        if lifecycle:
            for event in {item.get("lifecycleEvent") for item in ours} & LIFECYCLE_EVENTS:
                on_lifecycle(event)
        elif ours:
            on_mail()
        return Response(status_code=202)

    async def mail(request: Request) -> Response:
        return await notified(request, lifecycle=False)

    async def lifecycle(request: Request) -> Response:
        return await notified(request, lifecycle=True)

    async def health(request: Request) -> Response:
        if not matches(request.path_params["secret"], get_secrets()[0]):
            return Response(status_code=404)
        return PlainTextResponse("ok")

    return Starlette(routes=[
        Route("/webhooks/{secret}/mail", mail, methods=["POST"]),
        Route("/webhooks/{secret}/mail/lifecycle", lifecycle, methods=["POST"]),
        Route("/webhooks/{secret}/health", health, methods=["GET"]),
    ])


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


class WebhookServer:
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
