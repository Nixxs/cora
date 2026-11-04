"""The web interface: turning it on, pairing devices and the list of them: part of CoraApp (see app.py).

The page and its API are served by the same listener as the webhooks (app_areas/webhooks.py).
"""

from uuid import UUID

from textual import work

from backend_chat import WEB_EXCLUDED, BackendRoute
from live import typed_content
from session_config import WEB_CHAT_INSTRUCTIONS
from storage.web_device_store import WebDevice
from ui.model_settings import WebControls
from web.pairing import shown_code
from web.routes import WebAccess


class WebInterfaceMixin:
    """The web interface: turning it on, pairing devices and the list of them."""

    def web_access(self) -> WebAccess:
        """What the web routes may do, for the listener."""
        async def add_device(name: str, token_hash: str) -> WebDevice:
            return await self.conversation_persistence.call_database(self.persistence.add_web_device, name, token_hash)

        async def find_device(token_hash: str) -> WebDevice | None:
            return await self.conversation_persistence.call_database(self.persistence.find_web_device, token_hash)

        return WebAccess(self.web_pairing, add_device, find_device, self.on_web_paired,
                         send_message=self.answer_on_web)

    async def answer_on_web(self, device: WebDevice, text: str, on_text) -> str | None:
        """A message from the web page: the same conversation as the desk, answered like Signal.

        One at a time, so a second message is answered with the first exchange in its history.
        on_text gets her reply so far as it streams to the page; the desk shows the finished one.
        """
        async with self.web_reply_lock:
            if self.closing:
                return None
            self.activity.write(f"Answering your message from the web page ({device.name}).", "Web reply")

            async def write(history: list[dict]) -> str | None:
                try:
                    reply = await self.reply_in_writing(
                        typed_content(text), history, BackendRoute(excluded=WEB_EXCLUDED), WEB_CHAT_INSTRUCTIONS,
                        on_text=on_text,
                    )
                    if not reply:
                        raise ValueError("empty reply")
                    return reply
                except Exception as error:
                    # The type only: a message could echo the conversation.
                    self.activity.write(f"Web reply failed ({type(error).__name__}).", "Web reply failed")
                    return None

            reply = await self.answer_person_in_writing(
                [text], self.conversation.show_web_message, write,
                label="Cora · web", channel="web", where="wrote to you on your web page",
            )
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
