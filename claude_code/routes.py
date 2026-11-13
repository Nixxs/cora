"""The route Claude Code's hooks post to, on a listener of its own (server/listener.py).

That listener is on 127.0.0.1 at its own port, never the public one your proxy forwards, and every
event must carry the hook token (hooks.py), so only Cora's own hooks are heard.
"""

from collections.abc import Callable
import json

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from mail.webhook import matches

EVENT_PATH = "/claude-code/event"
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


def claude_code_routes(get_token: Callable[[], str | None], on_event: Callable[[dict], None]) -> list[Route]:
    """get_token() -> the hook token as it is now; on_event(event) runs for each one heard."""

    async def event(request: Request) -> Response:
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
        on_event(heard)
        # Empty, so Claude Code takes it as "carry on".
        return Response(status_code=204)

    return [Route(EVENT_PATH, event, methods=["POST"])]
