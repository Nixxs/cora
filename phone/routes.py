"""The listener's routes for phone calls (for server/listener.py's build_app): Twilio asks here what
to do with each call to Cora's number, then streams a connected call's audio both ways."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import json
from urllib.parse import parse_qsl

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from web.voice import Listener, VoiceRelay

from .audio import CallAudio
from .twilio import INCOMING_PATH, STREAM_PATH, valid_signature

# Twilio's call requests are a few dozen short fields.
BODY_MAX_BYTES = 16 * 1024
# How long Twilio has, once the WebSocket opens, to say which call it is.
START_SECONDS = 10
# A stream message is a JSON object with at most a few hundred bytes of audio.
MESSAGE_MAX_CHARS = 16 * 1024
# What the device listening is called, for the relay and Activity.
PHONE_DEVICE = "phone"
# How often the wait for her audio wakes to check the call is still there.
KEEP_ALIVE_SECONDS = 15


@dataclass
class PhoneAccess:
    """What the routes need from the app."""

    # The Twilio auth token ("" if none), and the public address Twilio calls (https://host).
    token: Callable[[], str]
    public_url: Callable[[], str]
    # on_call(caller, call_id) -> the TwiML answer for a call that Twilio has vouched for.
    on_call: Callable[[str, str], str]
    # claim(token) -> whether it's a one-time token on_call gave out (and it's now used up).
    claim: Callable[[str], bool] | None = None
    # Her voice, and the caller's for her (the same relay as the web page's voice).
    voice: VoiceRelay | None = None
    # on_stream(listener, connected): a call's audio starts or stops reaching her.
    on_stream: Callable[[Listener, bool], None] | None = None


def twiml(body: str, status: int = 200) -> Response:
    return Response(body, status_code=status, media_type="text/xml", headers={"Cache-Control": "no-store"})


async def call_started(websocket: WebSocket) -> dict | None:
    """Twilio's "start" message for the stream (after "connected"), or None if it doesn't come."""
    async def first_start() -> dict | None:
        while True:
            message = json.loads(await websocket.receive_text())
            if not isinstance(message, dict) or message.get("event") not in ("connected", "start"):
                return None
            if message["event"] == "start":
                return message.get("start") if isinstance(message.get("start"), dict) else None

    try:
        return await asyncio.wait_for(first_start(), START_SECONDS)
    except (TimeoutError, ValueError, KeyError, WebSocketDisconnect, RuntimeError):
        return None


def phone_routes(access: PhoneAccess) -> list[BaseRoute]:
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

    async def stream(websocket: WebSocket) -> None:
        """A connected call's audio (Twilio's media stream): the caller's voice for her, and hers
        for the caller. Only a stream carrying a token on_call gave out, unused, gets in."""
        if access.voice is None or access.claim is None or access.on_stream is None:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        start = await call_started(websocket)
        parameters = (start or {}).get("customParameters") or {}
        stream_id = (start or {}).get("streamSid")
        token = parameters.get("token") if isinstance(parameters, dict) else None
        if not isinstance(stream_id, str) or not isinstance(token, str) or not access.claim(token):
            await websocket.close(code=1008)
            return
        audio = CallAudio()
        listener = access.voice.listen(PHONE_DEVICE)
        access.on_stream(listener, True)

        async def hear_caller() -> None:
            """The caller's audio, until the call ends."""
            try:
                while True:
                    text = await websocket.receive_text()
                    if len(text) > MESSAGE_MAX_CHARS:
                        continue
                    try:
                        message = json.loads(text)
                    except ValueError:
                        continue
                    if not isinstance(message, dict):
                        continue
                    if message.get("event") == "stop":
                        return
                    media = message.get("media")
                    if message.get("event") == "media" and isinstance(media, dict) and media.get("track", "inbound") == "inbound":
                        payload = media.get("payload")
                        if isinstance(payload, str):
                            try:
                                access.voice.hear(listener, audio.for_live(payload))
                            except ValueError:
                                continue
            except (WebSocketDisconnect, RuntimeError):
                return

        hearing = asyncio.ensure_future(hear_caller())
        try:
            while True:
                sending = asyncio.ensure_future(access.voice.next_chunk(listener, KEEP_ALIVE_SECONDS))
                await asyncio.wait({sending, hearing}, return_when=asyncio.FIRST_COMPLETED)
                if hearing.done():
                    # The caller hung up: stop at once, so the desk gets its mic and speaker back.
                    sending.cancel()
                    return
                try:
                    chunk = sending.result()
                except TimeoutError:
                    continue
                if chunk is None or (isinstance(chunk, dict) and chunk.get("type") == "talk_ended"):
                    # Fell too far behind, or the call ended at her side (standby, or a page took
                    # over): closing the stream ends the call.
                    return
                if isinstance(chunk, bytes):
                    payload = audio.for_caller(chunk)
                    if payload:
                        await websocket.send_text(json.dumps(
                            {"event": "media", "streamSid": stream_id, "media": {"payload": payload}}))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            hearing.cancel()
            access.voice.stop(listener)
            access.on_stream(listener, False)
            try:
                await websocket.close()
            except RuntimeError:
                pass

    return [Route(INCOMING_PATH, incoming, methods=["POST"]), WebSocketRoute(STREAM_PATH, stream)]
