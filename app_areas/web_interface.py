"""The web interface: turning it on, pairing devices and the list of them: part of CoraApp (see app.py).

The page and its API are served by the same listener as the webhooks (app_areas/webhooks.py).
"""

import asyncio
from contextvars import ContextVar
import json
import secrets
from uuid import UUID

from textual import work

from backend_chat import WEB_EXCLUDED, BackendRoute
from live import typed_content
from messaging.messages import APPROVAL_SECONDS
from session_config import WEB_CHAT_INSTRUCTIONS
from storage.web_device_store import WebDevice
from ui.model_settings import WebControls
from web.feed import APPROVAL, APPROVAL_DONE, KEEP as FEED_KEEP
from web.pairing import shown_code
from web.routes import WebAccess

# The page's own mark for the message being answered, set for that answer's task only, so the
# message and her reply reach the feed marked and that page doesn't show them twice.
WEB_REF: ContextVar[str | None] = ContextVar("web_ref", default=None)
# What a request for approval shows of what would be done: each argument on its own line.
APPROVAL_VALUE_MAX_CHARS = 300
APPROVAL_DETAIL_MAX_CHARS = 1500


def approval_detail(arguments: dict) -> str:
    """Exactly what the tool would be given, as "name: value" lines, cut short if very long."""
    lines = []
    for name, value in arguments.items():
        shown = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        if len(shown) > APPROVAL_VALUE_MAX_CHARS:
            shown = shown[:APPROVAL_VALUE_MAX_CHARS - 1] + "…"
        lines.append(f"{name}: {shown}")
    detail = "\n".join(lines)
    return detail if len(detail) <= APPROVAL_DETAIL_MAX_CHARS else detail[:APPROVAL_DETAIL_MAX_CHARS - 1] + "…"


