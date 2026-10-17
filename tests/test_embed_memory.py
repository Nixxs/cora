"""Manual one-memory embedding tests; no live SDK requests.

Database tests are opt-in and use uniquely named disposable databases only.
Mocked responses verify plumbing, not embedding quality or semantic search.
"""

from datetime import datetime, timezone
from io import StringIO
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

from openai.types import CreateEmbeddingResponse
from pgvector import Vector
from sqlalchemy import inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

import memory.embed_memory as embedding
import migrate as migrations
from storage.database import build_engine
from memory.store import MemoryCandidate, MemoryStore, validate_memory_embedding
from storage.models import MEMORY_EMBEDDING_DIMENSIONS, MEMORY_EMBEDDING_MODEL, MemoryRecord
import test_memory_store as memory_tests
import test_migrations as migration_tests


def vector_fixture(first=0.1):
    return [first, 1, -0.25] + [0.0] * (MEMORY_EMBEDDING_DIMENSIONS - 3)


def response_fixture(**changes):
    """Instantiate the installed SDK's real response shape, without a client."""
    return CreateEmbeddingResponse(**{
        "object": "list", "model": MEMORY_EMBEDDING_MODEL,
        "data": [{"object": "embedding", "index": 0, "embedding": vector_fixture()}],
        "usage": {"prompt_tokens": 5, "total_tokens": 5}, **changes,
    })


def invalid_vectors():
    yield "not a list", tuple(vector_fixture())
    yield "null", None
    yield "string", "not a vector"
    yield "empty", []
    yield "short", vector_fixture()[:-1]
    yield "long", vector_fixture() + [1.0]
    yield "zero", [0.0] * MEMORY_EMBEDDING_DIMENSIONS
    yield "negative zero", [-0.0] * MEMORY_EMBEDDING_DIMENSIONS
    yield "float32 underflow to zero", [1e-50] * MEMORY_EMBEDDING_DIMENSIONS
    for label, value in (
        ("boolean", True), ("false", False), ("numeric string", "0.1"),
        ("nonnumeric string", "bad"), ("null element", None), ("nested", [1.0]),
        ("mapping", {"value": 1}), ("nan", float("nan")),
        ("infinity", float("inf")), ("negative infinity", -float("inf")),
        ("float32 overflow", 1e39), ("integer overflow", 10 ** 1000),
    ):
        yield label, [value] + vector_fixture()[1:]


def memory_fixture(**changes):
    return SimpleNamespace(**{
        "id": uuid4(), "text": "The user lives in Perth.",
        "evidence": "PRIVATE evidence must not be sent",
        "embedding": None, "embedding_model": None, "embedding_created_at": None,
        **changes,
    })


