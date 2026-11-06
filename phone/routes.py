"""The listener's routes for phone calls (for server/listener.py's build_app): Twilio asks here what
to do with each call to Cora's number."""

from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import parse_qsl

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from .twilio import INCOMING_PATH, valid_signature

# Twilio's call requests are a few dozen short fields.
BODY_MAX_BYTES = 16 * 1024


@dataclass
class PhoneAccess:
    """What the routes need from the app."""

    # The Twilio auth token ("" if none), and the public address Twilio calls (https://host).
    token: Callable[[], str]
    public_url: Callable[[], str]
    # on_call(caller, call_id) -> the TwiML answer for a call that Twilio has vouched for.
    on_call: Callable[[str, str], str]


def twiml(body: str, status: int = 200) -> Response:
    return Response(body, status_code=status, media_type="text/xml", headers={"Cache-Control": "no-store"})


def phone_routes(access: PhoneAccess) -> list[Route]:
    async def incoming(request: Request) -> Response:
        """A call to Cora's number. Only Twilio, signing with the account's auth token, gets an answer."""
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/x-www-form-urlencoded":
            return Response(status_code=415)
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > BODY_MAX_BYTES:
                return Response(status_code=413)
        params = parse_qsl(body.decode("utf-8", errors="replace"), keep_blank_values=True)
        # Signed over the address Twilio was given (the public one), not the one the proxy passes on.
        url = access.public_url() + INCOMING_PATH
        if not valid_signature(access.token(), url, params, request.headers.get("x-twilio-signature")):
            return Response(status_code=403)
        fields = dict(params)
        return twiml(access.on_call(fields.get("From", ""), fields.get("CallSid", "")))

    return [Route(INCOMING_PATH, incoming, methods=["POST"])]
