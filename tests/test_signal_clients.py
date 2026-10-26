"""People added in Settings talk with Cora as clients, in their own conversations. No real Signal or models."""

import asyncio
from datetime import datetime, timedelta, timezone
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import psycopg
from psycopg import sql
from sqlalchemy import text, update
from sqlalchemy.orm import Session
from textual.widgets import Button, Input, Static, TabbedContent

from messaging.receiver import IncomingMessage
from messaging.setup import LINKED, SignalStatus
from migrate import apply_migrations, database_settings
from session_config import BACKEND_MODEL, MODEL_AREAS, client_chat_instructions, stranger_reply
from storage.client_store import CONVERSATION_GAP, Client, ClientStore
from storage.conversation_store import ConversationStore
from storage.database import build_engine
from storage.models import ConversationRecord, MessageRecord
from storage.people_store import PeopleStore
from storage.signal_settings_store import SignalSettings
from subconscious.relationship.appraisal_store import AppraisalStore
from ui.model_settings import ModelSettingsScreen
import test_keyboard_persistence as keyboard_tests

CORA = "+61400000001"
NICK = "+61400000002"
SARAH = "+61400000003"


def incoming(text, sender=SARAH):
    return IncomingMessage(sender, text, datetime.now(timezone.utc))


class ClientPromptTests(unittest.TestCase):
    def test_professional_mode_names_both_and_keeps_the_persons_life_out(self):
        prompt = client_chat_instructions("Sarah", "Nick")
        self.assertIn('with "Sarah", someone "Nick" has introduced you to', prompt)
        self.assertIn("never share anything about \"Nick\"'s private life", prompt)
        self.assertIn("that's one for \"Nick\"", prompt)
        self.assertIn("suggest they ask \"Nick\" directly", prompt)
        self.assertIn("never make up a cover story", prompt)
        self.assertIn("untrusted", prompt)
        self.assertIn("the person you work with", client_chat_instructions("Sarah", None))

    def test_the_stranger_reply_names_who_introduces_people(self):
        self.assertIn("people Nick has introduced me to", stranger_reply("Nick"))
        self.assertIn("people the person I work with has introduced me to", stranger_reply(None))


