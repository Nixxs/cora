"""The share check, and what a client's chat may draw on: what they said, and what the person meant for them."""

import asyncio
import json
import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import psycopg
from psycopg import sql
from sqlalchemy import text

from backend_chat import BackendRoute
from memory.share_memory import SHARE_INSTRUCTIONS, share_memory
from memory.store import MemoryCandidate, MemoryStore, client_names
from migrate import apply_migrations, database_settings
from storage.client_store import ClientStore
from storage.conversation_store import ConversationStore
from storage.database import build_engine
from storage.models import MEMORY_EMBEDDING_DIMENSIONS
from tools.search_memory import SearchMemoryTool
import test_summarize as summarize_tests


def memory_fixture(**changes):
    values = dict(id=uuid4(), kind="task", text="Tell Sarah Nick will be late", evidence="tell Sarah I'll be late",
                  duplicate_of=None, share_checked_at=None, share_audience=None, shared_with_name=None)
    return SimpleNamespace(**{**values, **changes})


class ShareCheckTests(unittest.TestCase):
    def check(self, payload, memory=None):
        store = MagicMock()
        store.get_memory.return_value = memory or memory_fixture()
        store.save_share.side_effect = lambda memory_id, text_, audience, name: (audience, name)
        client = MagicMock()
        client.responses.create.return_value = summarize_tests.response_fixture(json.dumps(payload))
        with patch("memory.share_memory.ClientStore") as clients:
            clients.return_value.clients.return_value = [SimpleNamespace(name="Sarah Jones")]
            result = share_memory(store, uuid4(), client=client)
        return result, store, client

    def test_intent_to_pass_on_is_saved_with_who_it_is_for(self):
        result, store, client = self.check({"audience": "person", "person": " Sarah Jones "})
        self.assertEqual(result, ("person", "Sarah Jones"))
        sent = json.loads(client.responses.create.call_args.kwargs["input"])
        self.assertEqual(sent["contacts"], ["Sarah Jones"])
        self.assertEqual(client.responses.create.call_args.kwargs["instructions"], SHARE_INSTRUCTIONS)
        self.assertEqual(self.check({"audience": "anyone", "person": ""})[0], ("anyone", None))

    def test_none_unsure_or_unnamed_saves_nobody(self):
        for payload in ({"audience": "none", "person": ""}, {"audience": "person", "person": "  "},
                        {"audience": "person", "person": "x" * 101}):
            with self.subTest(payload=payload):
                self.assertEqual(self.check(payload)[0], (None, None))

    def test_a_checked_memory_is_not_asked_again(self):
        result, _, client = self.check({"audience": "anyone", "person": ""},
                                       memory_fixture(share_checked_at=datetime.now(timezone.utc),
                                                      share_audience="person", shared_with_name="Sarah"))
        self.assertEqual(result, ("person", "Sarah"))
        client.responses.create.assert_not_called()

    def test_names_match_in_full_or_by_first_name(self):
        self.assertEqual(sorted(client_names("  Sarah   Jones ")), ["sarah", "sarah jones"])


class ScopedSearchToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_clients_chat_searches_only_what_they_may_know(self):
        scope = (uuid4(), "Sarah")
        route = BackendRoute(memory_scope=scope)
        route.response.item.create = AsyncMock()
        route.response.create = AsyncMock()
        tool = SearchMemoryTool(lambda: route, lambda: False, MagicMock())
        with patch("tools.search_memory._search", return_value={"status": "ok", "matches": []}) as search:
            await tool.handle("c1", {"query": "is Nick running late?", "limit": None})
        self.assertEqual(search.call_args.args, ("is Nick running late?", 8, scope))

    async def test_live_and_desk_searches_are_unscoped(self):
        connection = MagicMock()  # a MagicMock invents attributes; only a real scope counts
        connection.response.item.create = AsyncMock()
        connection.response.create = AsyncMock()
        tool = SearchMemoryTool(lambda: connection, lambda: False, MagicMock())
        with patch("tools.search_memory._search", return_value={"status": "ok", "matches": []}) as search:
            await tool.handle("c1", {"query": "anything", "limit": 3})
        # No scope is passed at all: the desk searches everything, as before.
        self.assertEqual(search.call_args.args, ("anything", 3))


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class SharingDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        settings = database_settings()
        admin = psycopg.connect(**settings, autocommit=True)
        cls.addClassCleanup(admin.close)
        cls.database = "cora_test_memory_sharing_" + uuid4().hex
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.database)))
        cls.addClassCleanup(admin.execute, sql.SQL("DROP DATABASE {}").format(sql.Identifier(cls.database)))
        with psycopg.connect(**{**settings, "dbname": cls.database}, autocommit=True) as connection:
            apply_migrations(connection)
        with patch("storage.database.database_settings", return_value={**settings, "dbname": cls.database}):
            cls.engine = build_engine()
        cls.addClassCleanup(cls.engine.dispose)

    def setUp(self):
        with self.engine.begin() as connection:
            for table in ("memories", "messages", "conversations", "people"):
                connection.execute(text(f"DELETE FROM public.{table}"))
        self.conversations = ConversationStore(self.engine)
        self.memories = MemoryStore(self.engine)
        self.clients = ClientStore(self.engine)
        self.sarah = self.clients.add("Sarah Jones", "+61400000003")
        self.tom = self.clients.add("Tom", "+61400000004")

    def remember(self, said, text_, client=None, share=None):
        if client is None:
            conversation_id = uuid4()
            self.conversations.create_conversation(conversation_id)
        else:
            conversation_id = self.clients.conversation_for(client.id, datetime.now(timezone.utc))
        message = self.conversations.save_message(
            conversation_id, message_id=uuid4(), role="user", text=said, channel="keyboard", status="completed",
        )
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE public.conversations SET status = 'closed', ended_at = now() "
                                    "WHERE id = :id"), {"id": conversation_id})
        memory = self.memories.save_memories(conversation_id, [MemoryCandidate(message.id, "fact", text_, said)])[0]
        vector = [0.0] * MEMORY_EMBEDDING_DIMENSIONS
        vector[0] = 1.0
        self.memories.save_embedding(memory.id, memory.text, vector)
        if share is not None:
            with self.engine.begin() as connection:
                connection.execute(text("UPDATE public.memories SET dedup_checked_at = now(), "
                                        "correction_checked_at = now(), fade_checked_at = now() WHERE id = :id"),
                                   {"id": memory.id})
            self.memories.save_share(memory.id, memory.text, *share)
        return memory

    def visible(self, client):
        vector = [0.0] * MEMORY_EMBEDDING_DIMENSIONS
        vector[0] = 1.0
        searched = {m.text for m in self.memories.search_memories(vector, 20, (client.id, client.name))}
        snapshot = {m["text"] for m in json.loads(self.memories.get_startup_context(client.id, client.name) or "[]")}
        self.assertEqual(searched, snapshot)
        return searched

    def test_a_client_sees_what_they_said_and_what_was_meant_for_them_only(self):
        self.remember("I'm stressed about money", "Nick is stressed about money", share=(None, None))
        self.remember("tell Sarah I'll be late", "Nick will be late for Sarah", share=("person", "Sarah"))
        self.remember("anyone who asks, I'm away till Monday", "Nick is away until Monday", share=("anyone", None))
        self.remember("tell Tom the gate is fixed", "The gate is fixed", share=("person", "tom"))
        self.remember("I live in Perth", "Sarah lives in Perth", client=self.sarah)
        self.remember("I like sailing", "Tom likes sailing", client=self.tom)
        self.assertEqual(self.visible(self.sarah), {
            "Nick will be late for Sarah", "Nick is away until Monday", "Sarah lives in Perth",
        })
        self.assertEqual(self.visible(self.tom), {"Nick is away until Monday", "The gate is fixed", "Tom likes sailing"})

    def test_only_the_persons_settled_memories_wait_for_the_share_check(self):
        mine = self.remember("tell Sarah I'll be late", "Nick will be late")
        self.remember("I live in Perth", "Sarah lives in Perth", client=self.sarah)
        self.assertEqual(self.memories.get_pending_share_ids(), [])  # not through the fade check yet
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE public.memories SET dedup_checked_at = now(), "
                                    "correction_checked_at = now(), fade_checked_at = now()"))
        self.assertEqual(self.memories.get_pending_share_ids(), [mine.id])
        self.assertEqual(self.memories.save_share(mine.id, mine.text, "person", " Sarah "), ("person", "Sarah"))
        self.assertEqual(self.memories.save_share(mine.id, mine.text, "anyone", None), ("person", "Sarah"))
        self.assertEqual(self.memories.get_pending_share_ids(), [])
        with self.assertRaises(ValueError):
            self.memories.save_share(mine.id, mine.text, "person", "")
