"""Talking with Cora over her Signal account: receiving, answering and the shared chat. No real Signal."""

import asyncio
from datetime import datetime, timedelta, timezone
import io
from types import SimpleNamespace
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx2
from PIL import Image

import app_areas.signal_chats
from backend_chat import SIGNAL_EXCLUDED
from greeting import signal_hook
from messaging.container import ATTACHMENTS, delete_attachments
from messaging.images import IMAGE_MAX_BYTES, IMAGES_PER_MESSAGE, read_images, readable_images
from messaging.messages import SIGNAL_SORRY, image_seen_content, signal_content, signal_message_text
from messaging.receiver import WITHHELD_IMAGE, Attachment, IncomingMessage, SignalReceiver, parse_event
from tools.look_at_screen import PASTE_QUESTION, SIGNAL_IMAGE_INSTRUCTIONS
from messaging.setup import LINKED, OFF, SignalStatus
from messaging.signal_cli import SignalClient
from session_config import SIGNAL_CHAT_INSTRUCTIONS, stranger_reply
from storage.client_store import Client
from storage.signal_settings_store import SignalSettings
import test_keyboard_persistence as keyboard_tests

CORA = "+61400000001"
NICK = "+61400000002"
NOW = datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc)


def event(message=None, source=NICK, wrapped=True, **envelope):
    body = {"envelope": {"sourceNumber": source, "timestamp": int(NOW.timestamp() * 1000), **envelope}}
    if message is not None:
        body["envelope"]["dataMessage"] = message
    if wrapped:
        body = {"jsonrpc": "2.0", "method": "receive", "params": {**body, "account": CORA}}
    return json.dumps(body)


def incoming(text="Hi", sent_at=None, sender=NICK, attachments=(), images_seen=()):
    return IncomingMessage(sender, text, sent_at or datetime.now(timezone.utc), attachments, images_seen=images_seen)


def jpeg_bytes(size=(64, 48)):
    buffer = io.BytesIO()
    Image.new("RGB", size, "teal").save(buffer, format="JPEG")
    return buffer.getvalue()


PHOTO = Attachment("abc123.jpg", "image/jpeg", 1000)
PDF = Attachment("def456.pdf", "application/pdf", 1000)


class ParseTests(unittest.TestCase):
    def test_a_direct_text_message_wrapped_or_not(self):
        for wrapped in (True, False):
            with self.subTest(wrapped=wrapped):
                self.assertEqual(parse_event(event({"message": " Hi Cora "}, wrapped=wrapped)), incoming("Hi Cora", NOW))

    def test_everything_else_is_skipped(self):
        cases = {
            "receipt": event(receiptMessage={"isDelivery": True}),
            "typing": event(typingMessage={"action": "STARTED"}),
            "group": event({"message": "hi all", "groupInfo": {"groupId": "g"}}),
            "group v2": event({"message": "hi all", "groupV2": {"id": "g"}}),
            "reaction only": event({"reaction": {"emoji": "👍"}}),
            "her own phone": event(syncMessage={"sentMessage": {"message": "from Cora's phone"}}),
            "no sender": event({"message": "hi"}, source=None),
            "not json": "{nope",
            "not an object": "[]",
        }
        for name, data in cases.items():
            with self.subTest(name):
                self.assertIsNone(parse_event(data))

    def test_attachments_are_parsed_and_unread_ones_noted(self):
        message = parse_event(event({"message": "", "attachments": [
            {"id": "abc123.jpg", "contentType": "image/JPEG", "size": 1000}, {"id": "b"}, "junk",
        ]}))
        self.assertEqual(message.text, "")
        self.assertEqual(message.attachments, (PHOTO, Attachment("b")))
        self.assertEqual(signal_message_text(message), "[2 attachments, not opened]")
        self.assertEqual(signal_message_text(incoming("Look", attachments=(PDF,))), "Look [1 attachment, not opened]")

    def test_images_read_are_marked_like_pasted_ones_never_described(self):
        self.assertEqual(signal_message_text(incoming("Look", attachments=(PHOTO,), images_seen=("A teal square",))), "[image] Look")
        self.assertEqual(signal_message_text(incoming("", attachments=(PHOTO, PHOTO), images_seen=("one", "two"))), "[2 images]")
        self.assertEqual(signal_message_text(incoming("Look", attachments=(PHOTO, PDF), images_seen=("one",))),
                         "[image] Look [1 attachment, not opened]")
        self.assertEqual(signal_message_text(incoming("Hey", attachments=(PHOTO, PHOTO), images_seen=("one", WITHHELD_IMAGE))),
                         "[image] [image withheld] Hey")


