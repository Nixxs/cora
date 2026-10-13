"""Bounded manual search tests; never make live embedding requests.

Database tests are opt-in and reuse only the uniquely named disposable fixture.
Synthetic vectors prove cosine ordering, not semantic retrieval quality.
"""

from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timedelta, timezone
from io import StringIO
import os
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

from openai.types import CreateEmbeddingResponse
from pgvector import Vector
from sqlalchemy import event, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

import memory.search_memory as searching
from memory.store import MemoryCandidate, MemoryMatch, MemoryStore
from storage.models import (
    MEMORY_EMBEDDING_DIMENSIONS, MEMORY_EMBEDDING_MODEL,
    ConversationRecord, MemoryRecord, MessageRecord,
)
import test_embed_memory as embedding_tests
import test_memory_store as memory_tests


MATCH_FIELDS = (
    "memory_id", "kind", "text", "source_message_id", "conversation_id",
    "source_created_at", "distance",
)


def vector_fixture(x=1.0, y=0.0):
    return [x, y] + [0.0] * (MEMORY_EMBEDDING_DIMENSIONS - 2)


def response_fixture(**changes):
    return embedding_tests.response_fixture(**{
        "data": [{"object": "embedding", "index": 0, "embedding": vector_fixture()}],
        **changes,
    })


def match_fixture(**changes):
    return MemoryMatch(**{
        "memory_id": uuid4(), "kind": "fact", "text": "Synthetic remembered fact",
        "source_message_id": uuid4(), "conversation_id": uuid4(),
        # UTC is the preceding day, deliberately unlike the local date.
        "source_created_at": datetime(2020, 2, 3, 1, tzinfo=timezone(timedelta(hours=8))),
        "distance": 0.2928932188134524, **changes,
    })


