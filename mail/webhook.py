"""Microsoft Graph notifications: Graph tells Cora mail has landed or her calendar changed, and
she checks for herself.

These routes join her one listener (server/listener.py), which listens on 127.0.0.1 only. Every
route sits under a secret path, and each notification must carry the secret clientState Cora gave
Microsoft, so only her own subscriptions count. Nothing in a request is trusted or used: a
notification only says "look", and her mailbox is read through Graph itself. The secrets live in
the system credential store, never in the database or logs.
"""

from collections.abc import Callable
import hmac
import json
import secrets

import keyring
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from .account import KEYRING_SERVICE

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


def calendar_urls(public_url: str, path: str) -> tuple[str, str]:
    """The notification and lifecycle URLs given to Microsoft for her calendar."""
    base = f"{public_url.rstrip('/')}/webhooks/{path}/calendar"
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


def graph_routes(get_secrets: Callable[[], tuple[str, str]], on_mail: Callable[[], None],
                 on_lifecycle: Callable[[str], None], on_calendar: Callable[[], None] = lambda: None,
                 on_calendar_lifecycle: Callable[[str], None] = lambda event: None) -> list[Route]:
    """The notification routes, for the listener's build_app.

    get_secrets() -> (path, clientState) as they are now, so new ones apply at once.
    """
    def handshake(request: Request) -> Response | None:
        # Graph proves the URL is ours before a subscription starts: echo its token, as plain text.
        token = request.query_params.get("validationToken")
        if token is None:
            return None
        if len(token) > VALIDATION_TOKEN_MAX_CHARS:
            return Response(status_code=400)
        return PlainTextResponse(token)

    async def notified(request: Request, notify: Callable[[], None] | None,
                       lifecycle: Callable[[str], None] | None = None) -> Response:
        path, client_state = get_secrets()
        if not matches(request.path_params["secret"], path):
            return Response(status_code=404)
        if (answer := handshake(request)) is not None:
            return answer
        items = await read_items(request)
        if items is None:
            return Response(status_code=413)
        ours = [item for item in items if matches(item.get("clientState"), client_state)]
        if lifecycle is not None:
            for event in {item.get("lifecycleEvent") for item in ours} & LIFECYCLE_EVENTS:
                lifecycle(event)
        elif ours:
            notify()
        return Response(status_code=202)

    async def mail(request: Request) -> Response:
        return await notified(request, on_mail)

    async def lifecycle(request: Request) -> Response:
        return await notified(request, None, on_lifecycle)

    async def calendar(request: Request) -> Response:
        return await notified(request, on_calendar)

    async def calendar_lifecycle(request: Request) -> Response:
        return await notified(request, None, on_calendar_lifecycle)

    async def health(request: Request) -> Response:
        if not matches(request.path_params["secret"], get_secrets()[0]):
            return Response(status_code=404)
        return PlainTextResponse("ok")

    return [
        Route("/webhooks/{secret}/mail", mail, methods=["POST"]),
        Route("/webhooks/{secret}/mail/lifecycle", lifecycle, methods=["POST"]),
        Route("/webhooks/{secret}/calendar", calendar, methods=["POST"]),
        Route("/webhooks/{secret}/calendar/lifecycle", calendar_lifecycle, methods=["POST"]),
        Route("/webhooks/{secret}/health", health, methods=["GET"]),
    ]

