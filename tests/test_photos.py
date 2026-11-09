"""Photos to Cora (WB5a): kept in memory for look_again, seen directly in a written turn, and noted."""

import asyncio
import base64
from datetime import datetime, timezone
import io
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx2 as httpx
from PIL import Image

from messaging.images import photo_content, photo_described, read_images
from messaging.receiver import WITHHELD_IMAGE, Attachment, IncomingMessage
from server.listener import build_app
from storage.web_device_store import WebDevice
from tools.look_again import GONE_RESULT, KeptImages, LookAgainTool, validate_arguments
from web.pairing import Pairing, token_hash
from web.routes import COOKIE, PHOTO_MAX_BYTES, WebAccess, web_routes
import test_keyboard_persistence as keyboard_tests

NOW = datetime(2026, 11, 5, 9, tzinfo=timezone.utc)
IPHONE = WebDevice(uuid4(), "iPhone", NOW, NOW)
TOKEN = "a-paired-devices-token"


def jpeg_bytes(size=(64, 48)):
    buffer = io.BytesIO()
    Image.new("RGB", size, "teal").save(buffer, format="JPEG")
    return buffer.getvalue()


class KeptImagesTests(unittest.TestCase):
    def test_photos_get_ids_and_the_oldest_go_past_the_limits(self):
        kept = KeptImages(max_images=2, max_bytes=1000)
        first, second, third = kept.keep(b"a" * 100), kept.keep(b"b" * 100), kept.keep(b"c" * 100)
        self.assertEqual((first, second, third), ("img1", "img2", "img3"))
        self.assertIsNone(kept.get("img1"))
        self.assertEqual(kept.get("img3").jpeg, b"c" * 100)
        # Too many bytes: older ones go, but the newest always stays.
        kept.keep(b"d" * 950)
        self.assertEqual(list(kept.images), ["img4"])

    def test_a_conversation_ending_drops_them_and_ids_are_never_reused(self):
        kept = KeptImages()
        kept.keep(b"photo")
        kept.clear()
        self.assertIsNone(kept.get("img1"))
        self.assertEqual(kept.keep(b"photo"), "img2")


class LookAgainTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_kept_photo_is_looked_at_again_with_the_new_question(self):
        kept = KeptImages()
        image_id = kept.keep(b"jpeg-bytes")
        tool = LookAgainTool(MagicMock(), lambda: False, kept, MagicMock())
        with patch("tools.look_again.look", return_value="The price says $4.50.") as look:
            result = await tool.result(image_id, "What's the price on the second shelf?")
        self.assertEqual(look.call_args.args[:2], ("What's the price on the second shelf?", b"jpeg-bytes"))
        self.assertEqual((result["status"], result["seen"], result["image"]), ("ok", "The price says $4.50.", "img1"))

    async def test_a_photo_no_longer_kept_says_so_honestly(self):
        tool = LookAgainTool(MagicMock(), lambda: False, KeptImages(), MagicMock())
        self.assertEqual(await tool.result("img9", "What was it?"), GONE_RESULT)

    def test_it_takes_exactly_a_photo_id_and_a_question(self):
        validate_arguments({"image": "img1", "question": "What colour?"})
        for bad in ({"image": "img1"}, {"image": "", "question": "x"}, {"image": "x" * 40, "question": "x"},
                    {"image": "img1", "question": "x", "extra": 1}):
            with self.subTest(arguments=bad), self.assertRaises(ValueError):
                validate_arguments(bad)


class PhotoContentTests(unittest.TestCase):
    def test_a_written_turn_carries_the_photo_itself_then_its_description_and_id(self):
        described = photo_described("img3", "A fern in a terracotta pot.")
        self.assertTrue(described.startswith("Photo img3 (kept for this conversation; look_again"))
        content = photo_content("What plant is this?", b"jpeg", described)
        self.assertEqual(content[0], {"type": "input_text", "text": "What plant is this?"})
        self.assertEqual(content[1]["type"], "input_image")
        self.assertEqual(content[1]["image_url"], "data:image/jpeg;base64," + base64.b64encode(b"jpeg").decode())
        self.assertIn(described, content[2]["text"])
        # A photo on its own: no empty text first.
        self.assertEqual(photo_content("", b"jpeg", described)[0]["type"], "input_image")


class SignalPhotoTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_persons_photos_are_kept_with_ids_and_withheld_ones_or_contacts_arent(self):
        message = IncomingMessage("+61400000002", "Look", NOW, (
            Attachment("a.jpg", "image/jpeg", 10), Attachment("b.jpg", "image/jpeg", 10),
        ))
        kept = KeptImages()
        looks = iter(["A dog.", "WITHHELD"])
        with patch("messaging.images.look", side_effect=lambda *args, **kwargs: next(looks)):
            seen, failed = await read_images(message, AsyncMock(return_value=jpeg_bytes()), keep=kept.keep)
        self.assertEqual(failed, 0)
        self.assertEqual(seen, (photo_described("img1", "A dog."), WITHHELD_IMAGE))
        self.assertEqual(list(kept.images), ["img1"])
        with patch("messaging.images.look", return_value="A cat."):
            seen, _ = await read_images(IncomingMessage("+61400000009", "", NOW, (Attachment("c.jpg", "image/jpeg", 10),)),
                                        AsyncMock(return_value=jpeg_bytes()))
        self.assertEqual(seen, ("A cat.",))


class PhotoRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sent = []

        async def find_device(hashed):
            return IPHONE if hashed == token_hash(TOKEN) else None

        async def send_message(device, text, on_text, ref=None, **extra):
            self.sent.append((text, extra.get("image")))
            return "A fern."

        access = WebAccess(Pairing(), AsyncMock(), find_device, send_message=send_message)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(web_routes(access))),
                                        base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def post(self, body):
        return await self.client.post("/api/messages", json=body, headers={"Cookie": f"{COOKIE}={TOKEN}"})

    async def test_a_photo_goes_with_the_message_or_on_its_own(self):
        photo = jpeg_bytes()
        self.assertEqual((await self.post({"text": "What's this?", "image": base64.b64encode(photo).decode()})).status_code, 200)
        self.assertEqual((await self.post({"text": "", "image": base64.b64encode(photo).decode()})).status_code, 200)
        self.assertEqual(self.sent, [("What's this?", photo), ("", photo)])

    async def test_a_broken_or_oversized_photo_is_refused(self):
        self.assertEqual((await self.post({"text": "Hi", "image": "not base64!"})).status_code, 400)
        big = base64.b64encode(bytes(PHOTO_MAX_BYTES + 1)).decode()
        self.assertEqual((await self.post({"text": "Hi", "image": big})).status_code, 413)
        self.assertEqual((await self.post({"text": "", "image": ""})).status_code, 400)
        self.assertEqual(self.sent, [])


