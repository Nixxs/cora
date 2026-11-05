"""The web interface's routes, for the listener's build_app (server/listener.py).

The page's files are public: they hold nothing of Cora's. Everything under /api/ that reaches her
needs a paired device, known by the token in its cookie (see web/pairing.py).
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import json
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import BaseRoute, Mount, Route
from starlette.staticfiles import StaticFiles

from storage.web_device_store import WebDevice
from .feed import MESSAGE, FeedMessage, MessageFeed, Status
from .pairing import NO_CODE, PAIRED, Pairing, new_token, token_hash
from .rendering import chat_html

STATIC_DIR = Path(__file__).resolve().parent / "static"
COOKIE = "cora_device"
# Browsers cap cookie lifetimes at about 400 days; a device seen within that stays paired.
COOKIE_SECONDS = 400 * 24 * 60 * 60
BODY_MAX_BYTES = 2048
# A message from the page: room for a long one, not for anything abusive.
MESSAGE_MAX_CHARS = 8000
MESSAGE_BODY_MAX_BYTES = 4 * MESSAGE_MAX_CHARS + 1024
REPLY_FAILED = "Cora couldn't reply. Check Activity on her PC."
REF_MAX_CHARS = 64
# Something is sent at least this often, so proxies (Cloudflare closes a quiet connection after
# about 100 seconds) keep the live feed open.
KEEP_ALIVE_SECONDS = 20


class PageFiles(StaticFiles):
    """The page's files, which browsers must check with Cora before reusing.

    Without this, a browser (and iOS Home Screen apps most of all) keeps an old style.css or
    script after an update, so the page breaks until it's force-refreshed. Checking is cheap: an
    unchanged file answers 304 with no body.
    """

    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


@dataclass
class WebAccess:
    """What the routes need from the app: the open pairing code and the paired devices."""

    pairing: Pairing
    add_device: Callable[[str, str], Awaitable[WebDevice]]
    # The device holding a token hash, or None; marks it as seen.
    find_device: Callable[[str], Awaitable[WebDevice | None]]
    on_paired: Callable[[WebDevice], None] = lambda device: None
    # send_message(device, text, on_text, ref) -> her reply, or None if it failed. on_text gets
    # her text so far as it streams in. It carries on if the page goes away, so the reply is still
    # shown at the desk and saved. ref is the page's own mark for the message, carried into the
    # feed so that page doesn't show it twice.
    send_message: Callable[[WebDevice, str, Callable[[str], None], str | None], Awaitable[str | None]] | None = None
    # The conversation as the page sees it, and waiting until its earlier messages are loaded.
    feed: MessageFeed | None = None
    feed_ready: Callable[[], Awaitable[None]] = lambda: asyncio.sleep(0)
    # answer_approval(id, approved) -> whether that request was still waiting.
    answer_approval: Callable[[str, bool], bool] | None = None


def answer(body: dict, status: int = 200) -> JSONResponse:
    # Nothing about a device or Cora is kept in a browser's or proxy's cache.
    return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


async def read_json(request: Request, max_bytes: int = BODY_MAX_BYTES) -> dict | None:
    """A small JSON object, or None. JSON only, so a plain form on another site can't post here."""
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        return None
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > max_bytes:
            return None
    try:
        value = json.loads(body)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def line(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False) + "\n").encode()


async def reply_lines(updates: asyncio.Queue):
    """Her reply as it streams, one JSON object a line: {"type": "delta", "text"} for each new
    piece, then {"type": "done", "html"} or {"type": "error", "message"}."""
    sent = 0
    while True:
        kind, value = await updates.get()
        # Anything that queued up meanwhile goes as one piece.
        while kind == "text" and not updates.empty():
            kind, value = updates.get_nowait()
        if kind == "text":
            delta = value[sent:] if value.startswith(value[:sent]) else value
            sent = len(value)
            if delta:
                yield line({"type": "delta", "text": delta})
            continue
        yield line({"type": "done", "html": chat_html(value)} if value else {"type": "error", "message": REPLY_FAILED})
        return


def feed_event(feed: MessageFeed, message: FeedMessage) -> str:
    """One feed item as a server-sent event. A message: hers as safe HTML, the person's as plain
    text. An approval or its end: its details, shown by the page as text, never as markup."""
    if message.kind != MESSAGE:
        body = {**(message.data or {}), "at": message.at.isoformat()}
        return f"id: {feed.event_id(message)}\nevent: {message.kind}\ndata: {json.dumps(body, ensure_ascii=False)}\n\n"
    body = {"role": message.role, "channel": message.channel, "at": message.at.isoformat(), "ref": message.ref,
            "device": message.device}
    if message.role == "assistant":
        body["html"] = chat_html(message.text)
    else:
        body["text"] = message.text
    return f"id: {feed.event_id(message)}\nevent: message\ndata: {json.dumps(body, ensure_ascii=False)}\n\n"


def status_event(state: str) -> str:
    # No id: a page's place in the conversation is its last message, not the desk's state.
    return f"event: status\ndata: {json.dumps({'state': state})}\n\n"