class WebInterfaceMixin:
    """The web interface: turning it on, pairing devices and the list of them."""

    def web_access(self) -> WebAccess:
        """What the web routes may do, for the listener."""
        async def add_device(name: str, token_hash: str) -> WebDevice:
            return await self.conversation_persistence.call_database(self.persistence.add_web_device, name, token_hash)

        async def find_device(token_hash: str) -> WebDevice | None:
            return await self.conversation_persistence.call_database(self.persistence.find_web_device, token_hash)

        return WebAccess(self.web_pairing, add_device, find_device, self.on_web_paired,
                         send_message=self.answer_on_web, feed=self.web_feed, feed_ready=self.web_feed_ready,
                         answer_approval=self.answer_web_approval)

    async def ask_on_web(self, request) -> bool:
        """A tool that changes things, asked about on the page: every open page shows the request
        with Approve and Deny, and the first answer counts. Waits until answered; a request that
        runs out of time (cancelled by the caller) is shown as lapsed."""
        approval_id = secrets.token_hex(8)
        answer = asyncio.get_running_loop().create_future()
        self.web_approvals[approval_id] = answer
        self.web_feed.publish_approval(APPROVAL, {
            "id": approval_id, "title": f"{request.title} · {request.tool}",
            "detail": approval_detail(request.arguments), "seconds": APPROVAL_SECONDS,
        })
        self.notify(f"Cora is asking on the web page to use {request.title} · {request.tool}.")
        try:
            return await answer
        finally:
            del self.web_approvals[approval_id]
            # Running out of time cancels the wait, and the answer with it.
            answered = answer.done() and not answer.cancelled()
            outcome = ("approved" if answer.result() else "declined") if answered else "lapsed"
            self.web_feed.publish_approval(APPROVAL_DONE, {"id": approval_id, "outcome": outcome})

    def answer_web_approval(self, approval_id: str, approved: bool) -> bool:
        """The person's answer from a page; False if that request isn't waiting any more."""
        answer = self.web_approvals.get(approval_id)
        if answer is None or answer.done():
            return False
        answer.set_result(approved)
        return True

    def publish_to_web(self, role: str, text: str, channel: str) -> None:
        """Every message said in the conversation, for pages showing it (the conversation's listener)."""
        self.web_feed.publish(role, text, channel, ref=WEB_REF.get())

    async def web_feed_ready(self) -> None:
        """The feed holds the earlier messages too: loaded once, the first time a page connects."""
        async with self.web_feed_lock:
            if self.web_feed.seeded:
                return
            try:
                rows = await self.conversation_persistence.call_database(
                    self.persistence.load_recent_person_messages, FEED_KEEP,
                )
            except Exception as error:
                # Pages still get what's said from now on.
                self.activity.write(f"Earlier messages didn't load for the web page ({type(error).__name__}).",
                                    "Web interface")
                rows = []
            self.web_feed.seed(rows)

    async def answer_on_web(self, device: WebDevice, text: str, on_text, ref: str | None = None) -> str | None:
        """A message from the web page: the same conversation as the desk, answered like Signal.

        One at a time, so a second message is answered with the first exchange in its history.
        on_text gets her reply so far as it streams to the page; the desk shows the finished one.
        """
        async def write(history: list[dict]) -> str | None:
            try:
                # Tools that change things are asked about on the page, with Approve and Deny.
                route = BackendRoute(
                    excluded=WEB_EXCLUDED, ask_approval=self.ask_on_web, approval_seconds=APPROVAL_SECONDS,
                    tool_seconds=APPROVAL_SECONDS + 60, asked_where="on the web page",
                )
                reply = await self.reply_in_writing(
                    typed_content(text), history, route, WEB_CHAT_INSTRUCTIONS, on_text=on_text,
                )
                if not reply:
                    raise ValueError("empty reply")
                return reply
            except Exception as error:
                # The type only: a message could echo the conversation.
                self.activity.write(f"Web reply failed ({type(error).__name__}).", "Web reply failed")
                return None

        async with self.web_reply_lock:
            if self.closing:
                return None
            self.activity.write(f"Answering your message from the web page ({device.name}).", "Web reply")
            # Marked only while this message and her reply are said; anything after isn't.
            marked = WEB_REF.set(ref)
            try:
                reply = await self.answer_person_in_writing(
                    [text], self.conversation.show_web_message, write,
                    label="Cora · web", channel="web", where="wrote to you on your web page",
                )
            finally:
                WEB_REF.reset(marked)
            if reply is not None:
                self.activity.write(f"Web reply sent: characters={len(reply)}", "Web reply sent")
            return reply

    def on_web_paired(self, device: WebDevice) -> None:
        self.activity.write(f"Paired a device with the web interface: {device.name}.", "Web interface")
        self.notify(f"Paired: {device.name}")
        self.load_web_devices()

    @work(group="web-devices", exclusive=True, exit_on_error=False)
    async def load_web_devices(self) -> None:
        try:
            self.web_devices = await self.conversation_persistence.call_database(self.persistence.load_web_devices)
        except Exception as error:
            self.activity.write(f"Paired devices didn't load ({type(error).__name__}).", "Web interface")

    @work(group="web-devices-remove", exit_on_error=False)
    async def remove_web_device(self, device_id: UUID) -> None:
        try:
            await self.conversation_persistence.call_database(self.persistence.remove_web_device, device_id)
        except Exception as error:
            self.activity.write(f"Unpairing a device failed ({type(error).__name__}).", "Web interface")
            self.notify("That device couldn't be removed. Check Activity.", severity="error")
            return
        self.activity.write("Unpaired a device from the web interface.", "Web interface")
        self.load_web_devices()

    @work(group="webhook-setup", exit_on_error=False)
    async def set_web_enabled(self, enabled: bool) -> None:
        if not enabled:
            self.web_pairing.cancel()
        try:
            await self.conversation_persistence.call_database(self.persistence.save_web_enabled, enabled)
        except Exception as error:
            self.activity.write(f"Saving the web interface {'on' if enabled else 'off'} failed "
                                f"({type(error).__name__}).", "Web interface")
            self.notify("That couldn't be saved. Check Activity.", severity="error")
            return
        await self.apply_webhook_settings()

    def start_web_pairing(self) -> None:
        if not (self.webhook_settings.web_enabled and self.webhook_listening()):
            self.notify("Turn the web interface on first, and wait until it's listening.", severity="warning")
            return
        self.web_pairing.start()
        self.activity.write("A web pairing code is open.", "Web interface")

    def web_pairing_shown(self) -> tuple[str, str, int] | None:
        """(the link in the QR code, the code as shown, seconds left), while a code is open."""
        code = self.web_pairing.current()
        if code is None or not self.webhook_settings.public_url:
            return None
        link = f"{self.webhook_settings.public_url}/#pair={code.code}"
        return link, shown_code(code.code), self.web_pairing.seconds_left()

    def web_status(self) -> str:
        if not self.webhook_settings.web_enabled:
            return ""
        if not self.webhook_listening():
            return "not listening"
        return self.webhook_settings.public_url or ""

    def make_web_controls(self) -> WebControls:
        self.load_web_devices()
        self.web_controls = WebControls(
            status=self.web_status, set_enabled=self.set_web_enabled,
            start_pairing=self.start_web_pairing, cancel_pairing=self.web_pairing.cancel,
            pairing=self.web_pairing_shown, devices=lambda: self.web_devices,
            remove_device=self.remove_web_device, enabled=self.webhook_settings.web_enabled,
        )
        return self.web_controls
