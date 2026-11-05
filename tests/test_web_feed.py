"""The live conversation on the web page (WB3): the feed, the events stream, and what reaches it."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx2 as httpx
import psycopg
from psycopg import sql
from sqlalchemy.orm import Session

from migrate import apply_migrations, database_settings
from server.listener import Listener, build_app
from storage.database import build_engine
from storage.models import ConversationRecord, MessageRecord, PersonRecord
from storage.recent_messages import recent_person_messages
from storage.web_device_store import WebDevice
from ui.conversation import Conversation
from web import routes
from web.feed import APPROVAL, APPROVAL_DONE, BACKLOG, FIRST_LOAD, MESSAGE, MessageFeed
from web.pairing import Pairing, token_hash
from web.rendering import chat_html
from web.routes import COOKIE, WebAccess, web_routes
import test_keyboard_persistence as keyboard_tests

TOKEN = "a-paired-devices-token"
EARLIER = datetime(2026, 11, 3, 9, tzinfo=timezone.utc)
IPHONE = WebDevice(uuid4(), "iPhone", EARLIER, EARLIER)


class FeedTests(unittest.TestCase):
    def test_a_first_load_gets_the_recent_messages_and_starts_fresh(self):
        feed = MessageFeed()
        for number in range(FIRST_LOAD + 5):
            feed.publish("user", f"message {number}", "voice")
        fresh, messages = feed.since(None)
        self.assertTrue(fresh)
        self.assertEqual(len(messages), FIRST_LOAD)
        self.assertEqual(messages[-1].text, f"message {FIRST_LOAD + 4}")

    def test_a_reconnecting_page_gets_only_what_it_missed(self):
        feed = MessageFeed()
        first = feed.publish("user", "one", "web")
        feed.publish("assistant", "two", "web")
        fresh, missed = feed.since(feed.event_id(first))
        self.assertFalse(fresh)
        self.assertEqual([message.text for message in missed], ["two"])
        self.assertEqual(feed.since(feed.event_id(missed[-1])), (False, []))

    def test_a_page_from_an_earlier_run_or_too_far_behind_starts_afresh(self):
        feed = MessageFeed(keep=3)
        old = feed.publish("user", "old", "voice")
        self.assertTrue(feed.since(f"another-run:{old.seq}")[0])
        for number in range(5):
            feed.publish("user", str(number), "voice")
        # "old" is no longer kept, so what came after it can't all be sent.
        self.assertTrue(feed.since(feed.event_id(old))[0])
        self.assertTrue(feed.since("nonsense")[0])

    def test_earlier_messages_go_first_and_whats_been_said_since_isnt_repeated(self):
        feed = MessageFeed()
        feed.publish("user", "said since start", "keyboard", ref=None)
        feed.publish("assistant", "and her answer", "screen")
        feed.seed([
            ("user", "voice", "from yesterday", EARLIER),
            ("user", "keyboard", "said since start", EARLIER + timedelta(days=1)),
        ])
        self.assertEqual([item.text for item in feed.items], ["from yesterday", "said since start", "and her answer"])
        self.assertEqual([item.seq for item in feed.items], [1, 2, 3])
        # Only once.
        feed.seed([("user", "voice", "again", EARLIER)])
        self.assertEqual(len(feed.items), 3)

    def test_a_page_too_far_behind_is_dropped_to_catch_up(self):
        feed = MessageFeed()
        queue = feed.subscribe()
        for number in range(BACKLOG + 1):
            feed.publish("user", str(number), "voice")
        self.assertNotIn(queue, feed.subscribers)
        self.assertIsNone(list(queue._queue)[-1])


class ConversationListenerTests(unittest.TestCase):
    def test_every_message_said_is_recorded_then_passed_on(self):
        recorded, heard = [], []
        conversation = Conversation(MagicMock(), MagicMock(), MagicMock(),
                                    record_message=lambda *message: recorded.append(message))
        conversation.listeners.append(lambda *message: heard.append(message))
        conversation.show_signal_message("From Signal")
        conversation.show_web_message("From the page", "iPad")
        conversation.show_written_reply("Her reply", label="Cora · web", channel="web")
        # Typed at the desk: saved elsewhere, so only passed on.
        conversation.announce("user", "From the desk", "keyboard")
        # The device is saved only for a web message, so other callers keep three arguments.
        self.assertEqual(recorded, [("user", "From Signal", "signal"), ("user", "From the page", "web", "iPad"),
                                    ("assistant", "Her reply", "web")])
        self.assertEqual(heard, [("user", "From Signal", "signal", None), ("user", "From the page", "web", "iPad"),
                                 ("assistant", "Her reply", "web", None), ("user", "From the desk", "keyboard", None)])

    def test_a_failing_listener_doesnt_stop_the_chat(self):
        conversation = Conversation(MagicMock(), MagicMock(), MagicMock())
        conversation.listeners.append(MagicMock(side_effect=RuntimeError("boom")))
        later = MagicMock()
        conversation.listeners.append(later)
        conversation.show_web_message("Hi")
        later.assert_called_once_with("user", "Hi", "web", None)


class EventsStreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.feed = MessageFeed()
        self.ready = AsyncMock()

        async def find_device(hashed):
            return IPHONE if hashed == token_hash(TOKEN) else None

        access = WebAccess(Pairing(), AsyncMock(), find_device, feed=self.feed, feed_ready=self.ready)
        self.server = Listener(build_app(web_routes(access)), 0)
        self.server.open()
        self.port = self.server.socket.getsockname()[1]
        self.serving = asyncio.create_task(self.server.serve())
        for _ in range(50):
            if self.server.server is not None and self.server.server.started:
                break
            await asyncio.sleep(0.05)
        self.client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{self.port}")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.server.stop()
        await asyncio.wait_for(self.serving, 5)

    async def read_events(self, count, last_event_id=None):
        """The first `count` events (name, id, data) from the stream, then hang up."""
        headers = {"Cookie": f"{COOKIE}={TOKEN}"}
        if last_event_id:
            headers["Last-Event-ID"] = last_event_id
        events, current = [], {}
        async with self.client.stream("GET", "/api/events", headers=headers) as response:
            self.assertEqual(response.headers["content-type"].split(";")[0], "text/event-stream")
            async for text in response.aiter_lines():
                if not text:
                    if "event" in current:
                        events.append((current["event"], current.get("id"), current.get("data")))
                    current = {}
                    if len(events) == count:
                        break
                    continue
                field, _, value = text.partition(": ")
                current[field] = value
        return events

    async def test_only_a_paired_device_gets_the_conversation(self):
        answer = await self.client.get("/api/events")
        self.assertEqual(answer.status_code, 401)

    async def test_a_first_load_resets_then_gets_recent_messages_then_new_ones(self):
        self.feed.publish("user", "Earlier, by voice", "voice")
        self.feed.publish("assistant", "**Yes**", "voice")

        async def later():
            await asyncio.sleep(0.2)
            self.feed.publish("user", "From Signal", "signal", ref=None)

        speaking = asyncio.create_task(later())
        events = await asyncio.wait_for(self.read_events(4), 5)
        await speaking
        self.ready.assert_awaited()
        self.assertEqual([name for name, _, _ in events], ["reset", "message", "message", "message"])
        bodies = [json.loads(data) for _, _, data in events[1:]]
        self.assertEqual(bodies[0]["text"], "Earlier, by voice")
        self.assertEqual(bodies[1]["html"], chat_html("**Yes**"))
        self.assertNotIn("text", bodies[1])
        self.assertEqual((bodies[2]["text"], bodies[2]["channel"]), ("From Signal", "signal"))
        self.assertEqual(events[3][1], f"{self.feed.epoch}:3")

    async def test_reconnecting_with_the_last_id_gets_only_what_was_missed(self):
        first = self.feed.publish("user", "Seen already", "web")
        self.feed.publish("assistant", "Missed while away", "web", ref="page-ref")
        events = await asyncio.wait_for(self.read_events(1, self.feed.event_id(first)), 5)
        self.assertEqual(events[0][0], "message")
        body = json.loads(events[0][2])
        self.assertEqual((body["html"], body["ref"]), (chat_html("Missed while away"), "page-ref"))

    async def test_a_quiet_feed_still_sends_something_so_proxies_keep_it_open(self):
        with patch.object(routes, "KEEP_ALIVE_SECONDS", 0.1):
            headers = {"Cookie": f"{COOKIE}={TOKEN}"}
            seen = []
            async with self.client.stream("GET", "/api/events", headers=headers) as response:
                async for text in response.aiter_lines():
                    seen.append(text)
                    if text.startswith(":"):
                        break
            self.assertIn(": still here", seen)

    async def test_a_page_that_goes_away_stops_listening(self):
        await asyncio.wait_for(self.read_events(1), 5)
        for _ in range(50):
            if not self.feed.subscribers:
                break
            await asyncio.sleep(0.05)
        self.assertEqual(self.feed.subscribers, set())


class WebFeedAppTests(unittest.IsolatedAsyncioTestCase):
    async def test_everything_said_reaches_the_feed_and_the_pages_own_messages_are_marked(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.backend_chat.reply = AsyncMock(return_value="It's at 2pm.")
            app.live.note_signal_exchange = AsyncMock()
            app.conversation.show_signal_message("From Signal")
            await app.answer_on_web(IPHONE, "When's the dentist?", lambda text: None, "page-ref")
            app.conversation.append_transcript("You", "By voice")
            app.conversation.flush_transcript()
            said = [(item.role, item.text, item.channel, item.ref) for item in app.web_feed.items]
            self.assertEqual(said, [
                ("user", "From Signal", "signal", None),
                ("user", "When's the dentist?", "web", "page-ref"),
                ("assistant", "It's at 2pm.", "web", "page-ref"),
                ("user", "By voice", "voice", None),
            ])

    async def test_earlier_messages_are_loaded_once_when_a_page_first_connects(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.persistence.load_recent_person_messages = MagicMock(return_value=[
                ("user", "voice", "Yesterday's message", EARLIER),
            ])
            app.conversation.show_signal_message("Today's message")
            await app.web_feed_ready()
            await app.web_feed_ready()
            app.persistence.load_recent_person_messages.assert_called_once()
            self.assertEqual([item.text for item in app.web_feed.items], ["Yesterday's message", "Today's message"])

    async def test_if_the_earlier_messages_dont_load_pages_still_get_whats_said_from_now(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.persistence.load_recent_person_messages = MagicMock(side_effect=RuntimeError("db down"))
            app.conversation.show_signal_message("Today's message")
            await app.web_feed_ready()
            self.assertTrue(app.web_feed.seeded)
            self.assertEqual([item.text for item in app.web_feed.items], ["Today's message"])


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class RecentMessagesDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        settings = database_settings()
        admin = psycopg.connect(**settings, autocommit=True)
        cls.addClassCleanup(admin.close)
        cls.database = "cora_test_recent_messages_" + uuid4().hex
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.database)))
        cls.addClassCleanup(admin.execute, sql.SQL("DROP DATABASE {}").format(sql.Identifier(cls.database)))
        with psycopg.connect(**{**settings, "dbname": cls.database}, autocommit=True) as connection:
            apply_migrations(connection)
        with patch("storage.database.database_settings", return_value={**settings, "dbname": cls.database}):
            cls.engine = build_engine()
        cls.addClassCleanup(cls.engine.dispose)

    def test_only_the_persons_own_sent_messages_newest_kept_oldest_first(self):
        with Session(self.engine) as session, session.begin():
            client = PersonRecord(kind="client", display_name="Sam", signal_number="+61400000009")
            session.add(client)
            session.flush()
            earlier, today = ConversationRecord(), ConversationRecord()
            theirs = ConversationRecord(person_id=client.id)
            session.add_all([earlier, today, theirs])
            session.flush()
            rows = [
                (earlier, 1, "user", "voice", "first", "not_applicable", 0),
                (earlier, 2, "assistant", "voice", "second", "not_applicable", 1),
                (today, 1, "user", "keyboard", "never sent", "failed", 2),
                (today, 2, "user", "web", "third", "not_applicable", 3, "iPhone"),
                (theirs, 1, "user", "signal", "a client's", "not_applicable", 4),
            ]
            for conversation, sequence, role, channel, text, delivery, minutes, *device in rows:
                session.add(MessageRecord(
                    conversation_id=conversation.id, sequence_number=sequence, role=role, channel=channel,
                    text=text, delivery_status=delivery, created_at=EARLIER + timedelta(minutes=minutes),
                    device=device[0] if device else None,
                ))
        self.assertEqual([row[2] for row in recent_person_messages(self.engine, 10)], ["first", "second", "third"])
        self.assertEqual([row[2] for row in recent_person_messages(self.engine, 2)], ["second", "third"])
        self.assertEqual(recent_person_messages(self.engine, 1)[0],
                         ("user", "web", "third", EARLIER + timedelta(minutes=3), "iPhone"))
        self.assertIsNone(recent_person_messages(self.engine, 10)[0][4])

    def test_the_save_step_keeps_a_web_messages_device_and_leaves_others_without(self):
        from storage.conversation_store import ConversationStore
        store = ConversationStore(self.engine)
        conversation_id = uuid4()
        store.create_conversation(conversation_id)
        web = store.save_message(conversation_id, message_id=uuid4(), role="user", text="From the iPad",
                                 channel="web", device="iPad")
        voice = store.save_message(conversation_id, message_id=uuid4(), role="user", text="By voice", channel="voice")
        self.assertEqual((web.device, voice.device), ("iPad", None))


class ApprovalAndStatusFeedTests(unittest.TestCase):
    def test_approvals_are_kept_in_order_with_messages_and_survive_seeding(self):
        feed = MessageFeed()
        feed.publish("user", "Email them for me", "web")
        feed.publish_approval(APPROVAL, {"id": "a1", "title": "Email · send_email", "detail": "to: x", "seconds": 600})
        feed.publish_approval(APPROVAL_DONE, {"id": "a1", "outcome": "approved"})
        feed.seed([("user", "voice", "Earlier", EARLIER)])
        self.assertEqual([(item.kind, item.seq) for item in feed.items],
                         [(MESSAGE, 1), (MESSAGE, 2), (APPROVAL, 3), (APPROVAL_DONE, 4)])
        self.assertEqual(feed.items[3].data, {"id": "a1", "outcome": "approved"})

    def test_the_desks_state_goes_to_pages_as_it_changes_but_isnt_kept(self):
        feed = MessageFeed()
        queue = feed.subscribe()
        feed.set_status("active")
        feed.set_status("active")
        feed.set_status("standby")
        self.assertEqual([item.state for item in queue._queue], ["active", "standby"])
        self.assertEqual(feed.status, "standby")
        self.assertEqual(len(feed.items), 0)


class DeviceLabelTests(unittest.TestCase):
    def test_the_desks_chat_labels_web_messages_with_their_device_and_cora_still_reads_them(self):
        conversation = Conversation(MagicMock(), MagicMock(), MagicMock())
        conversation.show_web_message("From the iPad", "iPad (Home Screen app)")
        conversation.show_web_message("Device unknown")
        self.assertEqual(conversation.log_entries[-2:], ["You · web · iPad (Home Screen app): From the iPad",
                                                         "You · web: Device unknown"])
        history = [(item["role"], item["content"][0]["text"]) for item in conversation.get_reconnect_history()]
        self.assertEqual(history, [("user", "From the iPad"), ("user", "Device unknown")])

    def test_the_feed_carries_the_device_from_now_and_from_history(self):
        feed = MessageFeed()
        feed.publish("user", "Said now", "web", device="iPhone")
        feed.seed([("user", "web", "Said earlier", EARLIER, "iPad"), ("assistant", "web", "Her reply", EARLIER, None),
                   ("user", "voice", "Old row without one", EARLIER)])
        self.assertEqual([(item.text, item.device) for item in feed.items], [
            ("Said earlier", "iPad"), ("Her reply", None), ("Old row without one", None), ("Said now", "iPhone"),
        ])
        event = json.loads(routes.feed_event(feed, feed.items[-1]).split("data: ", 1)[1])
        self.assertEqual(event["device"], "iPhone")

    def test_a_device_name_never_has_a_colon(self):
        from storage.web_device_store import device_name
        self.assertEqual(device_name("Work: laptop"), "Work laptop")
