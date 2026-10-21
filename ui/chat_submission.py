"""Typed-message transaction and draft state, independent of worker scheduling."""
import asyncio
from uuid import UUID, uuid4
from tools.look_at_screen import PASTE_INSTRUCTIONS, PASTE_QUESTION, look
from ui.composer import ChatComposer


class ChatSubmission:
    def __init__(self, *, persistence, conversation, live, backend_work,
                 is_closing, is_reconnecting, is_standby, schedule,
                 record_activity, write_activity, notify, trace):
        self.persistence = persistence
        self.conversation = conversation
        self.live = live
        self.backend_work = backend_work
        self.is_closing = is_closing
        self.is_reconnecting = is_reconnecting
        self.is_standby = is_standby
        self.schedule = schedule
        self.record_activity = record_activity
        self.write_activity = write_activity
        self.notify = notify
        self.trace = trace
        self.pending_message: tuple[str, UUID] | None = None
        self.worker = None

    def submitted(self, event: ChatComposer.Submitted) -> None:
        self.record_activity(user_activity=True)
        message = event.text.strip()
        composer = event.composer

        if (not message and not composer.image_png) or composer.disabled \
                or self.is_closing() or self.is_reconnecting():
            return
        if self.is_standby():
            self.notify("Live is in standby. Click Standby or press Ctrl+R to resume; your draft is kept.", severity="warning")
            return

        composer.disabled = True
        self.conversation.flush_transcript()
        if self.pending_message is None or self.pending_message[0] != message:
            self.pending_message = (message, uuid4())
        message_id = self.pending_message[1]
        self.worker = self.schedule(message, message_id, composer)

    async def submit_keyboard_message(
        self, message: str, message_id: UUID, composer: ChatComposer
    ) -> None:
        try:
            if self.is_closing():
                return
            image_seen = None
            if composer.image_png:
                # A separate one-off request reads the image so it never fills Cora's session.
                try:
                    image_seen = await asyncio.to_thread(
                        look, message or PASTE_QUESTION, composer.image_png,
                        mime="image/png", instructions=PASTE_INSTRUCTIONS,
                    )
                except Exception as error:
                    # Errors may echo the request; log only the type.
                    self.write_activity(
                        f"Pasted image look failed ({type(error).__name__}). Draft kept; nothing sent.",
                        "Image look failed",
                    )
                    self.notify("Message not sent: the image could not be read. Draft kept.", severity="error")
                    return
                self.write_activity("Pasted image read for Cora", "Image read")
                if self.is_closing():
                    return
            # Image bytes are never stored; the saved and shown text only marks one was sent.
            shown = f"[image] {message}".rstrip() if composer.image_png else message
            try:
                await self.persistence.call_database(self.persistence.storage.save_message, message_id, shown)
            except Exception as error:
                # SQL errors may include query parameters: never log private text/credentials.
                self.write_activity(
                    f"Database save failed ({type(error).__name__}). Draft kept; nothing sent.",
                    "Database save failed",
                )
                self.notify("Message not sent: database save failed. Draft kept.", severity="error")
                return
            self.write_activity(
                f"Keyboard message saved: conversation_id={self.persistence.storage.conversation_id}, "
                f"message_id={message_id}",
                "Keyboard message saved",
            )
            if self.is_closing():
                # Saved text remains pending; shutdown must not start a new send.
                return
            try:
                self.backend_work.start_typed_request()
                self.trace("typed.submit", message_id=str(message_id))
                if image_seen:
                    await self.live.send_text(message, image_seen=image_seen)
                else:
                    await self.live.send_text(message)
                self.trace("typed.sent", message_id=str(message_id))
            except Exception as error:
                self.backend_work.finish_typed_request()
                self.trace("typed.failed", message_id=str(message_id), error_type=type(error).__name__)
                # A failed send may have reached the backend partially; do not auto-retry it.
                self.pending_message = None
                await self.record_keyboard_delivery(message_id, "failed")
                self.write_activity(f"Text send error: {error}", "Text send error")
                return

            self.pending_message = None
            await self.record_keyboard_delivery(message_id, "sent")
            self.conversation.flush_transcript()
            self.conversation.write_log(f"You: {shown}")
            self.conversation.write_log("")
            composer.clear()
            composer.remove_image()
        finally:
            self.record_activity()
            composer.disabled = self.is_closing() or self.is_reconnecting()
            if not composer.disabled:
                composer.focus()

    async def record_keyboard_delivery(self, message_id: UUID, status: str) -> None:
        try:
            await self.persistence.call_database(self.persistence.storage.record_delivery, message_id, status)
        except Exception as error:
            self.write_activity(
                f"Could not confirm database delivery status '{status}' for message_id={message_id} "
                f"({type(error).__name__}). Text was already saved.",
                "Delivery status error",
            )
            self.notify("Text saved, but database delivery status could not be confirmed.", severity="warning")