class WebPhotoAppTests(unittest.IsolatedAsyncioTestCase):
    async def test_she_sees_the_photo_this_turn_and_later_turns_have_its_description_and_id(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.backend_chat.reply = AsyncMock(return_value="That's a Boston fern.")
            app.live.note_signal_exchange = AsyncMock()
            with patch("app_areas.web_interface.read_photo", return_value=(b"scaled", "A fern in a pot.")):
                await app.answer_on_web(IPHONE, "What plant is this?", lambda text: None, image=b"original")
            content = app.backend_chat.reply.await_args.kwargs["content"]
            self.assertEqual([part["type"] for part in content], ["input_text", "input_image", "input_text"])
            self.assertEqual(app.kept_images.get("img1").jpeg, b"scaled")
            self.assertIn("You · web · iPhone: [image] What plant is this?", app.conversation.log_entries)
            later = [item["content"][0]["text"] for item in app.conversation.get_reconnect_history()]
            self.assertIn("(The photo I sent. " + photo_described("img1", "A fern in a pot.") + ")", later)
            # Voice Cora is told what it showed.
            noted = app.live.note_signal_exchange.await_args.args[0]
            self.assertIn("A fern in a pot.", noted)

    async def test_a_withheld_photo_isnt_seen_or_kept(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.backend_chat.reply = AsyncMock(return_value="I didn't look at that.")
            app.live.note_signal_exchange = AsyncMock()
            with patch("app_areas.web_interface.read_photo", return_value=(b"scaled", WITHHELD_IMAGE)):
                await app.answer_on_web(IPHONE, "", lambda text: None, image=b"original")
            content = app.backend_chat.reply.await_args.kwargs["content"]
            self.assertNotIn("input_image", [part["type"] for part in content])
            self.assertEqual(app.kept_images.images, {})

    async def test_look_again_is_hers_in_her_own_chats_only_and_photos_end_with_the_conversation(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.assertIn("look_again", app.tools.names())
            from app_areas.signal_chats import CLIENT_CHAT_TOOLS
            self.assertNotIn("look_again", CLIENT_CHAT_TOOLS)
            app.kept_images.keep(b"photo")
            app.standby = True
            app.conversation_persistence.end_conversation = AsyncMock(return_value=True)
            with patch.object(app, "process_finished_conversations"):
                await app.end_standby_conversation().wait()
            self.assertEqual(app.kept_images.images, {})


class PicturesOnThePageTests(unittest.IsolatedAsyncioTestCase):
    """WB5b: the person's photos on every device, and her pictures shown on the page."""

    def test_her_pictures_get_their_own_ids_with_caption_and_page(self):
        pictures = KeptImages(prefix="pic")
        picture_id = pictures.keep(b"jpeg", "The cafe", "https://cafe.example/")
        self.assertEqual(picture_id, "pic1")
        self.assertEqual((pictures.get("pic1").caption, pictures.get("pic1").page_url),
                         ("The cafe", "https://cafe.example/"))

    def test_the_conversation_tells_listeners_of_photos_and_pictures(self):
        from ui.conversation import Conversation
        recorded, heard, photos, pictures = [], [], [], []
        conversation = Conversation(MagicMock(), MagicMock(), MagicMock(),
                                    record_message=lambda *message: recorded.append(message))
        conversation.listeners.append(lambda *message: heard.append(message))
        conversation.photo_listeners.append(photos.append)
        conversation.picture_listeners.append(lambda *picture: pictures.append(picture))
        conversation.show_photo_note(photo_described("img4", "A fern."))
        self.assertEqual(photos, ["img4"])
        image = Image.new("RGB", (8, 8))
        conversation.show_image(image, "The cafe", "https://cafe.example/")
        # Saved as text as always; the page gets the picture itself instead of that text.
        self.assertEqual(recorded, [("assistant", "[image] The cafe (https://cafe.example/)", "screen")])
        self.assertEqual(heard, [])
        self.assertEqual(pictures, [(image, "The cafe", "https://cafe.example/")])

    async def test_photos_and_pictures_reach_the_feed_and_are_served_from_memory(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.kept_images.keep(b"your-photo")
            app.conversation.show_photo_note(photo_described("img1", "A fern."))
            photo = app.web_feed.items[-1]
            self.assertEqual((photo.kind, photo.role, photo.data["id"]), ("photo", "user", "img1"))
            app.conversation.show_image(Image.new("RGB", (3000, 1500), "teal"), "The cafe", "https://cafe.example/")
            picture = app.web_feed.items[-1]
            self.assertEqual((picture.kind, picture.role), ("picture", "assistant"))
            self.assertEqual(picture.data, {"id": "pic1", "caption": "The cafe", "url": "https://cafe.example/"})
            served = app.web_photo("pic1")
            self.assertLessEqual(max(Image.open(io.BytesIO(served)).size), 1600)
            self.assertEqual(app.web_photo("img1"), b"your-photo")
            self.assertIsNone(app.web_photo("img9"))
            # Both go when the conversation ends.
            app.standby = True
            app.conversation_persistence.end_conversation = AsyncMock(return_value=True)
            with patch.object(app, "process_finished_conversations"):
                await app.end_standby_conversation().wait()
            self.assertIsNone(app.web_photo("pic1"))
            self.assertIsNone(app.web_photo("img1"))

    async def test_web_chats_can_show_pictures_but_not_look_at_the_desks_screen(self):
        from backend_chat import WEB_EXCLUDED
        from session_config import WEB_CHAT_INSTRUCTIONS
        self.assertNotIn("show_web_image", WEB_EXCLUDED)
        self.assertIn("look_at_screen", WEB_EXCLUDED)
        self.assertIn("show_web_image", WEB_CHAT_INSTRUCTIONS)


class PhotoEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async def find_device(hashed):
            return IPHONE if hashed == token_hash(TOKEN) else None

        kept = {"img1": b"jpeg-bytes"}
        access = WebAccess(Pairing(), AsyncMock(), find_device, photo=kept.get)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(web_routes(access))),
                                        base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_only_a_paired_device_gets_a_kept_photo_and_none_once_its_gone(self):
        self.assertEqual((await self.client.get("/api/photos/img1")).status_code, 401)
        paired = {"Cookie": f"{COOKIE}={TOKEN}"}
        answer = await self.client.get("/api/photos/img1", headers=paired)
        self.assertEqual((answer.status_code, answer.content), (200, b"jpeg-bytes"))
        self.assertEqual(answer.headers["content-type"], "image/jpeg")
        self.assertEqual(answer.headers["cache-control"], "private, no-store")
        self.assertEqual((await self.client.get("/api/photos/img2", headers=paired)).status_code, 404)