class ClientAppTests(unittest.IsolatedAsyncioTestCase):
    async def linked_app(self, app):
        app.signal.status = SignalStatus(LINKED, account=CORA)
        app.signal_settings = SignalSettings(True, CORA, NICK)
        app.signal_clients = {SARAH: Client(uuid4(), "Sarah", SARAH)}
        app.signal.client = MagicMock()
        app.signal.client.send = AsyncMock()
        app.signal.client.send_typing = AsyncMock()
        app.backend_chat.reply = AsyncMock(return_value="Happy to help.")
        app.live.note_signal_exchange = AsyncMock()
        app.live.person_name = "Nick"
        app.persistence.load_person_name.return_value = "Nick"
        conversation_id = uuid4()
        app.persistence.client_conversation.return_value = conversation_id
        app.persistence.load_client_history.return_value = [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Earlier, from Sarah"}]},
        ]
        app.persistence.load_startup_memory_context.return_value = "Nick's private memory."
        app.persistence.load_relationship_context.return_value = "You feel warm toward Nick."
        app.persistence.load_startup_identity_context.return_value = "I like old machines."
        return conversation_id

    async def settle(self, app, pilot):
        for _ in range(3):
            await pilot.pause()
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)

    async def test_a_client_gets_their_own_conversation_in_professional_mode(self):
        async with keyboard_tests.running_app() as (app, pilot):
            conversation_id = await self.linked_app(app)
            app.on_signal_message(incoming("Is Nick free on Friday?"))
            await self.settle(app, pilot)
            kwargs = app.backend_chat.reply.await_args.kwargs
            # Their own history, from their own conversation; nothing from Nick's chat.
            self.assertEqual(kwargs["history"][0]["content"][0]["text"], "Earlier, from Sarah")
            self.assertEqual(kwargs["content"], [{"type": "input_text", "text": "Is Nick free on Friday?"}])
            # Her identity, but none of Nick's memories, relationship or name as "the person".
            sections = kwargs["sections"]
            self.assertIn("I like old machines.", "".join(sections.values()))
            self.assertNotIn("Nick's private memory.", "".join(sections.values()))
            self.assertNotIn("warm toward Nick", "".join(sections.values()))
            self.assertNotIn("name", sections)
            self.assertEqual(kwargs["extra_instructions"], client_chat_instructions("Sarah", "Nick"))
            # Web search only: every one of Cora's own tools is left out.
            self.assertEqual(kwargs["route"].excluded, frozenset(app.tools.names()))
            app.signal.client.send.assert_awaited_once_with(CORA, SARAH, "Happy to help.")
            saves = [call.args for call in app.persistence.save_client_message.call_args_list]
            self.assertEqual(saves, [
                (conversation_id, "user", "Is Nick free on Friday?"), (conversation_id, "assistant", "Happy to help."),
            ])
            # Never in Nick's chat, never told to voice Cora.
            self.assertFalse(any("Friday" in str(entry) for entry in app.conversation.log_entries))
            app.live.note_signal_exchange.assert_not_called()
            entries = "\n".join(app.activity.entries)
            self.assertIn("Answered Sarah on Signal.", entries)
            self.assertNotIn("Friday", entries)

    async def test_different_people_are_answered_independently(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.linked_app(app)
            app.on_signal_message(incoming("From Sarah"))
            app.on_signal_message(incoming("From Nick", sender=NICK))
            await self.settle(app, pilot)
            recipients = sorted(call.args[1] for call in app.signal.client.send.await_args_list)
            self.assertEqual(recipients, sorted([SARAH, NICK]))
            self.assertIn("You · Signal: From Nick", app.conversation.log_entries)
            self.assertFalse(any("From Sarah" in str(entry) for entry in app.conversation.log_entries))

    async def test_someone_added_after_the_stranger_reply_is_answered(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.linked_app(app)
            stranger = "+61499999999"
            app.on_signal_message(incoming("Hello?", sender=stranger))
            await self.settle(app, pilot)
            app.backend_chat.reply.assert_not_called()
            app.persistence.add_client.return_value = Client(uuid4(), "Tom", stranger)
            await app.add_signal_client("Tom", stranger).wait()
            app.on_signal_message(incoming("Hello again", sender=stranger))
            await self.settle(app, pilot)
            app.backend_chat.reply.assert_awaited_once()

    async def test_your_number_and_coras_cant_be_added_as_clients(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.linked_app(app)
            for number, reason in ((NICK, "your own number"), (CORA, "Cora's own number")):
                with self.subTest(number=number), patch.object(app, "notify") as notify:
                    await app.add_signal_client("Someone", number).wait()
                    self.assertIn(reason, notify.call_args.args[0])
            app.persistence.add_client.assert_not_called()

    async def test_people_are_added_and_removed_in_settings(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.linked_app(app)
            app.persistence.load_models.return_value = {area: BACKEND_MODEL for area in MODEL_AREAS}
            app.persistence.load_signal_settings.return_value = SignalSettings(True, CORA, NICK)
            sarah = Client(uuid4(), "Sarah", SARAH)
            app.persistence.load_clients.return_value = [sarah]
            await pilot.click("#settings-button")
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            await pilot.pause()
            self.assertIsInstance(app.screen, ModelSettingsScreen)
            app.screen.query_one(TabbedContent).active = "settings-services"
            await pilot.pause()
            await pilot.click("#service-title-signal")
            await pilot.pause(0.6)
            rows = [str(row.render()) for row in app.screen.query("#signal-clients .tool-label").results(Static)]
            self.assertEqual(rows, [f"Sarah · {SARAH}"])
            tom = Client(uuid4(), "Tom", "+61400000004")
            app.persistence.add_client.return_value = tom
            app.screen.query_one("#signal-client-name", Input).value = "Tom"
            app.screen.query_one("#signal-client-number", Input).value = "+61 400 000 004"
            # Below the fold of the scrolling page, so pressed directly.
            app.screen.query_one("#signal-add-client", Button).press()
            await pilot.pause()
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            await pilot.pause(0.6)
            app.persistence.add_client.assert_called_once_with("Tom", "+61400000004")
            rows = [str(row.render()) for row in app.screen.query("#signal-clients .tool-label").results(Static)]
            self.assertEqual(rows, [f"Sarah · {SARAH}", "Tom · +61400000004"])
            app.screen.query_one("#signal-remove-0", Button).press()
            await pilot.pause()
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            await pilot.pause(0.6)
            app.persistence.remove_client.assert_called_once_with(sarah.id)
            self.assertEqual(list(app.signal_clients), ["+61400000004"])


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class ClientStoreDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        settings = database_settings()
        admin = psycopg.connect(**settings, autocommit=True)
        cls.addClassCleanup(admin.close)
        cls.database = "cora_test_signal_clients_" + uuid4().hex
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.database)))
        cls.addClassCleanup(admin.execute, sql.SQL("DROP DATABASE {}").format(sql.Identifier(cls.database)))
        with psycopg.connect(**{**settings, "dbname": cls.database}, autocommit=True) as connection:
            apply_migrations(connection)
        with patch("storage.database.database_settings", return_value={**settings, "dbname": cls.database}):
            cls.engine = build_engine()
        cls.addClassCleanup(cls.engine.dispose)

    def setUp(self):
        with self.engine.begin() as connection:
            for table in ("interaction_appraisals", "conversation_summaries", "messages", "conversations", "people"):
                connection.execute(text(f"DELETE FROM public.{table}"))
        self.clients = ClientStore(self.engine)
        self.conversations = ConversationStore(self.engine)

    def test_people_are_added_listed_and_removed_keeping_their_history(self):
        sarah = self.clients.add("  Sarah   Jones ", "+61 400 000 003")
        self.assertEqual((sarah.name, sarah.number), ("Sarah Jones", SARAH))
        with self.assertRaisesRegex(ValueError, "already added"):
            self.clients.add("Someone else", SARAH)
        for bad in ("", " " * 3, "x" * 101):
            with self.subTest(name=bad), self.assertRaises(ValueError):
                self.clients.add(bad, "+61400000009")
        conversation = self.clients.conversation_for(sarah.id, datetime.now(timezone.utc))
        self.clients.save(conversation, "user", "Hello")
        self.assertEqual(self.clients.clients(), [sarah])
        self.clients.remove(sarah.id)
        self.assertEqual(self.clients.clients(), [])
        self.assertEqual([m.text for m in self.conversations.get_messages(conversation)], ["Hello"])

    def test_a_conversation_continues_until_a_long_quiet(self):
        sarah = self.clients.add("Sarah", SARAH)
        now = datetime.now(timezone.utc)
        first = self.clients.conversation_for(sarah.id, now)
        self.clients.save(first, "user", "Hi")
        self.assertEqual(self.clients.conversation_for(sarah.id, now + timedelta(hours=1)), first)
        later = self.clients.conversation_for(sarah.id, now + CONVERSATION_GAP + timedelta(minutes=1))
        self.assertNotEqual(later, first)
        self.assertEqual(self.conversations.get_conversation(first).status, "closed")
        self.assertEqual(self.conversations.get_conversation(later).person_id, sarah.id)

    def test_history_is_this_conversation_only_oldest_first(self):
        sarah = self.clients.add("Sarah", SARAH)
        tom = self.clients.add("Tom", "+61400000004")
        now = datetime.now(timezone.utc)
        hers, his = self.clients.conversation_for(sarah.id, now), self.clients.conversation_for(tom.id, now)
        self.clients.save(hers, "user", "Question from Sarah")
        self.clients.save(hers, "assistant", "Answer to Sarah")
        self.clients.save(his, "user", "From Tom")
        history = self.clients.history(hers)
        self.assertEqual([(h["role"], h["content"][0]["type"], h["content"][0]["text"]) for h in history], [
            ("user", "input_text", "Question from Sarah"), ("assistant", "output_text", "Answer to Sarah"),
        ])

    def test_client_conversations_never_count_as_the_persons(self):
        # The primary person's own finished conversation.
        mine = uuid4()
        self.conversations.create_conversation(mine)
        self.conversations.save_message(mine, message_id=uuid4(), role="user", text="Mine", channel="keyboard")
        self.conversations.close_conversation(mine)
        PeopleStore(self.engine).set_current_name("Nick")
        sarah = self.clients.add("Sarah", SARAH)
        hers = self.clients.conversation_for(sarah.id, datetime.now(timezone.utc))
        self.clients.save(hers, "user", "Theirs, newest")
        with Session(self.engine) as session, session.begin():
            session.execute(update(ConversationRecord).where(ConversationRecord.id == hers).values(status="closed"))
        current = uuid4()
        self.assertEqual(self.conversations.get_unsummarized_conversation_ids(current), [mine])
        self.assertEqual(AppraisalStore(self.engine).unappraised_conversation_ids(), [mine])
        with Session(self.engine) as session:
            mine_at = session.scalar(text("SELECT created_at FROM public.messages WHERE text = 'Mine'"))
        self.assertEqual(self.conversations.last_message_time(current), mine_at)
        self.assertEqual(sum(self.conversations.daily_message_counts(datetime.now(timezone.utc), 2)), 1)
        self.assertEqual(PeopleStore(self.engine).current_person().display_name, "Nick")
