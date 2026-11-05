"""The web interface: turning it on, pairing devices and the list of them: part of CoraApp (see app.py).

The page and its API are served by the same listener as the webhooks (app_areas/webhooks.py).
"""

import asyncio
from contextvars import ContextVar
import io
import json
import secrets
from uuid import UUID

from textual import work

from backend_chat import WEB_EXCLUDED, BackendRoute
from live import typed_content
from messaging.images import photo_content, photo_described, read_photo
from messaging.messages import APPROVAL_SECONDS, image_seen_content
from messaging.receiver import WITHHELD_IMAGE
from tools.look_at_screen import PASTE_QUESTION, WEB_PHOTO_INSTRUCTIONS
from tools.where_is_person import NOTE as WHERE_NOTE, WHY_NOT as WHERE_NOT
from session_config import WEB_CHAT_INSTRUCTIONS
from storage.web_device_store import WebDevice
from ui.model_settings import WebControls
from web import maps
from web.feed import APPROVAL, APPROVAL_DONE, KEEP as FEED_KEEP, MAP, PHOTO, PICTURE
from web.pairing import shown_code
from web.routes import WebAccess

# The page's own mark for the message being answered, set for that answer's task only, so the
# message and her reply reach the feed marked and that page doesn't show them twice.
WEB_REF: ContextVar[str | None] = ContextVar("web_ref", default=None)
# What voice Cora is told as a call on the web page starts or ends, so she knows where the person is.
TALKING_FROM = (
    "they're talking to you from their {device} through your web page, probably away from the "
    "desk, so the desk's screen and microphone aren't where they are. If they want to show you "
    "something, they can send a photo from the page; if where they are matters to what they "
    "ask, where_is_person can check."
)
BACK_AT_DESK = "they're talking to you at the desk again, on its microphone and speaker."
# Her pictures for the page are kept this size at most.
PICTURE_MAX_SIDE = 1600
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
                         answer_approval=self.answer_web_approval, voice=self.web_voice,
                         on_listening=self.on_web_listening, on_talk=self.on_web_talk, photo=self.web_photo,
                         answer_location=lambda device, body: self.whereabouts.answer(device.name, body))

    def on_web_talk(self, device: WebDevice, listener, talking: bool) -> None:
        """A page starts or stops talking to her. Starting wakes her from standby (and hanging up
        then puts her back), and Live hears that device's microphone instead of the desk's; only
        one device talks at a time, so another taking over ends the first."""
        if talking:
            previous = self.web_voice.start_talking(listener)
            if previous is not None:
                self.web_voice.tell(previous, {"type": "talk_ended", "reason": "taken over"})
            elif self.standby or not self.live.ready:
                # Woken for this call: hanging up puts her back.
                self.web_talk_woke = True
                if self.standby and not self.reconnecting:
                    self.action_reconnect()
                    if self.reconnecting:
                        self.pending_greeting = "welcome_back"
            self.run_worker(self.live.use_web_microphone(listener.mic), group="web-talk", exit_on_error=False)
            self.web_voice.tell(listener, {"type": "talking"})
            self.activity.write(f"Talking to Cora from {device.name}; the desk mic is quiet.", "Web voice")
            self.tell_cora_where(TALKING_FROM.format(device=device.name))
            return
        if not self.web_voice.stop_talking(listener):
            return
        self.end_web_talk(f"{device.name} hung up.")
        self.web_voice.tell(listener, {"type": "talk_ended", "reason": "hung up"})

    def end_web_talk(self, why: str) -> None:
        """The desk mic hears her again; if a page's Talk woke her, she goes back to standby
        (once she's finished speaking and anything pending is done, as when she asks for it)."""
        self.run_worker(self.live.use_web_microphone(None), group="web-talk", exit_on_error=False)
        woke = self.web_talk_woke
        self.web_talk_woke = False
        if woke and self.live.ready and not self.standby:
            self.request_standby()
            self.activity.write(f"{why} Cora goes back to standby, as Talk woke her.", "Web voice")
        else:
            self.activity.write(f"{why} The desk mic hears her again.", "Web voice")
            # She stays on: she's told the person is back on the desk's mic and speaker.
            self.tell_cora_where(BACK_AT_DESK)

    def tell_cora_where(self, where: str) -> None:
        """Let voice Cora know where the person is talking from; if she's still waking (Talk
        woke her), it's told once she's connected (see web_voice_connected)."""
        self.web_talk_where = where
        if self.live.ready:
            self.run_worker(self.send_web_talk_where(), group="web-talk-where", exit_on_error=False)

    def web_voice_connected(self) -> None:
        """Live is active: anything she's still to be told about where the person is goes now."""
        if self.web_talk_where is not None:
            self.run_worker(self.send_web_talk_where(), group="web-talk-where", exit_on_error=False)

    async def send_web_talk_where(self) -> None:
        where, self.web_talk_where = self.web_talk_where, None
        if where is None:
            return
        try:
            if not await self.live.note_where_they_are(where):
                self.web_talk_where = where  # not connected after all: told when she is
        except Exception as error:
            self.activity.write(f"Telling Cora where you're talking from failed ({type(error).__name__}).",
                                "Web voice")

    def web_talk_lost_live(self) -> None:
        """She went into standby or disconnected while a page was talking: that call ends."""
        talker = self.web_voice.talker
        if talker is None or self.reconnecting:
            return
        self.web_voice.stop_talking(talker)
        self.web_talk_woke = False
        # Not carried into her next session.
        self.web_talk_where = None
        self.run_worker(self.live.use_web_microphone(None), group="web-talk", exit_on_error=False)
        self.web_voice.tell(talker, {"type": "talk_ended", "reason": "standby"})
        self.activity.write(f"Talk on {talker.device} ended: Cora is in standby.", "Web voice")

    def on_web_listening(self, device: WebDevice, listening: bool) -> None:
        """A page started or stopped playing her voice: the desk speaker is quiet while any does."""
        if listening:
            if len(self.web_voice.listeners) == 1:
                # What the desk was still about to say goes quiet too, rather than both at once.
                self.live.yield_speaker()
            self.activity.write(f"Cora's voice is playing on {device.name}; the desk speaker is quiet.",
                                "Web voice")
        else:
            self.activity.write(
                f"{device.name} stopped listening."
                + ("" if self.web_voice.active else " The desk speaker plays her voice again."),
                "Web voice",
            )

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

    def publish_web_photo(self, image_id: str) -> None:
        """One of the person's kept photos, for every page (the one that sent it shows its own)."""
        self.web_feed.publish_image(PHOTO, {"id": image_id, "ref": WEB_REF.get()}, "user")

    def publish_web_picture(self, image, caption: str, page_url: str) -> None:
        """A picture she showed (a PIL image from show_web_image), kept in memory for the page."""
        picture = image.convert("RGB")
        picture.thumbnail((PICTURE_MAX_SIDE, PICTURE_MAX_SIDE))
        buffer = io.BytesIO()
        picture.save(buffer, format="JPEG", quality=85)
        picture_id = self.web_pictures.keep(buffer.getvalue(), caption, page_url)
        self.web_feed.publish_image(PICTURE, {"id": picture_id, "caption": caption, "url": page_url}, "assistant")

    def web_photo(self, image_id: str) -> bytes | None:
        """A kept photo of the person's (img…) or a picture she showed (pic…), for a page; None if gone."""
        kept = (self.web_pictures if image_id.startswith("pic") else self.kept_images).get(image_id)
        return kept.jpeg if kept is not None else None

    async def locate_person(self, refresh: bool) -> dict:
        """where_is_person: the place the person's open page says they are, for Cora. Each check
        shows a quiet line in the chat and on the page, and one in Activity (without the place)."""
        where, why_not, fresh = await self.whereabouts.check(refresh)
        if where is None:
            self.activity.write(f"Cora tried to check where you are: {why_not}.", "Where you are")
            return {"status": "unavailable", "message": WHERE_NOT[why_not]}
        minutes = int((self.whereabouts.clock() - where.at) // 60)
        if fresh:
            note = f"Cora checked where you are, from {where.device}: {where.place} (±{where.accuracy} m)."
            self.activity.write(f"Cora checked where you are, from {where.device}.", "Where you are")
        else:
            note = f"Cora used where you were {minutes} min ago: {where.place}." if minutes else (
                f"Cora used where you were just now: {where.place}.")
            self.activity.write("Cora used where you were at the last check.", "Where you are")
        self.conversation.show_note(note)
        self.web_feed.publish_notice(note)
        return {"status": "ok", "place": where.place, "accuracy_metres": where.accuracy,
                "from_device": where.device, "checked_minutes_ago": minutes, "note": WHERE_NOTE}

    def unavailable_tools(self) -> frozenset[str]:
        """Tools not set up, so not offered anywhere: show_map without a Google Maps key."""
        return frozenset() if self.maps_key else frozenset({"show_map"})

    def show_map(self, view: maps.MapView) -> bool:
        """show_map: the map on every open page, and its caption and link in the desk's chat.
        False when no key is set (her session may still list the tool until she reconnects)."""
        if not self.maps_key:
            return False
        caption, link = view.caption(), view.link()
        self.conversation.show_map(caption, link)
        self.web_feed.publish_image(MAP, {"caption": caption, "embed": view.embed_url(self.maps_key), "link": link},
                                    "assistant")
        self.activity.write(f"Cora showed a map ({view.mode}).", "Map")
        return True

    async def load_maps_key(self) -> None:
        try:
            self.maps_key = await asyncio.to_thread(maps.maps_key)
        except Exception as error:
            self.activity.write(f"The Google Maps key didn't load ({type(error).__name__}).", "Maps")

    @work(group="maps-key", exit_on_error=False)
    async def save_maps_key(self, key: str) -> None:
        """The Google Maps key, in the system credential store; empty forgets it. Her voice session
        gets show_map (or loses it) at her next connect; written chats at once."""
        try:
            saved = await asyncio.to_thread(maps.save_maps_key, key)
        except ValueError as error:
            self.notify(str(error), severity="warning")
            return
        except Exception as error:
            self.activity.write(f"Saving the Google Maps key failed ({type(error).__name__}).", "Maps")
            self.notify("The Google Maps key couldn't be saved. Check Activity.", severity="error")
            return
        changed = bool(saved) != bool(self.maps_key)
        self.maps_key = saved
        if self.web_controls is not None:
            self.web_controls.has_maps_key = bool(saved)
        if changed:
            # Like service tools: she gets the change when Settings closes.
            self.service_tools_changed = True
        self.activity.write("Saved the Google Maps key." if saved else "Forgot the Google Maps key.", "Maps")
        self.notify("Maps are on: Cora can show maps on the web page." if saved else "Maps are off.")

    def publish_to_web(self, role: str, text: str, channel: str, device: str | None = None) -> None:
        """Every message said in the conversation, for pages showing it (the conversation's listener)."""
        self.web_feed.publish(role, text, channel, ref=WEB_REF.get(), device=device)

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

    async def answer_on_web(self, device: WebDevice, text: str, on_text, ref: str | None = None,
                            image: bytes | None = None) -> str | None:
        """A message from the web page: the same conversation as the desk, answered like Signal.

        One at a time, so a second message is answered with the first exchange in its history.
        on_text gets her reply so far as it streams to the page; the desk shows the finished one.
        image: a photo sent with it, which she sees herself on this turn (see read_web_photo).
        """
        content, described = typed_content(text), None
        if image is not None:
            content, described = await self.read_web_photo(text, image)
        shown = f"[image] {text}".rstrip() if image is not None else text

        def show(said: str) -> None:
            self.conversation.show_web_message(said, device.name)
            if described is not None:
                self.conversation.show_photo_note(described)

        async def write(history: list[dict]) -> str | None:
            try:
                # Tools that change things are asked about on the page, with Approve and Deny.
                route = BackendRoute(
                    excluded=WEB_EXCLUDED | self.unavailable_tools(), ask_approval=self.ask_on_web, approval_seconds=APPROVAL_SECONDS,
                    tool_seconds=APPROVAL_SECONDS + 60, asked_where="on the web page",
                )
                reply = await self.reply_in_writing(
                    content, history, route, WEB_CHAT_INSTRUCTIONS, on_text=on_text,
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
                    [shown], show, write, label="Cora · web", channel="web", where="wrote to you on your web page",
                    noted=f"{shown}\n{described}" if described else None,
                )
            finally:
                WEB_REF.reset(marked)
            if reply is not None:
                self.activity.write(f"Web reply sent: characters={len(reply)}", "Web reply sent")
            return reply

    async def read_web_photo(self, text: str, image: bytes) -> tuple[list[dict], str | None]:
        """(this turn's content, the photo's description for later turns or None).

        The hybrid: she sees the photo itself on this turn (a written turn is a request of its own,
        so it never fills a session), while a separate look describes it for later turns and
        voice, and withholds explicit, gory or abusive ones before she sees anything. The photo is
        kept in memory for look_again; never saved.
        """
        try:
            jpeg, seen = await asyncio.to_thread(read_photo, text or PASTE_QUESTION, image, WEB_PHOTO_INSTRUCTIONS)
        except Exception as error:
            self.activity.write(f"A photo from the web page couldn't be read ({type(error).__name__}).",
                                "Image look failed")
            return typed_content(text) + [{"type": "input_text", "text": (
                "A photo was sent with this message but couldn't be opened. Say so rather than guessing."
            )}], None
        if seen == WITHHELD_IMAGE:
            self.activity.write("Withheld an explicit, gory or abusive photo from Cora.", "Image withheld")
            return typed_content(text) + [image_seen_content(WITHHELD_IMAGE)], None
        described = photo_described(self.kept_images.keep(jpeg), seen)
        self.activity.write("Read a photo from the web page for Cora.", "Image read")
        return photo_content(text, jpeg, described), described

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
            has_maps_key=bool(self.maps_key), save_maps_key=self.save_maps_key,
        )
        return self.web_controls
