"""The routes Claude Code's hooks post to, on a listener of its own (server/listener.py).

That listener is on 127.0.0.1 at its own port, never the public one your proxy forwards, and every
event must carry the hook token (hooks.py), so only Cora's own hooks are heard.

Events are only heard. A permission request waits for Cora's answer: a PermissionRequest decision
(allow or deny), or nothing, so Claude Code asks you itself as usual.
"""

from collections.abc import Awaitable, Callable
import json

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from mail.webhook import matches

EVENT_PATH = "/claude-code/event"
PERMISSION_PATH = "/claude-code/permission"
DEFAULT_PORT = 8001
# Stop events carry Claude's whole last message; anything bigger isn't one of these.
BODY_MAX_BYTES = 512 * 1024


async def read_body(request: Request) -> bytes | None:
    """The body, or None when it's too large."""
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > BODY_MAX_BYTES:
            return None
    return body


def permission_decision(allow: bool, message: str = "") -> dict:
    """What a PermissionRequest hook prints to allow or deny the request."""
    decision = {"behavior": "allow"} if allow else {"behavior": "deny", "message": message}
    return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}}


async def no_decision(event: dict) -> dict | None:
    return None


def claude_code_routes(get_token: Callable[[], str | None], on_event: Callable[[dict], None],
                       on_permission: Callable[[dict], Awaitable[dict | None]] = no_decision) -> list[Route]:
    """get_token() -> the hook token as it is now; on_event(event) runs for each one heard, and
    on_permission(event) -> a permission_decision, or None to leave it to Claude Code's own prompt."""

    async def heard_from(request: Request) -> dict | Response:
        """The event posted, or the response refusing it."""
        token = get_token()
        if not token or not matches(request.headers.get("authorization"), f"Bearer {token}"):
            return Response(status_code=401)
        body = await read_body(request)
        if body is None:
            return Response(status_code=413)
        try:
            heard = json.loads(body)
        except ValueError:
            return Response(status_code=400)
        if not isinstance(heard, dict):
            return Response(status_code=400)
        return heard

    async def event(request: Request) -> Response:
        heard = await heard_from(request)
        if isinstance(heard, Response):
            return heard
        on_event(heard)
        # Empty, so Claude Code takes it as "carry on".
        return Response(status_code=204)

    async def permission(request: Request) -> Response:
        heard = await heard_from(request)
        if isinstance(heard, Response):
            return heard
        decision = await on_permission(heard)
        # Empty: no decision, so Claude Code asks you itself.
        return Response(status_code=204) if decision is None else JSONResponse(decision)

    return [Route(EVENT_PATH, event, methods=["POST"]), Route(PERMISSION_PATH, permission, methods=["POST"])]
