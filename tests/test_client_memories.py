"""Memories say who said them: clients' chats join the memory pass without becoming facts about the person."""

import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import psycopg
from psycopg import sql
from sqlalchemy import text

from memory.extract_memories import MEMORY_INSTRUCTIONS, client_memory_note, generate_memories
from memory.store import MemoryCandidate, MemoryStore
from memory.summarize import build_transcript
from migrate import apply_migrations, database_settings
from storage.client_store import ClientStore
from storage.conversation_store import ConversationStore
from storage.database import build_engine
from storage.models import MEMORY_EMBEDDING_DIMENSIONS
from subconscious.interests.interest_store import InterestStore
import test_summarize as summarize_tests


class ExtractionNoteTests(unittest.TestCase):
    def message(self, text="I live in Perth"):
        return SimpleNamespace(id=uuid4(), role="user", text=text, created_at=datetime.now(timezone.utc),
                               channel="signal", status="completed", delivery_status="not_applicable")

    def test_a_clients_memories_are_written_with_their_name(self):
        note = client_memory_note("Sarah")
        self.assertIn('the user is "Sarah"', note)
        self.assertIn("'Sarah lives in Perth'", note)
        self.assertIn("'Sarah said the meeting moved to Friday'", note)
        client = MagicMock()
        client.responses.create.return_value = summarize_tests.response_fixture(json.dumps({"memories": []}))
        generate_memories(client, "model", [self.message()], "Sarah")
        self.assertEqual(client.responses.create.call_args.kwargs["instructions"], MEMORY_INSTRUCTIONS + note)
        generate_memories(client, "model", [self.message()])
        self.assertEqual(client.responses.create.call_args.kwargs["instructions"], MEMORY_INSTRUCTIONS)

    def test_a_clients_summary_names_who_the_user_is(self):
        conversation = SimpleNamespace(status="closed")
        transcript = json.loads(build_transcript(conversation, [], "Sarah"))
        self.assertTrue(transcript["user_is"].startswith("Sarah, someone Cora talks with as a client"))
        self.assertNotIn("user_is", json.loads(build_transcript(conversation, [])))


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class ClientMemoryDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        settings = database_settings()
        admin = psycopg.connect(**settings, autocommit=True)
        cls.addClassCleanup(admin.close)
        cls.database = "cora_test_client_memories_" + uuid4().hex
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.database)))
        cls.addClassCleanup(admin.execute, sql.SQL("DROP DATABASE {}").format(sql.Identifier(cls.database)))
        with psycopg.connect(**{**settings, "dbname": cls.database}, autocommit=True) as connection:
            apply_migrations(connection)
        with patch("storage.database.database_settings", return_value={**settings, "dbname": cls.database}):
            cls.engine = build_engine()
        cls.addClassCleanup(cls.engine.dispose)

    def setUp(self):
        with self.engine.begin() as connection:
            for table in ("memories", "conversation_summaries", "messages", "conversations", "people",
                          "interest_worker_state"):
                connection.execute(text(f"DELETE FROM public.{table}"))
        self.conversations = ConversationStore(self.engine)
        self.memories = MemoryStore(self.engine)
        self.sarah = ClientStore(self.engine).add("Sarah", "+61400000003")

    def remember(self, said, kind="fact", text_=None, client=None, embedding=None):
        """One finished conversation with one user message and one memory from it."""
        if client is None:
            conversation_id = uuid4()
            self.conversations.create_conversation(conversation_id)
        else:
            conversation_id = ClientStore(self.engine).conversation_for(client.id, datetime.now(timezone.utc))
        message = self.conversations.save_message(
            conversation_id, message_id=uuid4(), role="user", text=said, channel="keyboard", status="completed",
        )
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE public.conversations SET status = 'closed', ended_at = now() "
                                    "WHERE id = :id"), {"id": conversation_id})
        memory = self.memories.save_memories(conversation_id, [MemoryCandidate(message.id, kind, text_ or said, said)])[0]
        if embedding is not None:
            vector = [0.0] * MEMORY_EMBEDDING_DIMENSIONS
            vector[embedding] = 1.0
            self.memories.save_embedding(memory.id, memory.text, vector)
            memory = self.memories.get_memory(memory.id)
        return memory, conversation_id

    def test_the_persons_snapshot_and_a_clients_are_kept_apart(self):
        self.remember("I live in Fremantle", text_="Lives in Fremantle")
        self.remember("I live in Perth", text_="Sarah lives in Perth", client=self.sarah)
        mine = [m["text"] for m in json.loads(self.memories.get_startup_context())]
        hers = [m["text"] for m in json.loads(self.memories.get_startup_context(self.sarah.id))]
        self.assertEqual(mine, ["Lives in Fremantle"])
        self.assertEqual(hers, ["Sarah lives in Perth"])

    def test_search_finds_both_and_says_who_said_a_clients(self):
        self.remember("I live in Fremantle", text_="Lives in Fremantle", embedding=0)
        self.remember("I live in Perth", text_="Sarah lives in Perth", client=self.sarah, embedding=0)
        vector = [0.0] * MEMORY_EMBEDDING_DIMENSIONS
        vector[0] = 1.0
        said_by = {match.text: match.said_by for match in self.memories.search_memories(vector)}
        self.assertEqual(said_by, {"Lives in Fremantle": None, "Sarah lives in Perth": "Sarah"})

    def test_duplicates_and_corrections_only_compare_one_persons_memories(self):
        self.remember("I live in Fremantle", text_="Lives in Fremantle", embedding=0)
        theirs, _ = self.remember("I live in Perth", text_="Sarah lives in Perth", client=self.sarah, embedding=0)
        mine_later, _ = self.remember("I moved to Perth", text_="Lives in Perth", embedding=0)
        self.assertEqual([c.text for c in self.memories.get_duplicate_candidates(mine_later)], ["Lives in Fremantle"])
        self.assertEqual([c.text for c in self.memories.get_correction_candidates(mine_later)], ["Lives in Fremantle"])
        self.assertEqual(self.memories.get_duplicate_candidates(theirs), [])

    def test_the_greetings_recent_plan_is_never_a_clients(self):
        self.remember("I'll call the bank tomorrow", kind="task", text_="Sarah will call the bank", client=self.sarah)
        self.assertIsNone(self.memories.recent_plan(datetime.now(timezone.utc)))
        self.remember("I'll fix the gate this week", kind="task", text_="Fix the gate this week")
        self.assertEqual(self.memories.recent_plan(datetime.now(timezone.utc)), "Fix the gate this week")

    def test_client_conversations_now_join_the_memory_pass_and_name_the_client(self):
        _, theirs = self.remember("Hello", client=self.sarah)
        self.assertIn(theirs, self.conversations.get_unsummarized_conversation_ids(uuid4()))
        self.assertEqual(self.conversations.client_name(theirs), "Sarah")
        _, mine = self.remember("Hi")
        self.assertIsNone(self.conversations.client_name(mine))

    def test_interests_grow_only_from_the_persons_memories(self):
        self.remember("I love old engines", kind="preference", text_="Loves old engines", embedding=1)
        self.remember("I love sailing", kind="preference", text_="Sarah loves sailing", client=self.sarah, embedding=2)
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE public.memories SET created_at = now() - interval '1 hour', "
                                    "dedup_checked_at = now()"))
        batch = InterestStore(self.engine).read_new_memories(20)
        self.assertEqual([memory.text for memory in batch.memories], ["Loves old engines"])