class ContentTests(unittest.TestCase):
    def test_late_messages_say_when_they_were_sent(self):
        content = signal_content([
            incoming("Are you there?", sent_at=NOW - timedelta(minutes=45)),
            incoming("Never mind, found it", sent_at=NOW - timedelta(seconds=30)),
        ], NOW)
        self.assertRegex(content[0]["text"], r"^\[sent \w{3} \d\d:\d\d, 45 minutes ago\] Are you there\?$")
        self.assertEqual(content[1], {"type": "input_text", "text": "Never mind, found it"})
        overnight = signal_content([incoming("Morning", sent_at=NOW - timedelta(hours=9))], NOW)
        self.assertIn("9 hours ago", overnight[0]["text"])

    def test_what_was_seen_follows_its_message_as_untrusted_text(self):
        content = signal_content([incoming("What's this?", NOW, attachments=(PHOTO,), images_seen=("A teal square",))], NOW)
        self.assertEqual(content[0], {"type": "input_text", "text": "[image] What's this?"})
        self.assertEqual(content[1], image_seen_content("A teal square"))
        self.assertIn("untrusted data", content[1]["text"])
        self.assertTrue(content[1]["text"].endswith("A teal square"))

    def test_a_withheld_image_is_a_fixed_note_with_nothing_of_the_image(self):
        content = signal_content([incoming("", NOW, attachments=(PHOTO,), images_seen=(WITHHELD_IMAGE,))], NOW)
        self.assertEqual(content[0], {"type": "input_text", "text": "[image withheld]"})
        self.assertIn("not described to you", content[1]["text"])
        self.assertNotIn(WITHHELD_IMAGE, content[1]["text"])


class ImageReadingTests(unittest.IsolatedAsyncioTestCase):
    async def test_images_are_scaled_and_read_with_the_message_as_the_question(self):
        fetched, looks = [], []

        async def fetch(attachment_id):
            fetched.append(attachment_id)
            return jpeg_bytes((4000, 3000))

        def fake_look(question, image, *, mime, instructions):
            looks.append((question, Image.open(io.BytesIO(image)).size, mime, instructions))
            return "A teal square"

        with patch("messaging.images.look", fake_look):
            seen, failed = await read_images(incoming("What's this?", attachments=(PHOTO, PDF)), fetch)
            self.assertEqual((seen, failed), (("A teal square",), 0))
            self.assertEqual(fetched, ["abc123.jpg"])  # the PDF is never fetched
            self.assertEqual(looks, [("What's this?", (2048, 1536), "image/jpeg", SIGNAL_IMAGE_INSTRUCTIONS)])
            await read_images(incoming("", attachments=(PHOTO,)), fetch)
            self.assertEqual(looks[-1][0], PASTE_QUESTION)

    async def test_too_many_too_big_or_unsupported_images_are_left_unopened(self):
        album = tuple(Attachment(f"p{n}.jpg", "image/jpeg", 10) for n in range(IMAGES_PER_MESSAGE + 2))
        self.assertEqual(len(readable_images(incoming(attachments=album))), IMAGES_PER_MESSAGE)
        huge = Attachment("big.jpg", "image/jpeg", IMAGE_MAX_BYTES + 1)
        heic = Attachment("phone.heic", "image/heic", 10)
        self.assertEqual(readable_images(incoming(attachments=(huge, heic, PDF))), [])

    async def test_an_image_the_look_declines_is_withheld_not_described(self):
        async def fetch(attachment_id):
            return jpeg_bytes()

        for reply in ("WITHHELD", " withheld. ", "**WITHHELD**"):
            with patch("messaging.images.look", return_value=reply):
                self.assertEqual(await read_images(incoming(attachments=(PHOTO,)), fetch), ((WITHHELD_IMAGE,), 0))
        with patch("messaging.images.look", return_value="Withheld payment notice, $40 due"):
            self.assertEqual(await read_images(incoming(attachments=(PHOTO,)), fetch), (("Withheld payment notice, $40 due",), 0))
        self.assertIn("WITHHELD", SIGNAL_IMAGE_INSTRUCTIONS)

    async def test_a_failed_read_is_counted_not_raised(self):
        async def fetch(attachment_id):
            return b"not an image"

        with patch("messaging.images.look") as fake_look:
            self.assertEqual(await read_images(incoming(attachments=(PHOTO,)), fetch), ((), 1))
            fake_look.assert_not_called()


class AttachmentCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_attachments_are_fetched_by_id_and_deleted_with_a_safe_rm(self):
        requests = []

        def handle(request):
            requests.append(json.loads(request.content))
            return httpx2.Response(200, json={"jsonrpc": "2.0", "id": "1", "result": {"data": "aGk="}})

        client = SignalClient("http://signal.test", transport=httpx2.MockTransport(handle))
        self.assertEqual(await client.attachment(CORA, "abc123.jpg"), b"hi")
        self.assertEqual((requests[0]["method"], requests[0]["params"]),
                         ("getAttachment", {"account": CORA, "id": "abc123.jpg"}))
        with patch("messaging.container._compose") as compose:
            await delete_attachments(["abc123.jpg", "../../etc/passwd", "x y", ""])
            compose.assert_called_once_with("exec", "-T", "signal", "rm", "-f", "--", f"{ATTACHMENTS}/abc123.jpg")
            compose.reset_mock()
            await delete_attachments(["../nope"])
            compose.assert_not_called()


class SignalClientStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_events_are_split_on_blank_lines_and_sends_name_the_recipient(self):
        requests = []

        def handle(request):
            requests.append(request)
            if request.url.path == "/api/v1/events":
                body = 'event:receive\ndata:{"a": 1}\n\n:keep-alive\n\nevent:receive\ndata:{"b":\ndata: 2}\n\n'
                return httpx2.Response(200, text=body, headers={"content-type": "text/event-stream"})
            return httpx2.Response(200, json={"jsonrpc": "2.0", "id": "1", "result": {}})

        client = SignalClient("http://signal.test", transport=httpx2.MockTransport(handle))
        received = [data async for data in client.events(CORA)]
        self.assertEqual(received, ['{"a": 1}', '{"b":\n2}'])
        self.assertEqual(requests[0].url.params["account"], CORA)
        await client.send(CORA, NICK, "Hello")
        self.assertEqual(json.loads(requests[1].content)["params"],
                         {"account": CORA, "recipient": [NICK], "message": "Hello"})
        await client.send_typing(CORA, NICK)
        self.assertEqual(json.loads(requests[2].content)["method"], "sendTyping")


class ReceiverTests(unittest.IsolatedAsyncioTestCase):
    async def test_messages_are_passed_on_and_a_dropped_stream_reconnects(self):
        streams = [[event({"message": "one"}), event(receiptMessage={})], ConnectionError("down"), [event({"message": "two"})]]
        connected = []

        class Client:
            async def events(self, account, on_open=lambda: None):
                connected.append(account)
                on_open()
                stream = streams.pop(0) if streams else None
                if stream is None:
                    await asyncio.Event().wait()
                if isinstance(stream, Exception):
                    raise stream
                for data in stream:
                    yield data

        received, activity = [], []
        receiver = SignalReceiver(Client(), CORA, received.append, lambda m, s: activity.append(s),
                                  retry_seconds=(0.01,))
        task = asyncio.ensure_future(receiver.run())
        for _ in range(100):
            if len(received) == 2:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        self.assertEqual([m.text for m in received], ["one", "two"])
        self.assertEqual(activity, ["Signal reconnecting"])
        self.assertIsNotNone(receiver.connected_at)
        self.assertTrue(all(account == CORA for account in connected))