class SearchMemoryTests(unittest.TestCase):
    def setUp(self):
        # Guard every owned SDK/engine boundary even when exercising regressions.
        for target in ("memory.search_memory.OpenAI", "memory.embed_memory.OpenAI", "memory.search_memory.build_engine"):
            guard = patch(target)
            mocked = guard.start()
            self.addCleanup(guard.stop)
            if target == "memory.search_memory.OpenAI":
                self.client_factory = mocked
            elif target == "memory.search_memory.build_engine":
                self.engine_factory = mocked
        self.client = MagicMock()
        self.client.embeddings.create.return_value = response_fixture()
        self.owned = self.client_factory.return_value
        self.owned.__enter__.return_value = self.client
        self.engine = MagicMock()
        self.engine_factory.return_value = self.engine
        self.store = MagicMock(spec=MemoryStore)
        self.matches = [match_fixture()]
        self.store.search_memories.return_value = self.matches

    def test_invalid_limits_and_queries_fail_before_api_or_database(self):
        limits = (True, False, 0, -1, 21, 1.0, "8", None, [], {})
        queries = ("", " \n\t", "x" * 1001, None, 12, True, b"query", [], {})
        with patch("memory.search_memory.generate_embedding") as generate, \
            patch("memory.store.Session") as session:
            for limit in limits:
                for client in (None, self.client):
                    with self.subTest(limit=limit, injected=client is not None):
                        with self.assertRaisesRegex(ValueError, "limit"):
                            searching.search_memory(self.store, "valid", limit=limit, client=client)
            for query in queries:
                for client in (None, self.client):
                    with self.subTest(query_type=type(query).__name__, injected=client is not None):
                        with self.assertRaisesRegex(ValueError, "query"):
                            searching.search_memory(self.store, query, client=client)
            # Limit validation takes precedence even when the query is invalid.
            with self.assertRaisesRegex(ValueError, "limit"):
                searching.search_memory(self.store, None, limit=False)
            generate.assert_not_called()
            session.assert_not_called()
        self.client_factory.assert_not_called()
        self.client.embeddings.create.assert_not_called()
        self.assertEqual(self.store.mock_calls, [])
        self.engine_factory.assert_not_called()

    def test_valid_injected_search_trims_query_and_uses_real_sdk_response(self):
        self.assertEqual(MEMORY_EMBEDDING_DIMENSIONS, 1536)
        self.assertEqual(MEMORY_EMBEDDING_MODEL, "text-embedding-3-small")
        self.assertIsInstance(self.client.embeddings.create.return_value, CreateEmbeddingResponse)
        with patch("memory.search_memory.generate_embedding", wraps=searching.generate_embedding) as generate:
            for query, limit in (("  a\n", 1), ("  private question? \t", 8), (" " + "x" * 1000 + " ", 20)):
                with self.subTest(limit=limit):
                    self.client.embeddings.create.reset_mock()
                    self.store.search_memories.reset_mock()
                    result = searching.search_memory(self.store, query, limit=limit, client=self.client)
                    self.assertIs(result, self.matches)
                    generate.assert_called_with(self.client, query.strip())
                    self.client.embeddings.create.assert_called_once_with(
                        model="text-embedding-3-small", input=query.strip(),
                        dimensions=1536, encoding_format="float",
                    )
                    self.store.search_memories.assert_called_once_with(Vector(vector_fixture()).to_list(), limit)
        self.client_factory.assert_not_called()
        self.client.__enter__.assert_not_called()
        self.client.__exit__.assert_not_called()
        self.client.close.assert_not_called()
        self.store.get_memory.assert_not_called()
        self.store.get_memories.assert_not_called()
        self.store.save_embedding.assert_not_called()

    def test_owned_client_closes_before_read_session_and_never_owns_engine(self):
        events = []
        store = MemoryStore(self.engine)
        with patch("memory.store.Session") as factory:
            session = factory.return_value.__enter__.return_value
            session.execute.return_value.all.return_value = []
            factory.side_effect = lambda *_: events.append("database") or factory.return_value
            self.client_factory.side_effect = lambda **_: events.append("open") or self.owned

            def request(**kwargs):
                factory.assert_not_called()
                events.append("api")
                return response_fixture()

            self.client.embeddings.create.side_effect = request
            self.owned.__exit__.side_effect = lambda *_: events.append("close") or False
            self.assertEqual(searching.search_memory(store, " question "), [])
            self.assertEqual(events, ["open", "api", "close", "database"])
            self.client_factory.assert_called_once_with(max_retries=0)
            self.owned.__enter__.assert_called_once_with()
            self.owned.__exit__.assert_called_once_with(None, None, None)
            factory.assert_called_once_with(self.engine)
            session.add.assert_not_called()
            session.commit.assert_not_called()
        self.engine.dispose.assert_not_called()

    def test_api_failures_and_invalid_embedding_responses_never_read_database(self):
        item = {"object": "embedding", "index": 0, "embedding": vector_fixture()}
        invalid = [response_fixture(**changes) for changes in (
            {"model": "text-embedding-3-large"}, {"data": []}, {"data": [item, item]},
            {"data": [{**item, "index": 1}]}, {"data": [{**item, "index": -1}]},
        )]
        # Reuse raw fixtures so SDK coercion cannot hide invalid vector values.
        invalid.extend(SimpleNamespace(
            model=MEMORY_EMBEDDING_MODEL, data=[SimpleNamespace(index=0, embedding=values)],
        ) for _, values in embedding_tests.invalid_vectors())
        with patch("memory.store.Session") as session:
            for injected in (False, True):
                for response in invalid:
                    with self.subTest(injected=injected, response_type=type(response).__name__):
                        self.owned.reset_mock()
                        self.client.embeddings.create.return_value = response
                        with self.assertRaises(ValueError):
                            searching.search_memory(self.store, "private query", client=self.client if injected else None)
                        self.assertEqual(self.owned.__exit__.call_count, 0 if injected else 1)
                self.owned.reset_mock()
                failure = RuntimeError("PRIVATE query/API payload")
                self.client.embeddings.create.side_effect = failure
                with self.assertRaises(RuntimeError) as error:
                    searching.search_memory(self.store, "private query", client=self.client if injected else None)
                self.assertIs(error.exception, failure)
                self.assertEqual(self.owned.__exit__.call_count, 0 if injected else 1)
                self.client.embeddings.create.side_effect = None
            session.assert_not_called()
        self.assertEqual(self.store.mock_calls, [])
        self.client.close.assert_not_called()

    def test_store_validates_limit_and_vector_before_database(self):
        store = MemoryStore(self.engine)
        with patch("memory.store.Session") as session:
            for limit in (True, False, 0, -1, 21, 8.0, "8", None, []):
                with self.subTest(limit=limit), self.assertRaises(ValueError):
                    store.search_memories(vector_fixture(), limit)
            for label, vector in embedding_tests.invalid_vectors():
                with self.subTest(vector=label), self.assertRaises(ValueError):
                    store.search_memories(vector)
            session.assert_not_called()
        self.engine.connect.assert_not_called()
        self.engine.dispose.assert_not_called()

    def test_sql_scalar_projection_bound_cosine_filter_limit_and_frozen_dto(self):
        store = MemoryStore(self.engine)
        match = self.matches[0]
        vector = vector_fixture(0.1, 1)
        original = vector.copy()
        with patch("memory.store.Session") as factory:
            session = factory.return_value.__enter__.return_value
            session.execute.return_value.all.return_value = [tuple(getattr(match, name) for name in MATCH_FIELDS)]
            for limit in (1, 8, 20):
                with self.subTest(limit=limit):
                    result = store.search_memories(vector, limit)
                    statement = session.execute.call_args.args[0]
                    compiled = statement.compile(dialect=postgresql.dialect())
                    sql = str(compiled)
                    self.assertEqual([(column.table.name, column.name)
                                      for column in list(statement.selected_columns)[:6]], [
                        ("memories", "id"), ("memories", "kind"), ("memories", "text"),
                        ("memories", "source_message_id"), ("messages", "conversation_id"),
                        ("messages", "created_at"),
                    ])
                    self.assertEqual(len(list(statement.selected_columns)), 7)
                    self.assertEqual(list(statement.selected_columns)[6].name, "distance")
                    self.assertIn("public.memories.embedding <=> %(", sql)
                    self.assertIn("JOIN public.messages ON public.memories.source_message_id = public.messages.id", sql)
                    self.assertIn("public.memories.embedding IS NOT NULL", sql)
                    self.assertIn("public.memories.embedding_model = %(", sql)
                    self.assertIn("public.messages.role = %(", sql)
                    self.assertIn("ORDER BY distance, public.memories.id", sql)
                    self.assertIn("LIMIT %(", sql)
                    self.assertNotIn("1536", sql)  # No enormous literal-bound vector SQL.
                    self.assertNotIn("evidence", sql)
                    self.assertNotIn("messages.text", sql)
                    self.assertEqual(list(compiled.params.values()).count(limit), 1)
                    self.assertIn(MEMORY_EMBEDDING_MODEL, compiled.params.values())
                    self.assertIn("user", compiled.params.values())
                    vectors = [value for value in compiled.params.values() if isinstance(value, list)]
                    self.assertEqual(vectors, [Vector(vector).to_list()])
                    self.assertEqual(result, [match])
                    self.assertIs(type(result[0]), MemoryMatch)
                    self.assertIs(type(result[0].distance), float)
            self.assertEqual(tuple(field.name for field in fields(MemoryMatch)), MATCH_FIELDS)
            with self.assertRaises(FrozenInstanceError):
                result[0].text = "Replacement"
            factory.return_value.__exit__.assert_called_with(None, None, None)
            for method in ("add", "add_all", "commit", "flush", "delete", "begin"):
                getattr(session, method).assert_not_called()
        self.assertEqual(vector, original)
        self.assertIs(store.engine, self.engine)
        self.engine.dispose.assert_not_called()
        self.assertEqual(result[0].source_message_id, match.source_message_id)

    def test_cli_ranked_output_utc_refs_default_explicit_limit_and_empty(self):
        query = "PRIVATE multi word question"
        second = match_fixture(text="Second synthetic fact", distance=1.0)
        for args, limit, matches in (([query], 8, [self.matches[0], second]),
                                     ([query, "--limit", "2"], 2, [])):
            events = []
            with self.subTest(limit=limit), patch("sys.argv", ["search_memory.py", *args]), \
                    patch("memory.search_memory.load_dotenv", side_effect=lambda: events.append("dotenv")), \
                    patch("memory.search_memory.MemoryStore", return_value=self.store) as factory, \
                    patch("memory.search_memory.search_memory", return_value=matches) as search, \
                    patch("sys.stdout", new_callable=StringIO) as output:
                self.engine_factory.side_effect = lambda: events.append("engine") or self.engine
                self.engine.reset_mock()
                searching.main()
            self.assertEqual(events, ["dotenv", "engine"])
            factory.assert_called_once_with(self.engine)
            search.assert_called_once_with(self.store, query, limit=limit)
            self.engine.dispose.assert_called_once_with()
            if matches:
                expected = "".join(
                    f"{rank}. [{match.kind}; source=2020-02-02; cosine_distance={match.distance:.4f}] {match.text}\n"
                    f"   memory_id={match.memory_id}, source_message_id={match.source_message_id}, conversation_id={match.conversation_id}\n"
                    for rank, match in enumerate(matches, 1)
                )
                self.assertEqual(output.getvalue(), expected)
            else:
                self.assertEqual(output.getvalue(), "No embedded memories matched. Unembedded memories are not searched.\n")
            self.assertNotIn(query, output.getvalue())
        self.client_factory.assert_not_called()

    def test_cli_help_and_argument_errors_precede_environment_engine_and_api(self):
        cases = ((["--help"], 0), ([], 2), (["query", "--limit", "no"], 2),
                 (["unquoted", "extra"], 2), (["query", "--unknown"], 2))
        with patch("memory.search_memory.load_dotenv") as dotenv, \
            patch("memory.search_memory.search_memory") as search:
            for args, code in cases:
                with self.subTest(args=args), patch("sys.argv", ["search_memory.py", *args]), \
                        patch("sys.stdout", new_callable=StringIO) as output, \
                        patch("sys.stderr", new_callable=StringIO), self.assertRaises(SystemExit) as error:
                    searching.main()
                self.assertEqual(error.exception.code, code)
                if code == 0:
                    self.assertIn("--limit", output.getvalue())
                    self.assertIn("quotes", output.getvalue())
                    self.assertIn("billable", output.getvalue())
            dotenv.assert_not_called()
            search.assert_not_called()
        self.engine_factory.assert_not_called()
        self.client_factory.assert_not_called()

    def test_cli_private_api_and_sql_errors_redacted_and_engine_always_disposed(self):
        private = "PRIVATE query transcript password=secret vector=[1,0]"
        for stage in ("engine", "store", "api", "sql", "invalid"):
            self.engine_factory.reset_mock(side_effect=True)
            self.engine_factory.return_value = self.engine
            self.engine.reset_mock()
            self.client.embeddings.create.side_effect = None
            failure = (DBAPIError("SELECT PRIVATE SQL", {"query": private}, RuntimeError(private))
                       if stage == "sql" else RuntimeError(private))
            with self.subTest(stage=stage), \
                    patch("sys.argv", ["search_memory.py", private, "--limit", "0" if stage == "invalid" else "8"]), \
                    patch("memory.search_memory.load_dotenv"), \
                    patch("memory.search_memory.MemoryStore", return_value=self.store) as factory, \
                    patch("memory.store.Session") as session, \
                    patch("sys.stdout", new_callable=StringIO) as output, \
                    patch("sys.stderr", new_callable=StringIO) as stderr:
                if stage == "engine":
                    self.engine_factory.side_effect = failure
                elif stage == "store":
                    factory.side_effect = failure
                elif stage == "api":
                    self.client.embeddings.create.side_effect = failure
                elif stage == "sql":
                    factory.return_value = MemoryStore(self.engine)
                    session.return_value.__enter__.return_value.execute.side_effect = failure
                with self.assertRaises(SystemExit) as error:
                    searching.main()
            name = "ValueError" if stage == "invalid" else type(failure).__name__
            self.assertEqual(str(error.exception),
                             f"Search not confirmed ({name}). Check query, database and API settings.")
            self.assertEqual(output.getvalue() + stderr.getvalue(), "")
            self.assertNotIn(private, str(error.exception))
            if stage == "engine":
                factory.assert_not_called()
                self.engine.dispose.assert_not_called()
            else:
                self.engine.dispose.assert_called_once_with()
            if stage in ("engine", "store", "api", "invalid"):
                session.assert_not_called()


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class SearchMemoryDatabaseTests(unittest.TestCase):
    # Import the module, not a discovered TestCase; all writes use this fixture.
    setUpClass = classmethod(memory_tests.MemoryStoreDatabaseTests.setUpClass.__func__)

    def setUp(self):
        self.assertRegex(self.settings["dbname"], r"^cora_test_summaries_[0-9a-f]{32}$")
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT current_database()")).scalar_one(), self.settings["dbname"])
        # Deletes memories first, then summaries, messages and conversations.
        memory_tests.MemoryStoreDatabaseTests.setUp(self)
        for target in ("memory.search_memory.OpenAI", "memory.embed_memory.OpenAI"):
            guard = patch(target)
            guard.start()
            self.addCleanup(guard.stop)
        self.source_date = datetime(2019, 4, 5, 12, tzinfo=timezone.utc)
        self.embedding_date = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)

    def seed(self, *, embedded=True):
        self.source = self.store.save_message(
            self.conversation_id, message_id=uuid4(), role="user",
            text="Synthetic source quote. PRIVATE full history.", channel="keyboard", status="completed",
        )
        assistant = self.store.save_message(
            self.conversation_id, message_id=uuid4(), role="assistant",
            text="Synthetic assistant quote", channel="screen", status="completed",
        )
        self.store.close_conversation(self.conversation_id)
        specs = [("tie high", vector_fixture()), ("diagonal", vector_fixture(1, 1)),
                 ("orthogonal", vector_fixture(0, 1)), ("opposite", vector_fixture(-1, 0)),
                 ("tie low", vector_fixture()), ("unembedded", None)]
        saved = self.memories.save_memories(self.conversation_id, [
            MemoryCandidate(self.source.id, "fact", label, "Synthetic source quote")
            for label, _ in specs
        ])
        rows = {row.text: row for row in saved}
        # Unique per test, with explicit stable UUID ordering independent of insertion.
        base = (uuid4().int >> 16) << 16
        self.ids = {label: UUID(int=base + index) for index, label in enumerate(
            ("tie low", "tie high", "diagonal", "orthogonal", "opposite", "unembedded", "assistant"), 1)}
        with Session(self.engine) as session, session.begin():
            session.get(MessageRecord, self.source.id).created_at = self.source_date
            for label, vector in specs:
                row = session.get(MemoryRecord, rows[label].id)
                row.id = self.ids[label]
                if embedded and vector is not None:
                    row.embedding = vector
                    row.embedding_model = MEMORY_EMBEDDING_MODEL
                    row.embedding_created_at = self.embedding_date
            # Deliberately bypass the user-source application validator, not DB constraints.
            if embedded:
                session.add(MemoryRecord(
                    id=self.ids["assistant"], source_message_id=assistant.id, kind="fact",
                    text="Assistant-linked invalid memory", evidence="Synthetic assistant quote",
                    embedding=vector_fixture(), embedding_model=MEMORY_EMBEDDING_MODEL,
                    embedding_created_at=self.embedding_date,
                ))

    def snapshot(self):
        # Capture original rows and embedding metadata, not merely row counts.
        with self.engine.connect() as connection:
            return tuple(tuple(tuple(row) for row in connection.execute(
                select(model.__table__).order_by(model.id),
            )) for model in (ConversationRecord, MessageRecord, MemoryRecord))

    def test_cosine_ranking_sql_limit_stable_ties_source_metadata_and_scalar_results(self):
        self.seed()
        before = self.snapshot()
        expected = [self.ids[label] for label in ("tie low", "tie high", "diagonal", "orthogonal", "opposite")]
        for _ in range(2):
            matches = self.memories.search_memories(vector_fixture())
            self.assertEqual([match.memory_id for match in matches], expected)
        for limit in (1, 2, 3, 20):
            self.assertEqual([match.memory_id for match in self.memories.search_memories(vector_fixture(), limit)],
                             expected[:limit])
        for match, distance in zip(matches, (0.0, 0.0, 0.2928932188134524, 1.0, 2.0)):
            self.assertAlmostEqual(match.distance, distance, places=6)
            self.assertIs(type(match.distance), float)
            self.assertEqual((match.source_message_id, match.conversation_id, match.source_created_at),
                             (self.source.id, self.conversation_id, self.source_date))
            self.assertNotEqual(match.source_created_at, self.embedding_date)
            self.assertEqual(set(vars(match)), set(MATCH_FIELDS))
            self.assertNotIn("PRIVATE", match.text)
            self.assertEqual(match.kind, "fact")
            with self.assertRaises(FrozenInstanceError):
                match.distance = 99.0
        self.assertEqual(self.snapshot(), before)
        self.engine.dispose()
        # DTOs contain scalar values, not lazy ORM relationships or vectors.
        self.assertEqual(matches[0].memory_id, expected[0])
        self.assertEqual(matches[0].source_created_at, self.source_date)

    def test_search_orchestrator_mocked_api_source_refs_and_no_row_mutations(self):
        self.seed()
        before = self.snapshot()
        client = MagicMock()
        client.embeddings.create.return_value = response_fixture()
        statements = []

        def capture(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(self.engine, "before_cursor_execute", capture)
        try:
            def request(**kwargs):
                self.assertEqual(statements, [])  # No read/session held over API call.
                return response_fixture()

            client.embeddings.create.side_effect = request
            matches = searching.search_memory(self.memories, "  PRIVATE synthetic query  ", limit=3, client=client)
        finally:
            event.remove(self.engine, "before_cursor_execute", capture)
        client.embeddings.create.assert_called_once_with(
            model=MEMORY_EMBEDDING_MODEL, input="PRIVATE synthetic query", dimensions=1536, encoding_format="float",
        )
        client.close.assert_not_called()
        client.__enter__.assert_not_called()
        client.__exit__.assert_not_called()
        self.assertEqual(len(statements), 1)
        self.assertTrue(statements[0].lstrip().upper().startswith("SELECT"))
        self.assertIn("<=>", statements[0])
        self.assertIn("LIMIT", statements[0])
        self.assertEqual([match.memory_id for match in matches],
                         [self.ids[label] for label in ("tie low", "tie high", "diagonal")])
        for match in matches:
            self.assertEqual((match.source_message_id, match.conversation_id, match.source_created_at),
                             (self.source.id, self.conversation_id, self.source_date))
        self.assertEqual(self.snapshot(), before)

    def test_empty_search_with_only_unembedded_memories_returns_no_matches_or_writes(self):
        self.assertEqual(self.memories.search_memories(vector_fixture()), [])
        self.seed(embedded=False)
        before = self.snapshot()
        self.assertEqual(self.memories.search_memories(vector_fixture(), 20), [])
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(all(row.embedding is None for row in self.memories.get_memories(self.conversation_id)))


if __name__ == "__main__":
    unittest.main()