def api_routes(access: WebAccess) -> list[Route]:
    # Replies still being written, including any whose page has gone.
    replying: set[asyncio.Task] = set()

    async def device_of(request: Request) -> WebDevice | None:
        token = request.cookies.get(COOKIE)
        if not token or len(token) > 100:
            return None
        return await access.find_device(token_hash(token))

    async def session(request: Request) -> Response:
        device = await device_of(request)
        if device is None:
            return answer({"paired": False}, 401)
        return answer({"paired": True, "device": device.name})

    async def pair(request: Request) -> Response:
        body = await read_json(request)
        if body is None:
            return answer({"error": "Send the code as JSON."}, 400)
        result = access.pairing.try_code(str(body.get("code", "")))
        if result == NO_CODE:
            return answer({"error": "No pairing code is open. Open Settings › Services › Web interface "
                                    "on Cora's PC and choose Pair a device."}, 403)
        if result != PAIRED:
            return answer({"error": "That code isn't right. Check it and try again."}, 403)
        token = new_token()
        device = await access.add_device(str(body.get("name", "")), token_hash(token))
        access.on_paired(device)
        response = answer({"paired": True, "device": device.name})
        response.set_cookie(COOKIE, token, max_age=COOKIE_SECONDS, path="/", secure=True, httponly=True,
                            samesite="strict")
        return response

    async def messages(request: Request) -> Response:
        device = await device_of(request)
        if device is None:
            return answer({"error": "This device isn't paired."}, 401)
        if access.send_message is None:
            return answer({"error": "Cora can't take messages here yet."}, 503)
        body = await read_json(request, MESSAGE_BODY_MAX_BYTES)
        text = str(body.get("text", "")).strip() if body is not None else ""
        if not text:
            return answer({"error": "Send the message as JSON, with some text."}, 400)
        if len(text) > MESSAGE_MAX_CHARS:
            return answer({"error": f"That's too long: at most {MESSAGE_MAX_CHARS} characters."}, 413)
        ref = body.get("ref")
        ref = ref if isinstance(ref, str) and 0 < len(ref) <= REF_MAX_CHARS else None
        updates: asyncio.Queue = asyncio.Queue()

        async def reply() -> None:
            try:
                updates.put_nowait(("done", await access.send_message(
                    device, text, lambda so_far: updates.put_nowait(("text", so_far)), ref,
                )))
            except Exception:
                updates.put_nowait(("done", None))

        # Its own task: if the page goes away mid-reply, she still finishes, shows and saves it.
        # Held until done, since asyncio keeps only weak references to tasks.
        task = asyncio.ensure_future(reply())
        replying.add(task)
        task.add_done_callback(replying.discard)
        return StreamingResponse(reply_lines(updates), media_type="application/x-ndjson", headers={
            "Cache-Control": "no-store",
            # Ask proxies to pass each piece on at once rather than holding the stream.
            "X-Accel-Buffering": "no",
        })

    async def events(request: Request) -> Response:
        """The conversation, live: recent messages first, then each new one as it's said."""
        device = await device_of(request)
        if device is None:
            return answer({"error": "This device isn't paired."}, 401)
        feed = access.feed
        if feed is None:
            return answer({"error": "Cora can't show the conversation here yet."}, 503)
        await access.feed_ready()
        last_event_id = request.headers.get("last-event-id")

        async def stream():
            # Subscribed before reading what's kept, so nothing said in between is missed; anything
            # both kept and queued is sent once.
            queue = feed.subscribe()
            try:
                fresh, backlog = feed.since(last_event_id)
                sent = feed.seq
                # How long the browser waits before reconnecting after a drop.
                yield "retry: 3000\n\n"
                if fresh:
                    yield "event: reset\ndata: {}\n\n"
                if feed.status is not None:
                    yield status_event(feed.status)
                for message in backlog:
                    yield feed_event(feed, message)
                while True:
                    try:
                        message = await asyncio.wait_for(queue.get(), KEEP_ALIVE_SECONDS)
                    except TimeoutError:
                        yield ": still here\n\n"
                        continue
                    if message is None:
                        # Fell too far behind: closed, and the browser reconnects to catch up.
                        return
                    if isinstance(message, Status):
                        yield status_event(message.state)
                        continue
                    if message.seq > sent:
                        sent = message.seq
                        yield feed_event(feed, message)
            finally:
                feed.unsubscribe(queue)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        })

    async def approvals(request: Request) -> Response:
        """The person's answer to a request shown on the page: {"id", "approved": true or false}."""
        device = await device_of(request)
        if device is None:
            return answer({"error": "This device isn't paired."}, 401)
        body = await read_json(request)
        approval_id = body.get("id") if body is not None else None
        approved = body.get("approved") if body is not None else None
        if not isinstance(approval_id, str) or not isinstance(approved, bool) or access.answer_approval is None:
            return answer({"error": "Send the request's id and approved: true or false, as JSON."}, 400)
        if not access.answer_approval(approval_id, approved):
            return answer({"error": "That request isn't waiting any more."}, 409)
        return answer({"answered": True})

    return [
        Route("/api/session", session, methods=["GET"]),
        Route("/api/pair", pair, methods=["POST"]),
        Route("/api/messages", messages, methods=["POST"]),
        Route("/api/events", events, methods=["GET"]),
        Route("/api/approvals", approvals, methods=["POST"]),
    ]


def web_routes(access: WebAccess | None = None) -> list[BaseRoute]:
    """The API when the web interface is on (access given), then the page's files."""
    routes: list[BaseRoute] = api_routes(access) if access is not None else []
    # At the root, so it would take any path: the listener puts it after the other groups.
    routes.append(Mount("/", PageFiles(directory=STATIC_DIR, html=True), name="web"))
    return routes