class SignalAppTests(unittest.IsolatedAsyncioTestCase):
    async def linked_app(self, app, reply="On my way."):
        app.signal.status = SignalStatus(LINKED, account=CORA)
        app.signal_settings = SignalSettings(True, CORA, NICK)
        app.signal.client = MagicMock()
        app.signal.client.send = AsyncMock()
        app.signal.client.send_typing = AsyncMock()
        app.backend_chat.reply = AsyncMock(return_value=reply)
        app.live.note_signal_exchange = AsyncMock()

    async def settle(self, app, pilot):
        for _ in range(3):
            await pilot.pause()
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)

    async def test_your_message_joins_the_chat_and_is_answered_over_signal(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.linked_app(app)
            app.conversation.log_entries.append("Cora: Earlier, by voice")
            app.confirming_tools = frozenset({"github__create_issue"})
            records = []
            app.conversation.record_message = lambda role, text, channel: records.append((role, text, channel))
            app.on_signal_message(incoming("Where are my keys?"))
            await self.settle(app, pilot)
            kwargs = app.backend_chat.reply.await_args.kwargs
            self.assertEqual(kwargs["content"], [{"type": "input_text", "text": "Where are my keys?"}])
            # One conversation with the desktop: earlier voice is history, and the new message isn't repeated.
            self.assertEqual([h["content"][0]["text"] for h in kwargs["history"]], ["Earlier, by voice"])
            self.assertEqual(kwargs["extra_instructions"], SIGNAL_CHAT_INSTRUCTIONS)
            # Tools that change things are offered: the person approves them in this chat.
            self.assertEqual(kwargs["route"].excluded, SIGNAL_EXCLUDED)
            self.assertIsNotNone(kwargs["route"].ask_approval)
            app.signal.client.send.assert_awaited_once_with(CORA, NICK, "On my way.")
            app.signal.client.send_typing.assert_awaited_once_with(CORA, NICK)
            self.assertIn("You · Signal: Where are my keys?", app.conversation.log_entries)
            self.assertIn("Cora · Signal:", app.conversation.log_entries)
            self.assertEqual(records, [("user", "Where are my keys?", "signal"), ("assistant", "On my way.", "signal")])
            app.live.note_signal_exchange.assert_awaited_once_with("Where are my keys?", "On my way.")
            history = app.conversation.get_reconnect_history()
            self.assertEqual([(h["role"], h["content"][0]["text"]) for h in history][-2:],
                             [("user", "Where are my keys?"), ("assistant", "On my way.")])

    async def test_messages_arriving_while_she_answers_are_answered_together(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.linked_app(app)
            release = asyncio.Event()
            replies = iter(["First answer", "Both answered"])

            async def reply(**kwargs):
                await release.wait()
                return next(replies)

            app.backend_chat.reply = AsyncMock(side_effect=reply)
            app.on_signal_message(incoming("one"))
            await pilot.pause()
            app.on_signal_message(incoming("two"))
            app.on_signal_message(incoming("three"))
            release.set()
            await self.settle(app, pilot)
            batches = [[part["text"] for part in call.kwargs["content"]] for call in app.backend_chat.reply.await_args_list]
            self.assertEqual(batches, [["one"], ["two", "three"]])
            self.assertEqual(app.signal.client.send.await_count, 2)

    async def test_a_number_nobody_added_gets_one_polite_reply_and_is_never_shown(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.linked_app(app)
            app.live.person_name = "Nick"
            for _ in range(2):
                app.on_signal_message(incoming("Hi, who's this?", sender="+61499999999"))
                await self.settle(app, pilot)
            app.backend_chat.reply.assert_not_called()
            app.signal.client.send.assert_awaited_once_with(CORA, "+61499999999", stranger_reply("Nick"))
            self.assertFalse(any("who's this" in str(entry) for entry in app.conversation.log_entries))
            app.persistence.save_client_message.assert_not_called()

    async def test_a_failed_reply_sends_a_short_apology_and_says_why_in_activity(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.linked_app(app)
            app.backend_chat.reply = AsyncMock(side_effect=TimeoutError("private words"))
            app.on_signal_message(incoming("Hello?"))
            await self.settle(app, pilot)
            app.signal.client.send.assert_awaited_once_with(CORA, NICK, SIGNAL_SORRY)
            entries = "\n".join(app.activity.entries)
            self.assertIn("Signal reply failed (TimeoutError)", entries)
            self.assertNotIn("private words", entries)

    async def test_your_image_is_read_for_her_marked_in_the_chat_and_deleted(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.linked_app(app)
            app.signal.client.attachment = AsyncMock(return_value=jpeg_bytes())
            records = []
            app.conversation.record_message = lambda role, text, channel: records.append((role, text, channel))
            with patch("messaging.images.look", return_value="A receipt for $42") as fake_look:
                app.on_signal_message(incoming("Can I claim this?", attachments=(PHOTO, PDF)))
                await self.settle(app, pilot)
            self.assertEqual(fake_look.call_args.args[0], "Can I claim this?")
            app.signal.client.attachment.assert_awaited_once_with(CORA, "abc123.jpg")
            content = app.backend_chat.reply.await_args.kwargs["content"]
            self.assertEqual(content, [
                {"type": "input_text", "text": "[image] Can I claim this? [1 attachment, not opened]"},
                image_seen_content("A receipt for $42"),
            ])
            # Saved and shown only as marked, like a pasted image; the description isn't kept.
            self.assertEqual(records[0], ("user", "[image] Can I claim this? [1 attachment, not opened]", "signal"))
            self.assertFalse(any("$42" in str(entry) for entry in app.conversation.log_entries))
            app_areas.signal_chats.delete_attachments.assert_awaited_once_with(["abc123.jpg", "def456.pdf"])
            self.assertTrue(any(entry.endswith("Read 1 Signal image(s) for Cora.") for entry in app.activity.entries))

    async def test_a_withheld_image_reaches_her_only_as_a_note_and_is_deleted(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.linked_app(app)
            app.signal.client.attachment = AsyncMock(return_value=jpeg_bytes())
            records = []
            app.conversation.record_message = lambda role, text, channel: records.append((role, text, channel))
            with patch("messaging.images.look", return_value="WITHHELD"):
                app.on_signal_message(incoming("hey", attachments=(PHOTO,)))
                await self.settle(app, pilot)
            content = app.backend_chat.reply.await_args.kwargs["content"]
            self.assertEqual(content, [
                {"type": "input_text", "text": "[image withheld] hey"},
                image_seen_content(WITHHELD_IMAGE),
            ])
            self.assertEqual(records[0], ("user", "[image withheld] hey", "signal"))
            app_areas.signal_chats.delete_attachments.assert_awaited_once_with(["abc123.jpg"])
            entries = app.activity.entries
            self.assertTrue(any(entry.endswith("Withheld 1 explicit, gory or abusive Signal image(s) from Cora.") for entry in entries))
            self.assertFalse(any("Read 1 Signal image" in entry for entry in entries))

    async def test_an_image_that_cant_be_read_is_answered_as_not_opened_and_still_deleted(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.linked_app(app)
            app.signal.client.attachment = AsyncMock(side_effect=RuntimeError("private"))
            app.on_signal_message(incoming("Look", attachments=(PHOTO,)))
            await self.settle(app, pilot)
            content = app.backend_chat.reply.await_args.kwargs["content"]
            self.assertEqual(content, [{"type": "input_text", "text": "Look [1 attachment, not opened]"}])
            app_areas.signal_chats.delete_attachments.assert_awaited_once_with(["abc123.jpg"])
            self.assertNotIn("private", "\n".join(app.activity.entries))

    async def test_a_strangers_attachment_is_deleted_unopened(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.linked_app(app)
            app.signal.client.attachment = AsyncMock()
            app.on_signal_message(incoming("Look!", sender="+61499999999", attachments=(PHOTO,)))
            await self.settle(app, pilot)
            app.signal.client.attachment.assert_not_awaited()
            app.backend_chat.reply.assert_not_called()
            app_areas.signal_chats.delete_attachments.assert_awaited_once_with(["abc123.jpg"])

    async def test_leftover_attachments_are_cleared_before_listening(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.linked_app(app)
            app.learn_signal_ids = AsyncMock()
            app.sync_signal_receiver = MagicMock()
            await app.listen_on_signal()
            app_areas.signal_chats.clear_attachments.assert_awaited_once()
            app.sync_signal_receiver.assert_called_once()

    async def test_listening_follows_whether_her_account_is_linked(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.signal.client = MagicMock()

            async def events(account):
                await asyncio.Event().wait()
                yield ""

            app.signal.client.events = events
            app.sync_signal_receiver()
            self.assertIsNone(app.signal_receiver)
            app.signal.status = SignalStatus(LINKED, account=CORA)
            app.sync_signal_receiver()
            worker = app.signal_receiver
            self.assertFalse(worker.is_finished)
            self.assertEqual(app.signal_listening_as, CORA)
            app.sync_signal_receiver()
            self.assertIs(app.signal_receiver, worker)
            app.signal.status = SignalStatus(OFF)
            app.sync_signal_receiver()
            await pilot.pause()
            self.assertIsNone(app.signal_receiver)
            self.assertTrue(worker.is_cancelled)


class LaunchGreetingTests(unittest.IsolatedAsyncioTestCase):
    def test_the_signal_hook_quotes_the_exchange_and_asks_her_to_follow_on(self):
        hook = signal_hook([("Where are my keys?", "By the door, I think.")])
        self.assertIn("They wrote: Where are my keys?", hook)
        self.assertIn("You replied: By the door, I think.", hook)
        self.assertIn("follow on from that", hook)
        self.assertIn("don't repeat your reply", hook)
        long = signal_hook([("x" * 3000, "y")])
        self.assertLess(len(long), 2000)

    async def test_catching_up_until_listening_quietly_with_nothing_left_to_answer(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.signal_settings = SignalSettings(False)
            self.assertFalse(app.signal_catching_up())
            app.signal_settings = SignalSettings(True, CORA, NICK)
            app.signal.status = SignalStatus(OFF)
            self.assertFalse(app.signal_catching_up())
            app.signal.status = SignalStatus("starting")
            self.assertTrue(app.signal_catching_up())
            app.signal.status = SignalStatus(LINKED, account=CORA)
            self.assertTrue(app.signal_catching_up())  # not listening yet
            now = asyncio.get_running_loop().time()
            app.signal_listener = SimpleNamespace(connected_at=now)
            self.assertTrue(app.signal_catching_up())  # just connected: waiting messages may still come
            app.signal_listener.connected_at = now - 10
            self.assertFalse(app.signal_catching_up())
            app.signal_last_message_at = now
            self.assertTrue(app.signal_catching_up())
            app.signal_last_message_at = now - 10
            app.signal_waiting = {NICK: [incoming("one")]}
            self.assertTrue(app.signal_catching_up())
            app.signal_waiting = {}
            app.signal_startup = SimpleNamespace(is_finished=False)
            self.assertTrue(app.signal_catching_up())

    async def test_messages_answered_at_launch_become_the_greetings_hook(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await SignalAppTests.linked_app(self, app, reply="By the door.")
            app.pending_greeting = "launch"
            app.on_signal_message(incoming("Where are my keys?"))
            await SignalAppTests.settle(self, app, pilot)
            self.assertEqual(app.signal_launch_exchanges, [("Where are my keys?", "By the door.")])
            app.signal_catching_up = lambda: False
            app.conversation_persistence.load_relationship_values = AsyncMock(return_value=None)
            app.choose_launch_hook = AsyncMock()
            app.live.send_greeting = AsyncMock()
            await app.send_greeting("launch").wait()
            app.choose_launch_hook.assert_not_called()
            brief = app.live.send_greeting.await_args.args[0]
            self.assertIn("They wrote: Where are my keys?", brief)

    async def test_after_the_greeting_signal_replies_are_not_saved_for_it(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await SignalAppTests.linked_app(self, app)
            app.pending_greeting = None
            app.on_signal_message(incoming("Later message"))
            await SignalAppTests.settle(self, app, pilot)
            self.assertEqual(app.signal_launch_exchanges, [])


class RegisteredTests(unittest.IsolatedAsyncioTestCase):
    async def test_is_registered_reads_signals_answer(self):
        replies = iter([
            [{"recipient": NICK, "isRegistered": True}], [{"recipient": NICK, "isRegistered": False, "uuid": None}], [],
        ])
        requests = []

        def handle(request):
            requests.append(json.loads(request.content))
            return httpx2.Response(200, json={"jsonrpc": "2.0", "id": "1", "result": next(replies)})

        client = SignalClient("http://signal.test", transport=httpx2.MockTransport(handle))
        self.assertTrue(await client.is_registered(CORA, NICK))
        self.assertFalse(await client.is_registered(CORA, NICK))
        self.assertFalse(await client.is_registered(CORA, NICK))
        self.assertEqual(requests[0]["method"], "getUserStatus")
        self.assertEqual(requests[0]["params"], {"account": CORA, "recipient": [NICK]})


class FirstMessageTests(unittest.IsolatedAsyncioTestCase):
    def test_a_first_message_with_only_a_signal_id_is_kept(self):
        message = parse_event(event({"message": "Hi Cora"}, source=None, sourceUuid="uuid-sarah"))
        self.assertEqual((message.sender, message.sender_id, message.text), ("", "uuid-sarah", "Hi Cora"))
        self.assertIsNone(parse_event(event({"message": "Hi"}, source=None)))

    async def test_signal_ids_and_accepting_requests_use_signal_clis_calls(self):
        requests = []

        def handle(request):
            body = json.loads(request.content)
            requests.append(body)
            result = [{"isRegistered": True, "uuid": "uuid-nick"}] if body["method"] == "getUserStatus" else {}
            return httpx2.Response(200, json={"jsonrpc": "2.0", "id": "1", "result": result})

        client = SignalClient("http://signal.test", transport=httpx2.MockTransport(handle))
        self.assertEqual(await client.signal_id(CORA, NICK), "uuid-nick")
        await client.accept_request(CORA, NICK)
        self.assertEqual(requests[1], {"jsonrpc": "2.0", "id": requests[1]["id"], "method": "sendMessageRequestResponse",
                                       "params": {"account": CORA, "recipient": [NICK], "type": "accept"}})

    async def test_a_known_persons_first_message_is_recognised_answered_and_accepted(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await SignalAppTests.linked_app(self, app, reply="Hello!")
            app.signal.client.accept_request = AsyncMock()
            app.signal.client.signal_id = AsyncMock(return_value="uuid-nick")
            await app.learn_signal_ids()
            self.assertEqual(app.signal_ids, {"uuid-nick": NICK})
            app.on_signal_message(IncomingMessage("", "First hello", datetime.now(timezone.utc), (), "uuid-nick"))
            await SignalAppTests.settle(self, app, pilot)
            app.signal.client.accept_request.assert_awaited_once_with(CORA, NICK)
            # Replies go to the saved Signal ID, which keeps working if the number is hidden later.
            app.signal.client.send.assert_awaited_once_with(CORA, "uuid-nick", "Hello!")
            app.persistence.save_signal_person_id.assert_called_once_with("uuid-nick")
            self.assertIn("You · Signal: First hello", app.conversation.log_entries)

    async def test_an_unknown_id_gets_the_stranger_reply_by_id_and_no_acceptance(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await SignalAppTests.linked_app(self, app)
            app.signal.client.accept_request = AsyncMock()
            app.on_signal_message(IncomingMessage("", "Hi?", datetime.now(timezone.utc), (), "uuid-stranger"))
            await SignalAppTests.settle(self, app, pilot)
            app.signal.client.send.assert_awaited_once_with(CORA, "uuid-stranger", stranger_reply(None))
            app.signal.client.accept_request.assert_not_called()
            app.backend_chat.reply.assert_not_called()


class SavedSignalIdTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_ids_are_used_without_looking_anyone_up(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await SignalAppTests.linked_app(self, app)
            app.signal_settings = SignalSettings(True, CORA, NICK, "uuid-nick")
            app.signal_clients = {"+61400000003": Client(uuid4(), "Karene", "+61400000003", "uuid-karene")}
            app.signal.client.signal_id = AsyncMock()
            await app.learn_signal_ids()
            app.signal.client.signal_id.assert_not_called()
            self.assertEqual(app.signal_ids, {"uuid-nick": NICK, "uuid-karene": "+61400000003"})

    async def test_a_hidden_number_is_still_recognised_and_answered_by_id(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await SignalAppTests.linked_app(self, app, reply="Hi Karene.")
            karene = Client(uuid4(), "Karene", "+61400000003", "uuid-karene")
            app.signal_clients = {karene.number: karene}
            app.signal.client.accept_request = AsyncMock()
            app.signal.client.signal_id = AsyncMock(return_value=None)  # she now hides her number
            app.persistence.client_conversation.return_value = uuid4()
            app.persistence.load_client_history.return_value = []
            await app.learn_signal_ids()
            app.on_signal_message(IncomingMessage("", "Hello", datetime.now(timezone.utc), (), "uuid-karene"))
            await SignalAppTests.settle(self, app, pilot)
            app.signal.client.send.assert_awaited_once_with(CORA, "uuid-karene", "Hi Karene.")

    async def test_a_message_with_number_and_id_saves_the_id(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await SignalAppTests.linked_app(self, app)
            karene = Client(uuid4(), "Karene", "+61400000003")
            app.signal_clients = {karene.number: karene}
            app.persistence.client_conversation.return_value = uuid4()
            app.persistence.load_client_history.return_value = []
            app.on_signal_message(IncomingMessage(karene.number, "Hi", datetime.now(timezone.utc), (), "uuid-karene"))
            await SignalAppTests.settle(self, app, pilot)
            app.persistence.save_client_signal_id.assert_called_once_with(karene.id, "uuid-karene")
            self.assertEqual(app.signal_clients[karene.number].signal_id, "uuid-karene")
            self.assertEqual(app.signal_ids["uuid-karene"], karene.number)