class EmbedMemoryTests(unittest.TestCase):
    def setUp(self):
        # Even a regression to owned-client creation cannot make a live request.
        factory = patch("memory.embed_memory.OpenAI")
        self.client_factory = factory.start()
        self.addCleanup(factory.stop)
        self.client = MagicMock()
        self.client.embeddings.create.return_value = response_fixture()
        self.owned = self.client_factory.return_value
        self.owned.__enter__.return_value = self.client
        self.store = MagicMock(spec=MemoryStore)
        self.memory = memory_fixture()
        self.store.get_memory.return_value = self.memory
        self.store.save_embedding.return_value = memory_fixture(
            id=self.memory.id, embedding=Vector(vector_fixture()).to_list(),
            embedding_model=MEMORY_EMBEDDING_MODEL,
        )

    def test_validation_canonicalizes_float32_and_mapping_returns_lists(self):
        self.assertEqual(MEMORY_EMBEDDING_DIMENSIONS, 1536)
        self.assertEqual(MEMORY_EMBEDDING_MODEL, "text-embedding-3-small")
        values = vector_fixture()
        original = values.copy()
        canonical = validate_memory_embedding(values)
        self.assertEqual(canonical, Vector(values).to_list())
        self.assertIsNot(canonical, values)
        self.assertEqual(values, original)
        self.assertNotEqual(canonical[0], values[0])
        self.assertTrue(all(type(value) is float for value in canonical))
        column = MemoryRecord.__table__.c.embedding
        self.assertEqual(column.type.dim, 1536)
        processor = column.type.result_processor(postgresql.dialect(), None)
        result = processor(Vector(canonical).to_text())
        self.assertIsInstance(result, list)
        self.assertEqual(result, canonical)
        self.assertIsNone(processor(None))

    def test_invalid_vectors_are_rejected_before_any_database_session(self):
        engine = MagicMock()
        store = MemoryStore(engine)
        with patch("memory.store.Session") as session:
            for label, values in invalid_vectors():
                with self.subTest(case=label):
                    with self.assertRaises(ValueError):
                        validate_memory_embedding(values)
                    with self.assertRaises(ValueError):
                        store.save_embedding(uuid4(), "Original text", values)
            session.assert_not_called()
        engine.connect.assert_not_called()

    def test_generate_embedding_uses_sdk_fixture_and_only_memory_text(self):
        response = self.client.embeddings.create.return_value
        self.assertIsInstance(response, CreateEmbeddingResponse)
        self.assertEqual(response.object, "list")
        self.assertEqual(response.data[0].object, "embedding")
        self.assertEqual(response.usage.total_tokens, 5)
        result = embedding.generate_embedding(self.client, self.memory.text)
        self.assertEqual(result, Vector(vector_fixture()).to_list())
        self.client.embeddings.create.assert_called_once_with(
            model="text-embedding-3-small", input=self.memory.text,
            dimensions=1536, encoding_format="float",
        )

    def test_wrong_response_model_count_or_index_is_not_saved(self):
        item = {"object": "embedding", "index": 0, "embedding": vector_fixture()}
        cases = [
            {"model": "text-embedding-3-large"}, {"data": []},
            {"data": [item, item]}, {"data": [{**item, "index": 1}]},
            {"data": [{**item, "index": -1}]},
        ]
        for changes in cases:
            with self.subTest(changes=list(changes)):
                self.client.embeddings.create.return_value = response_fixture(**changes)
                with self.assertRaises(ValueError):
                    embedding.embed_memory(self.store, self.memory.id, client=self.client)
        self.store.save_embedding.assert_not_called()
        self.client_factory.assert_not_called()

    def test_invalid_response_vectors_are_not_saved(self):
        for label, values in invalid_vectors():
            with self.subTest(case=label):
                # SDK validation can coerce booleans/strings; preserve raw bad
                # values here to test our boundary, not Pydantic coercion.
                self.client.embeddings.create.return_value = SimpleNamespace(
                    model=MEMORY_EMBEDDING_MODEL,
                    data=[SimpleNamespace(index=0, embedding=values)],
                )
                with self.assertRaises(ValueError):
                    embedding.embed_memory(self.store, self.memory.id, client=self.client)
        self.store.save_embedding.assert_not_called()

    def test_missing_cached_and_incompatible_memories_fail_or_return_before_api(self):
        self.store.get_memory.return_value = None
        with self.assertRaises(LookupError):
            embedding.embed_memory(self.store, self.memory.id)
        cached = memory_fixture(
            embedding=Vector(vector_fixture()).to_list(),
            embedding_model=MEMORY_EMBEDDING_MODEL,
            embedding_created_at=datetime(2026, 10, 13, tzinfo=timezone.utc),
        )
        self.store.get_memory.return_value = cached
        for client in (None, self.client):
            with self.subTest(injected=client is not None):
                self.assertIs(embedding.embed_memory(self.store, cached.id, client=client), cached)
        cached.embedding_model = "other-model"
        with self.assertRaises(ValueError):
            embedding.embed_memory(self.store, cached.id)
        self.client_factory.assert_not_called()
        self.client.embeddings.create.assert_not_called()
        self.store.save_embedding.assert_not_called()

    def test_injected_client_is_not_owned_and_save_uses_original_text(self):
        result = embedding.embed_memory(self.store, self.memory.id, client=self.client)
        self.assertIs(result, self.store.save_embedding.return_value)
        self.store.get_memory.assert_called_once_with(self.memory.id)
        self.store.save_embedding.assert_called_once_with(
            self.memory.id, self.memory.text, Vector(vector_fixture()).to_list(),
        )
        self.client_factory.assert_not_called()
        self.client.__enter__.assert_not_called()
        self.client.__exit__.assert_not_called()
        self.client.close.assert_not_called()

    def test_owned_client_has_no_retries_and_closes_before_saving(self):
        events = []
        self.store.get_memory.side_effect = lambda _: events.append("read") or self.memory
        self.client_factory.side_effect = lambda **_: events.append("open") or self.owned
        self.client.embeddings.create.side_effect = lambda **_: events.append("api") or response_fixture()
        self.owned.__exit__.side_effect = lambda *_: events.append("close") or False
        self.store.save_embedding.side_effect = lambda *_: events.append("save") or self.memory
        self.assertIs(embedding.embed_memory(self.store, self.memory.id), self.memory)
        self.assertEqual(events, ["read", "open", "api", "close", "save"])
        self.client_factory.assert_called_once_with(max_retries=0)
        self.owned.__enter__.assert_called_once_with()
        self.owned.__exit__.assert_called_once_with(None, None, None)

    def test_api_failure_never_saves_and_only_owned_client_is_closed(self):
        for injected in (False, True):
            with self.subTest(injected=injected):
                self.owned.reset_mock()
                self.client.embeddings.create.reset_mock()
                failure = RuntimeError("private API error")
                self.client.embeddings.create.side_effect = failure
                with self.assertRaises(RuntimeError) as error:
                    embedding.embed_memory(
                        self.store, self.memory.id, client=self.client if injected else None,
                    )
                self.assertIs(error.exception, failure)
                self.client.embeddings.create.assert_called_once()
                self.assertEqual(self.owned.__exit__.call_count, 0 if injected else 1)
                self.client.close.assert_not_called()
        self.store.save_embedding.assert_not_called()

    def test_save_embedding_locks_refreshes_and_exits_transaction_before_return(self):
        engine = MagicMock()
        row = memory_fixture()
        with patch("memory.store.Session") as factory:
            session = factory.return_value.__enter__.return_value
            session.scalar.return_value = row
            result = MemoryStore(engine).save_embedding(row.id, row.text, vector_fixture())
            factory.assert_called_once_with(engine, expire_on_commit=False)
            statement = session.scalar.call_args.args[0]
            compiled = statement.compile(dialect=postgresql.dialect())
            self.assertIn("FOR UPDATE", str(compiled))
            self.assertIn(row.id, compiled.params.values())
            self.assertIs(result, row)
            self.assertEqual(row.embedding, Vector(vector_fixture()).to_list())
            self.assertEqual(row.embedding_model, "text-embedding-3-small")
            self.assertEqual(str(row.embedding_created_at), "CURRENT_TIMESTAMP")
            session.flush.assert_called_once_with()
            session.refresh.assert_called_once_with(row)
            session.begin.return_value.__exit__.assert_called_once_with(None, None, None)
            factory.return_value.__exit__.assert_called_once_with(None, None, None)

    def test_cli_parses_one_memory_uuid_and_prints_only_safe_summary(self):
        events = []
        saved = self.store.save_embedding.return_value
        engine = MagicMock()
        with patch("sys.argv", ["embed_memory.py", str(saved.id)]), \
                patch("memory.embed_memory.load_dotenv", side_effect=lambda: events.append("dotenv")), \
                patch("memory.embed_memory.build_engine", side_effect=lambda: events.append("engine") or engine), \
                patch("memory.embed_memory.MemoryStore", return_value=self.store) as factory, \
                patch("memory.embed_memory.embed_memory", return_value=saved) as embed, \
                patch("sys.stdout", new_callable=StringIO) as output:
            embedding.main()
        self.assertEqual(events, ["dotenv", "engine"])
        factory.assert_called_once_with(engine)
        embed.assert_called_once_with(self.store, UUID(str(saved.id)))
        engine.dispose.assert_called_once_with()
        self.assertEqual(output.getvalue(),
                         f"Embedding ready: memory_id={saved.id}, model=text-embedding-3-small, dimensions=1536\n")

    def test_cli_help_and_bad_arguments_do_not_load_settings_or_connect(self):
        cases = [(["--help"], 0), ([], 2), (["not-a-uuid"], 2),
                 ([str(uuid4()), str(uuid4())], 2), (["--conversation-id", str(uuid4())], 2)]
        with patch("memory.embed_memory.load_dotenv") as dotenv, \
            patch("memory.embed_memory.build_engine") as engine, \
            patch.dict(os.environ, {"COLUMNS": "200"}):  # argparse wraps help to the terminal width
            for args, code in cases:
                with self.subTest(args=args), patch("sys.argv", ["embed_memory.py", *args]), \
                        patch("sys.stdout", new_callable=StringIO) as output, \
                        patch("sys.stderr", new_callable=StringIO), self.assertRaises(SystemExit) as error:
                    embedding.main()
                self.assertEqual(error.exception.code, code)
                if code == 0:
                    self.assertIn("memories.id UUID, not a conversation UUID", output.getvalue())
            dotenv.assert_not_called()
            engine.assert_not_called()
        self.client_factory.assert_not_called()

    def test_cli_redacts_failures_and_disposes_created_engine(self):
        secret = "PRIVATE transcript evidence vector=[0.1] password=hidden"
        for stage in ("store", "embed"):
            with self.subTest(stage=stage):
                engine = MagicMock()
                with patch("sys.argv", ["embed_memory.py", str(self.memory.id)]), \
                        patch("memory.embed_memory.load_dotenv"), \
                        patch("memory.embed_memory.build_engine", return_value=engine), \
                        patch("memory.embed_memory.MemoryStore", return_value=self.store) as factory, \
                        patch("memory.embed_memory.embed_memory") as embed, \
                        patch("sys.stdout", new_callable=StringIO) as output, \
                        patch("sys.stderr", new_callable=StringIO) as stderr:
                    (factory if stage == "store" else embed).side_effect = RuntimeError(secret)
                    with self.assertRaises(SystemExit) as error:
                        embedding.main()
                self.assertEqual(str(error.exception),
                                 "Embedding not confirmed (RuntimeError). Check memory, database and API settings.")
                self.assertEqual(output.getvalue() + stderr.getvalue(), "")
                self.assertNotIn(secret, str(error.exception))
                engine.dispose.assert_called_once_with()
                if stage == "store":
                    embed.assert_not_called()

    def test_cli_engine_creation_failure_is_redacted_before_store_or_api(self):
        with patch("sys.argv", ["embed_memory.py", str(self.memory.id)]), \
                patch("memory.embed_memory.load_dotenv"), \
                patch("memory.embed_memory.build_engine", side_effect=ValueError("secret database credentials")), \
                patch("memory.embed_memory.MemoryStore") as factory, \
                patch("sys.stdout", new_callable=StringIO) as output:
            with self.assertRaises(SystemExit) as error:
                embedding.main()
        self.assertEqual(str(error.exception),
                         "Embedding not confirmed (ValueError). Check memory, database and API settings.")
        self.assertEqual(output.getvalue(), "")
        factory.assert_not_called()
        self.client_factory.assert_not_called()


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class EmbeddingDatabaseTests(unittest.TestCase):
    # Reuse fixture functions, never inherit/import another discovered TestCase.
    setUpClass = classmethod(memory_tests.MemoryStoreDatabaseTests.setUpClass.__func__)

    def setUp(self):
        self.assertRegex(self.settings["dbname"], r"^cora_test_summaries_[0-9a-f]{32}$")
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT current_database()")).scalar_one(),
                             self.settings["dbname"])
        # This fixture deletes memories FIRST, before summaries/messages/chats.
        memory_tests.MemoryStoreDatabaseTests.setUp(self)
        self.source = self.store.save_message(
            self.conversation_id, message_id=uuid4(), role="user",
            text="I live in Perth. PRIVATE transcript detail.", channel="voice", status="completed",
        )
        self.closed = self.store.close_conversation(self.conversation_id)
        self.candidate = MemoryCandidate(self.source.id, "fact", "The user lives in Perth.", "I live in Perth")
        self.memory = self.memories.save_memories(self.conversation_id, [self.candidate])[0]

    def test_null_detached_read_float32_reopen_cached_rerun_and_original_fields(self):
        fields = ("id", "source_message_id", "kind", "text", "evidence", "created_at")
        original = tuple(getattr(self.memory, field) for field in fields)
        read = self.memories.get_memory(self.memory.id)
        self.assertTrue(inspect(read).detached)
        self.assertEqual((read.embedding, read.embedding_model, read.embedding_created_at), (None, None, None))
        self.assertEqual(tuple(getattr(read, field) for field in fields), original)
        self.assertIsNone(self.memories.get_memory(uuid4()))
        self.assertIsNone(self.memories.get_memory(self.conversation_id))
        client = MagicMock()
        client.embeddings.create.return_value = response_fixture()
        with patch("memory.embed_memory.OpenAI") as factory:
            factory.return_value.__enter__.return_value = client
            saved = embedding.embed_memory(self.memories, read.id)
            cached = embedding.embed_memory(self.memories, read.id)
            factory.assert_called_once_with(max_retries=0)
            factory.return_value.__exit__.assert_called_once_with(None, None, None)
        client.embeddings.create.assert_called_once_with(
            model=MEMORY_EMBEDDING_MODEL, input=self.candidate.text,
            dimensions=1536, encoding_format="float",
        )
        self.assertTrue(inspect(saved).detached)
        self.assertIsNotNone(saved.embedding_created_at.utcoffset())
        self.assertEqual((cached.embedding, cached.embedding_created_at),
                         (saved.embedding, saved.embedding_created_at))
        with patch("storage.database.database_settings", return_value=self.settings):
            reopened = build_engine()
        try:
            with Session(reopened) as session:
                result = session.get(MemoryRecord, read.id)
                self.assertIsInstance(result.embedding, list)
                self.assertEqual(len(result.embedding), 1536)
                self.assertTrue(all(type(value) is float for value in result.embedding))
                self.assertEqual(Vector(result.embedding).to_list(), Vector(vector_fixture()).to_list())
                self.assertEqual(result.embedding_model, "text-embedding-3-small")
                self.assertEqual(result.embedding_created_at, saved.embedding_created_at)
                self.assertEqual(tuple(getattr(result, field) for field in fields), original)
        finally:
            reopened.dispose()
        message = self.store.get_messages(self.conversation_id)[0]
        message_fields = ("id", "conversation_id", "sequence_number", "role", "text", "channel",
                          "created_at", "status", "delivery_status")
        self.assertEqual(tuple(getattr(message, field) for field in message_fields),
                         tuple(getattr(self.source, field) for field in message_fields))
        conversation = self.store.get_conversation(self.conversation_id)
        self.assertEqual((conversation.status, conversation.started_at, conversation.ended_at),
                         (self.closed.status, self.closed.started_at, self.closed.ended_at))
        self.assertIsNone(self.store.get_summary(self.conversation_id))

    def test_canonical_retries_preserve_values_timestamp_and_reject_conflicts(self):
        saved = self.memories.save_embedding(self.memory.id, self.memory.text, vector_fixture())
        snapshot = (saved.embedding.copy(), saved.embedding_model, saved.embedding_created_at)
        for values in (vector_fixture(), Vector(vector_fixture()).to_list()):
            with self.subTest(canonical=values[0] != 0.1):
                retry = self.memories.save_embedding(self.memory.id, self.memory.text, values)
                self.assertEqual((retry.embedding, retry.embedding_model, retry.embedding_created_at), snapshot)
        with self.assertRaises(ValueError):
            self.memories.save_embedding(self.memory.id, self.memory.text, vector_fixture(0.2))
        with self.assertRaises(LookupError):
            self.memories.save_embedding(uuid4(), self.memory.text, vector_fixture())
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE public.memories SET text = :text WHERE id = :id"),
                               {"id": self.memory.id, "text": "Corrected source text"})
        with self.assertRaisesRegex(ValueError, "text changed"):
            self.memories.save_embedding(self.memory.id, self.memory.text, vector_fixture())
        read = self.memories.get_memory(self.memory.id)
        self.assertEqual(read.text, "Corrected source text")
        self.assertEqual((read.embedding, read.embedding_model, read.embedding_created_at), snapshot)
        self.assertIsNone(self.memories.get_memory(uuid4()))

    def test_database_schema_metadata_dimensions_and_nonzero_constraints(self):
        with self.engine.connect() as connection:
            columns = connection.execute(text("""
                SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull
                FROM pg_attribute a WHERE a.attrelid = 'public.memories'::regclass
                AND a.attname IN ('embedding', 'embedding_model', 'embedding_created_at')
                ORDER BY a.attname
            """)).all()
        self.assertEqual([tuple(row) for row in columns], [
            ("embedding", "vector(1536)", False),
            ("embedding_created_at", "timestamp with time zone", False),
            ("embedding_model", "text", False),
        ])
        mapped = {constraint.name for constraint in MemoryRecord.__table__.constraints}
        self.assertTrue({"memories_embedding_metadata_check", "memories_embedding_nonzero_check"} <= mapped)
        vector = Vector(vector_fixture()).to_text()
        now = datetime(2026, 10, 13, tzinfo=timezone.utc)
        cases = []
        # All six partial-null combinations must fail, not just a missing model.
        for mask in range(1, 7):
            cases.append((vector if mask & 1 else None, MEMORY_EMBEDDING_MODEL if mask & 2 else None,
                          now if mask & 4 else None, "memories_embedding_metadata_check"))
        cases.extend([
            (vector, "other-model", now, "memories_embedding_metadata_check"),
            (Vector([0.0] * 1536).to_text(), MEMORY_EMBEDDING_MODEL, now, "memories_embedding_nonzero_check"),
            ("[1,2]", MEMORY_EMBEDDING_MODEL, now, None),
        ])
        for values, model, timestamp, constraint in cases:
            with self.subTest(model=model, constraint=constraint, has_vector=values is not None):
                with self.assertRaises(DBAPIError) as error:
                    with self.engine.begin() as connection:
                        connection.execute(text("""
                            UPDATE public.memories SET embedding = CAST(:vector AS vector),
                            embedding_model = :model, embedding_created_at = :timestamp WHERE id = :id
                        """), {"vector": values, "model": model, "timestamp": timestamp, "id": self.memory.id})
                if constraint is not None:
                    self.assertEqual(error.exception.orig.diag.constraint_name, constraint)
                else:
                    self.assertIn("dimensions", str(error.exception.orig))
                read = self.memories.get_memory(self.memory.id)
                self.assertEqual((read.embedding, read.embedding_model, read.embedding_created_at), (None, None, None))
        self.memories.save_embedding(self.memory.id, self.memory.text, vector_fixture())


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class EmbeddingMigrationDatabaseTests(unittest.TestCase):
    setUpClass = classmethod(migration_tests.MigrationDatabaseTests.setUpClass.__func__)

    def setUp(self):
        self.assertRegex(self.database, r"^cora_test_migrations_[0-9a-f]{32}$")
        migration_tests.MigrationDatabaseTests.setUp(self)
        self.assertEqual(self.connection.execute("SELECT current_database()").fetchone()[0], self.database)

    def test_fifth_migration_preserves_preexisting_memory_and_is_idempotent(self):
        root = Path(__file__).resolve().parents[1] / "migrations"
        first_four = sorted(root.glob("000[1-4]_*.sql"))
        self.assertEqual(len(first_four), 4)
        for path in first_four:
            (self.directory / path.name).write_text(path.read_text(encoding="utf-8-sig"), encoding="utf-8")
        self.assertEqual(migrations.apply_migrations(self.connection, self.directory),
                         [path.name for path in first_four])
        conversation_id, message_id, memory_id = uuid4(), uuid4(), uuid4()
        with self.connection.transaction():
            self.connection.execute("INSERT INTO public.conversations (id, status) VALUES (%s, 'closed')",
                                    (conversation_id,))
            self.connection.execute("""
                INSERT INTO public.messages (id, conversation_id, sequence_number, role, text, channel, status)
                VALUES (%s, %s, 1, 'user', 'I live in Perth.', 'voice', 'completed')
            """, (message_id, conversation_id))
            self.connection.execute("""
                INSERT INTO public.memories (id, source_message_id, kind, text, evidence)
                VALUES (%s, %s, 'fact', 'The user lives in Perth.', 'I live in Perth')
            """, (memory_id, message_id))
        query = "SELECT id, source_message_id, kind, text, evidence, created_at FROM public.memories WHERE id = %s"
        before = self.connection.execute(query, (memory_id,)).fetchone()
        path = root / "0005_add_memory_embeddings.sql"
        (self.directory / path.name).write_text(path.read_text(encoding="utf-8-sig"), encoding="utf-8")
        self.assertEqual(migrations.apply_migrations(self.connection, self.directory), [path.name])
        self.assertEqual(self.connection.execute(query, (memory_id,)).fetchone(), before)
        self.assertEqual(self.connection.execute("""
            SELECT embedding, embedding_model, embedding_created_at FROM public.memories WHERE id = %s
        """, (memory_id,)).fetchone(), (None, None, None))
        self.assertEqual(migrations.apply_migrations(self.connection, self.directory), [])
        self.assertEqual(self.connection.execute(query, (memory_id,)).fetchone(), before)


if __name__ == "__main__":
    unittest.main()