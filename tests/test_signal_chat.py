"""Talking with Cora over her Signal account: receiving, answering and the shared chat. No real Signal."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import AsyncMock, MagicMock

import httpx2

from backend_chat import SIGNAL_EXCLUDED
from messaging.messages import SIGNAL_SORRY, signal_content, signal_message_text
from messaging.receiver import IncomingMessage, SignalReceiver, parse_event
from messaging.setup import LINKED, OFF, SignalStatus
from messaging.signal_cli import SignalClient
from session_config import SIGNAL_CHAT_INSTRUCTIONS
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


def incoming(text="Hi", sent_at=None, sender=NICK, attachments=0):
    return IncomingMessage(sender, text, sent_at or datetime.now(timezone.utc), attachments)


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

    def test_attachments_are_counted_not_read(self):
        message = parse_event(event({"message": "", "attachments": [{"id": "a"}, {"id": "b"}]}))
        self.assertEqual((message.text, message.attachments), ("", 2))
        self.assertEqual(signal_message_text(message), "[2 attachments, not opened]")
        self.assertEqual(signal_message_text(incoming("Look", attachments=1)), "Look [1 attachment, not opened]")


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
            async def events(self, account):
                connected.append(account)
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
            self.assertEqual(kwargs["route"].excluded, SIGNAL_EXCLUDED | {"github__create_issue"})
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

    async def test_anyone_else_is_ignored_and_never_shown(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.linked_app(app)
            app.on_signal_message(incoming("Hi, who's this?", sender="+61499999999"))
            await self.settle(app, pilot)
            app.backend_chat.reply.assert_not_called()
            app.signal.client.send.assert_not_called()
            self.assertFalse(any("who's this" in str(entry) for entry in app.conversation.log_entries))
            self.assertIn("A Signal message from someone else was ignored.", "\n".join(app.activity.entries))

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
