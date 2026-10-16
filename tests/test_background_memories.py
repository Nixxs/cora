"""Background memory plumbing; model clients are mocked, DB tests are opt-in."""

import asyncio
from contextlib import contextmanager
import json
import os
from threading import Event, get_ident
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, call, patch
from uuid import uuid4

from textual.widgets import Collapsible

from app import CoraApp
import memory.deduplicate_memory as deduplication
import memory.embed_memory as embedding
from memory.store import MemoryCandidate, MemoryStore
from storage.database import build_engine
from storage.models import MemoryRecord
import test_background_summaries as background_tests
import test_embed_memory as embedding_tests
import test_keyboard_persistence as keyboard_tests
import test_memory_store as memory_tests


# running_app replaces decorated methods: capture these before entering it.
SUMMARY_WORKER = CoraApp.summarize_previous_conversations
STARTUP_WORKER = CoraApp.check_interrupted_conversations
REAL_EMBED_MEMORY = embedding.embed_memory
REAL_DEDUPLICATE_MEMORY = deduplication.deduplicate_memory


class BackgroundMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_oldest_first_extract_then_summary_off_thread_without_private_logging(self):
        oldest, newer = uuid4(), uuid4()
        main_thread = get_ident()
        extraction_threads = []
        private = SimpleNamespace(text="Private ongoing plan", evidence="Private exact quote")
        async with keyboard_tests.running_app() as (app, pilot):
            app.persistence.mark_interrupted_conversations.return_value = 0
            app.persistence.load_startup_memory_context.return_value = "Previously loaded memories"
            order = MagicMock()
            order.attach_mock(app.persistence.load_startup_memory_context, "startup")
            app.startup_worker = STARTUP_WORKER(app)
            await asyncio.wait_for(app.startup_worker.wait(), 5)
            async with background_tests.mocked_pass(app, [oldest, newer]) as context:
                engine, store, summary, started, disposed, threads, build, construct = context
                order.attach_mock(summary.extraction, "extract")
                order.attach_mock(summary, "summary")

                def extract(store_arg, conversation_id):
                    extraction_threads.append(get_ident())
                    return [private] if conversation_id == oldest else []

                summary.extraction.side_effect = extract
                with patch("memory.extract_memories.OpenAI") as extraction_api, \
                    patch("memory.summarize.OpenAI") as summary_api:
                    app.summary_worker = SUMMARY_WORKER(app)
                    await asyncio.wait_for(app.summary_worker.wait(), 5)
                    await asyncio.wait_for(disposed.wait(), 5)
                    extraction_api.assert_not_called()
                    summary_api.assert_not_called()
                self.assertEqual(order.mock_calls, [
                    call.startup(), call.extract(store, oldest), call.summary(store, oldest),
                    call.extract(store, newer), call.summary(store, newer),
                ])
                store.get_unsummarized_conversation_ids.assert_called_once_with(
                    app.persistence.conversation_id,
                )
                build.assert_called_once_with()
                construct.assert_called_once_with(engine)
                self.assertIsNot(engine, app.persistence.store.engine)
                self.assertEqual(len(extraction_threads), 2)
                self.assertEqual(len(threads), 2)
                self.assertTrue(all(thread != main_thread for thread in extraction_threads + threads))
                engine.dispose.assert_called_once_with()
                entries = "\n".join(app.activity.entries)
                for conversation_id, count in ((oldest, 1), (newer, 0)):
                    extraction_log = f"Memories extracted: conversation_id={conversation_id}, count={count}"
                    summary_log = f"Summary saved: conversation_id={conversation_id}"
                    self.assertIn(extraction_log, entries)
                    self.assertIn(summary_log, entries)
                    self.assertLess(entries.index(extraction_log), entries.index(summary_log))
                self.assertNotIn(private.text, entries)
                self.assertNotIn(private.evidence, entries)
                # Startup is a snapshot; this pass does not update the live session.
                self.assertEqual(app.live.memory_context, "Previously loaded memories")
                app.persistence.load_startup_memory_context.assert_called_once_with()

    async def test_extraction_failure_stops_summary_and_later_ids_with_type_only_error(self):
        first, later = uuid4(), uuid4()
        secret = "private transcript, SQL parameters, API credentials"
        async with keyboard_tests.running_app() as (app, pilot):
            async with background_tests.mocked_pass(app, [first, later]) as context:
                engine, store, summary, started, disposed, threads, build, construct = context
                summary.extraction.side_effect = RuntimeError(secret)
                app.summary_worker = SUMMARY_WORKER(app)
                await asyncio.wait_for(app.summary_worker.wait(), 5)
                await asyncio.wait_for(disposed.wait(), 5)
                summary.extraction.assert_called_once_with(store, first)
                summary.assert_not_called()
                self.assertEqual(threads, [])
                engine.dispose.assert_called_once_with()
                entries = "\n".join(app.activity.entries)
                self.assertIn("Background memory pass stopped (RuntimeError).", entries)
                self.assertEqual(app.query_one("#activity-panel", Collapsible).title,
                                 "Activity · Memory processing failed")
                for value in secret.split(", "):
                    self.assertNotIn(value, entries)
                self.assertNotIn("Memories extracted", entries)
                self.assertNotIn("Summary saved", entries)

    async def test_shutdown_during_extraction_allows_inflight_save_but_no_summary_or_next_id(self):
        first, later = uuid4(), uuid4()
        loop = asyncio.get_running_loop()
        extraction_started = asyncio.Event()
        release = Event()  # mocked_pass's release only controls its summary stub.
        saved = []
        private = SimpleNamespace(text="Private saved task", evidence="Private source quote")
        async with keyboard_tests.running_app() as (app, pilot):
            async with background_tests.mocked_pass(app, [first, later]) as context:
                engine, store, summary, started, disposed, threads, build, construct = context

                def extract(store_arg, conversation_id):
                    loop.call_soon_threadsafe(extraction_started.set)
                    if not release.wait(timeout=5):
                        raise TimeoutError("Test did not release extraction")
                    # A synchronous extractor can still commit after cancellation.
                    saved.append((conversation_id, private))
                    return [private]

                summary.extraction.side_effect = extract
                try:
                    app.summary_worker = SUMMARY_WORKER(app)
                    await asyncio.wait_for(extraction_started.wait(), 5)
                    self.assertIsNot(engine, app.persistence.store.engine)
                    with patch.object(app, "exit") as exit_app:
                        await asyncio.wait_for(app.action_disconnect_and_quit(), 3)
                        exit_app.assert_called_once_with()
                    self.assertTrue(app.closing)
                    self.assertTrue(app.summary_worker.is_cancelled)
                    app.live.close.assert_awaited_once_with()
                    app.persistence.finish_conversation.assert_called_once_with()
                    app.persistence.close.assert_called_once_with()
                    self.assertEqual(saved, [])
                    self.assertFalse(disposed.is_set())
                    engine.dispose.assert_not_called()
                    summary.assert_not_called()
                finally:
                    # Cancelled Worker.wait() does not join the executor thread.
                    release.set()
                    if app.summary_worker is not None:
                        await asyncio.wait_for(disposed.wait(), 5)
                self.assertEqual(saved, [(first, private)])
                summary.extraction.assert_called_once_with(store, first)
                summary.assert_not_called()
                engine.dispose.assert_called_once_with()
                entries = "\n".join(app.activity.entries)
                for value in (private.text, private.evidence, "Memories extracted", "Summary saved",
                              "Background memory pass stopped", "Memory processing failed"):
                    self.assertNotIn(value, entries)


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1",
                     "Set CORA_RUN_DB_TESTS=1 for database tests")
class BackgroundMemoryDatabaseTests(unittest.IsolatedAsyncioTestCase):
    # Unique disposable DB only; remove FK-dependent memories before source rows.
    setUpClass = classmethod(memory_tests.MemoryStoreDatabaseTests.setUpClass.__func__)
    setUp = memory_tests.MemoryStoreDatabaseTests.setUp

    async def test_real_extractor_saves_grounded_task_before_summary_and_second_pass_skips_clients(self):
        await self.check_pipeline(with_memory=True)

    async def test_zero_memories_still_saves_summary_and_second_pass_skips_clients(self):
        await self.check_pipeline(with_memory=False)

    async def check_pipeline(self, *, with_memory):
        source_text = "I am going to submit my visa application next Tuesday." if with_memory else "Hello Cora."
        memory_text = "The user plans to submit their visa application next Tuesday."
        summary_text = "The user discussed next Tuesday's application." if with_memory else "The user greeted Cora."
        previous = self.conversation_id
        source = self.store.save_message(
            previous, message_id=uuid4(), role="user", text=source_text,
            channel="voice", status="incomplete",
        )
        self.store.close_conversation(previous)
        # Previously summarised chats are not backfilled by the background queue.
        already_summarized = uuid4()
        self.store.create_conversation(already_summarized)
        self.store.save_message(
            already_summarized, message_id=uuid4(), role="user", text="I prefer small steps.",
            channel="keyboard", status="completed",
        )
        self.store.close_conversation(already_summarized)
        self.store.save_summary(already_summarized, "Already summarised.")
        rows = [{"source_message_id": str(source.id), "kind": "task",
                 "text": memory_text, "evidence": source_text}] if with_memory else []
        loop = asyncio.get_running_loop()
        disposed = asyncio.Event()
        request_order, request_threads = [], []
        # Only the disposable fixture's settings are used for this separate engine.
        with patch("storage.database.database_settings", return_value=self.settings):
            background_engine = build_engine()
        real_dispose = background_engine.dispose

        def dispose():
            try:
                real_dispose()
            finally:
                loop.call_soon_threadsafe(disposed.set)

        def extract_response(**request):
            request_order.append("extract")
            request_threads.append(get_ident())
            self.assertIsNone(self.store.get_summary(previous))
            payload = json.loads(request["input"])
            self.assertEqual(len(payload["messages"]), 1)
            self.assertEqual(payload["messages"][0]["id"], str(source.id))
            self.assertEqual(payload["messages"][0]["role"], "user")
            self.assertEqual(payload["messages"][0]["text"], source_text)
            self.assertEqual(request["text"]["format"]["name"], "cora_memories")
            return SimpleNamespace(status="completed", output=[], output_text=json.dumps({"memories": rows}))

        def summary_response(**request):
            request_order.append("summary")
            request_threads.append(get_ident())
            self.assertEqual(len(self.memories.get_memories(previous)), int(with_memory))
            self.assertIsNone(self.store.get_summary(previous))
            self.assertEqual(json.loads(request["input"])["messages"][0]["text"], source_text)
            return SimpleNamespace(status="completed", output=[], output_text=summary_text)

        try:
            with patch("memory.background.build_engine", return_value=background_engine) as build, \
                    patch.object(background_engine, "dispose", side_effect=dispose) as disposal, \
                    patch("memory.extract_memories.OpenAI") as extraction_factory, \
                    patch("memory.summarize.OpenAI") as summary_factory:
                extraction_client, summary_client = MagicMock(), MagicMock()
                extraction_factory.return_value.__enter__.return_value = extraction_client
                summary_factory.return_value.__enter__.return_value = summary_client
                extraction_client.responses.create.side_effect = extract_response
                summary_client.responses.create.side_effect = summary_response
                # Default persistence is mocked: no real startup DB or live connection.
                async with keyboard_tests.running_app() as (app, pilot):
                    app.persistence.mark_interrupted_conversations.return_value = 0
                    app.startup_worker = STARTUP_WORKER(app)
                    await asyncio.wait_for(app.startup_worker.wait(), 5)
                    build.assert_not_called()
                    self.assertEqual(app.live.memory_context, "")
                    self.assertIsNone(self.store.get_conversation(app.persistence.conversation_id))
                    self.assertEqual(self.store.get_unsummarized_conversation_ids(
                        app.persistence.conversation_id), [previous])
                    self.assertIsNot(background_engine, self.engine)
                    try:
                        app.summary_worker = SUMMARY_WORKER(app)
                        await asyncio.wait_for(app.summary_worker.wait(), 5)
                        await asyncio.wait_for(disposed.wait(), 5)
                        self.assertEqual(request_order, ["extract", "summary"])
                        self.assertTrue(all(thread != get_ident() for thread in request_threads))
                        memories = self.memories.get_memories(previous)
                        self.assertEqual(len(memories), int(with_memory))
                        if with_memory:
                            self.assertIsInstance(memories[0], MemoryRecord)
                            self.assertEqual((memories[0].source_message_id, memories[0].kind,
                                              memories[0].text, memories[0].evidence),
                                             (source.id, "task", memory_text, source_text))
                            self.assertIn(memory_text, self.memories.get_startup_context())
                        self.assertEqual(app.live.memory_context, "")
                        app.persistence.load_startup_memory_context.assert_called_once_with()
                        first_summary = self.store.get_summary(previous)
                        self.assertIsNotNone(first_summary)
                        self.assertEqual(first_summary.text, summary_text)
                        self.assertEqual(self.store.get_unsummarized_conversation_ids(
                            app.persistence.conversation_id), [])
                        entries = "\n".join(app.activity.entries)
                        self.assertIn(f"Memories extracted: conversation_id={previous}, count={int(with_memory)}", entries)
                        for private in (source_text, memory_text, summary_text):
                            self.assertNotIn(private, entries)

                        disposed.clear()
                        app.summary_worker = SUMMARY_WORKER(app)
                        await asyncio.wait_for(app.summary_worker.wait(), 5)
                        await asyncio.wait_for(disposed.wait(), 5)
                        self.assertEqual(build.call_args_list, [call(), call()])
                        self.assertEqual(disposal.call_count, 2)
                        extraction_client.responses.create.assert_called_once()
                        summary_client.responses.create.assert_called_once()
                        for factory in (extraction_factory, summary_factory):
                            factory.assert_called_once_with(max_retries=0)
                            factory.return_value.__exit__.assert_called_once_with(None, None, None)
                        again = self.store.get_summary(previous)
                        self.assertEqual((again.text, again.created_at),
                                         (first_summary.text, first_summary.created_at))
                        self.assertEqual([(row.id, row.created_at) for row in self.memories.get_memories(previous)],
                                         [(row.id, row.created_at) for row in memories])
                        self.assertEqual(self.memories.get_memories(already_summarized), [])
                        message = self.store.get_messages(previous)[0]
                        self.assertEqual((message.id, message.text, message.status),
                                         (source.id, source_text, "incomplete"))
                    finally:
                        if app.summary_worker is not None:
                            await asyncio.wait_for(disposed.wait(), 5)
        finally:
            real_dispose()


@contextmanager
def mocked_maintenance(app, *, conversations=(), embedding_ids=(), dedup_ids=()):
    loop = asyncio.get_running_loop()
    disposed = asyncio.Event()
    engine = MagicMock()
    engine.dispose.side_effect = lambda: loop.call_soon_threadsafe(disposed.set)
    conversations_store = MagicMock()
    conversations_store.get_unsummarized_conversation_ids.return_value = list(conversations)
    memory_store = MagicMock(spec=MemoryStore)
    memory_store.get_pending_embedding_ids.return_value = list(embedding_ids)
    memory_store.get_pending_deduplication_ids.return_value = list(dedup_ids)
    with patch("memory.background.build_engine", return_value=engine) as build, \
            patch("memory.background.ConversationStore", return_value=conversations_store) as conversation_factory, \
            patch("memory.background.MemoryStore", return_value=memory_store) as memory_factory, \
            patch("memory.background.extract_memories", return_value=[]) as extract, \
            patch("memory.background.summarize_conversation") as summary, \
            patch("memory.background.embed_memory") as embed, \
            patch("memory.background.deduplicate_memory", return_value=False) as dedup, \
                 patch("memory.embed_memory.OpenAI") as embedding_api, \
                 patch("memory.deduplicate_memory.OpenAI") as dedup_api, \
                 patch("memory.extract_memories.OpenAI") as extraction_api, \
                 patch("memory.summarize.OpenAI") as summary_api:
        yield (engine, conversations_store, memory_store, extract, summary, embed, dedup,
               disposed, build, conversation_factory, memory_factory)
        for factory in (embedding_api, dedup_api, extraction_api, summary_api):
            factory.assert_not_called()


class MemoryMaintenanceTests(unittest.IsolatedAsyncioTestCase):
    """Background embedding/dedup maintenance, including already-summarised chats."""

    async def test_queue_first_then_all_embeddings_then_dedup_with_own_engine_even_when_queue_empty(self):
        for with_conversation in (True, False):
            with self.subTest(with_conversation=with_conversation):
                previous, first, second = uuid4(), uuid4(), uuid4()
                async with keyboard_tests.running_app() as (app, pilot):
                    async_queue = [previous] if with_conversation else []
                    with mocked_maintenance(app, conversations=async_queue,
                                            embedding_ids=[first, second], dedup_ids=[first, second]) as ctx:
                        (engine, store, memories, extract, summary, embed, dedup,
                         disposed, build, conversation_factory, memory_factory) = ctx
                        order = MagicMock()
                        for name, mock in (("queue", store.get_unsummarized_conversation_ids),
                                           ("extract", extract), ("summary", summary),
                                           ("pending_embeddings", memories.get_pending_embedding_ids),
                                           ("embed", embed),
                                           ("pending_dedup", memories.get_pending_deduplication_ids),
                                           ("dedup", dedup)):
                            order.attach_mock(mock, name)
                        threads = []
                        embed.side_effect = lambda *_: threads.append(get_ident())
                        dedup.side_effect = lambda *_: threads.append(get_ident()) or False
                        app.summary_worker = SUMMARY_WORKER(app)
                        await asyncio.wait_for(app.summary_worker.wait(), 5)
                        await asyncio.wait_for(disposed.wait(), 5)
                        expected = [call.queue(app.persistence.conversation_id)]
                        if with_conversation:
                            expected += [call.extract(store, previous), call.summary(store, previous)]
                        expected += [call.pending_embeddings(), call.embed(memories, first),
                                     call.embed(memories, second), call.pending_dedup(),
                                     call.dedup(memories, first), call.dedup(memories, second)]
                        self.assertEqual(order.mock_calls, expected)
                        self.assertEqual(len(threads), 4)
                        self.assertTrue(all(thread != get_ident() for thread in threads))
                        build.assert_called_once_with()
                        conversation_factory.assert_called_once_with(engine)
                        memory_factory.assert_called_once_with(engine)
                        self.assertIsNot(engine, app.persistence.store.engine)
                        engine.dispose.assert_called_once_with()
                        app.persistence.store.engine.dispose.assert_not_called()
                        app.persistence.load_startup_memory_context.assert_not_called()
                        entries = "\n".join(app.activity.entries)
                        self.assertIn(f"Memory embedded: memory_id={first}", entries)
                        self.assertIn(f"Memory checked: memory_id={second}, duplicate=False", entries)
                        if not with_conversation:
                            extract.assert_not_called()
                            summary.assert_not_called()

    async def test_any_stage_failure_stops_entire_pass_disposes_and_logs_only_exception_class(self):
        secret = "PRIVATE transcript evidence vector credentials"
        for stage in ("extract", "summary", "embed", "dedup"):
            with self.subTest(stage=stage):
                first_chat, later_chat, first, later = uuid4(), uuid4(), uuid4(), uuid4()
                async with keyboard_tests.running_app() as (app, pilot):
                    with mocked_maintenance(app, conversations=[first_chat, later_chat],
                                            embedding_ids=[first, later], dedup_ids=[first, later]) as ctx:
                        (engine, store, memories, extract, summary, embed, dedup,
                         disposed, build, conversation_factory, memory_factory) = ctx
                        operations = {"extract": extract, "summary": summary, "embed": embed, "dedup": dedup}
                        operations[stage].side_effect = RuntimeError(secret)
                        app.summary_worker = SUMMARY_WORKER(app)
                        await asyncio.wait_for(app.summary_worker.wait(), 5)
                        await asyncio.wait_for(disposed.wait(), 5)
                        operations[stage].assert_called_once()
                        if stage in ("extract", "summary"):
                            memory_factory.assert_not_called()
                            embed.assert_not_called()
                            dedup.assert_not_called()
                            if stage == "extract":
                                summary.assert_not_called()
                        elif stage == "embed":
                            memories.get_pending_deduplication_ids.assert_not_called()
                            dedup.assert_not_called()
                        else:
                            self.assertEqual(embed.call_count, 2)
                        engine.dispose.assert_called_once_with()
                        entries = "\n".join(app.activity.entries)
                        self.assertIn("Background memory pass stopped (RuntimeError).", entries)
                        self.assertEqual(app.query_one("#activity-panel", Collapsible).title,
                                         "Activity · Memory processing failed")
                        for private in secret.split():
                            self.assertNotIn(private, entries)

    async def test_cancel_blocked_embedding_allows_inflight_finish_but_no_next_or_dedup(self):
        first, later = uuid4(), uuid4()
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release = Event()
        completed = []
        async with keyboard_tests.running_app() as (app, pilot):
            with mocked_maintenance(app, embedding_ids=[first, later], dedup_ids=[first, later]) as ctx:
                (engine, store, memories, extract, summary, embed, dedup,
                 disposed, build, conversation_factory, memory_factory) = ctx

                def blocked_embedding(memory_store, memory_id):
                    loop.call_soon_threadsafe(started.set)
                    if not release.wait(timeout=5):
                        raise TimeoutError("Test did not release embedding")
                    completed.append(memory_id)

                embed.side_effect = blocked_embedding
                try:
                    app.summary_worker = SUMMARY_WORKER(app)
                    await asyncio.wait_for(started.wait(), 5)
                    with patch.object(app, "exit") as exit_app:
                        await asyncio.wait_for(app.action_disconnect_and_quit(), 3)
                        exit_app.assert_called_once_with()
                    self.assertTrue(app.closing)
                    self.assertTrue(app.summary_worker.is_cancelled)
                    app.live.close.assert_awaited_once_with()
                    app.persistence.finish_conversation.assert_called_once_with()
                    app.persistence.close.assert_called_once_with()
                    self.assertEqual(completed, [])
                    self.assertFalse(disposed.is_set())
                    engine.dispose.assert_not_called()
                finally:
                    release.set()
                    if app.summary_worker is not None:
                        await asyncio.wait_for(disposed.wait(), 5)
                self.assertEqual(completed, [first])
                embed.assert_called_once_with(memories, first)
                memories.get_pending_deduplication_ids.assert_not_called()
                dedup.assert_not_called()
                engine.dispose.assert_called_once_with()
                entries = "\n".join(app.activity.entries)
                for unwanted in ("Memory embedded", "Memory checked", "Background memory pass stopped"):
                    self.assertNotIn(unwanted, entries)


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1",
                     "Set CORA_RUN_DB_TESTS=1 for disposable database tests")
class MemoryMaintenanceDatabaseTests(unittest.IsolatedAsyncioTestCase):
    setUpClass = classmethod(memory_tests.MemoryStoreDatabaseTests.setUpClass.__func__)
    setUp = memory_tests.MemoryStoreDatabaseTests.setUp

    async def test_completed_summary_still_retries_embedding_then_no_candidate_check_and_second_pass_is_cached(self):
        self.assertRegex(self.settings["dbname"], r"^cora_test_summaries_[0-9a-f]{32}$")
        source = self.store.save_message(
            self.conversation_id, message_id=uuid4(), role="user", text="I live in Perth.",
            channel="voice", status="completed",
        )
        self.store.close_conversation(self.conversation_id)
        summary = self.store.save_summary(self.conversation_id, "Already completed summary.")
        candidate = MemoryCandidate(source.id, "fact", "Lives in Perth", source.text)
        memory = self.memories.save_memories(self.conversation_id, [candidate])[0]
        self.assertIsNone(memory.embedding)
        self.assertIsNone(memory.dedup_checked_at)
        loop = asyncio.get_running_loop()
        disposed = asyncio.Event()
        with patch("storage.database.database_settings", return_value=self.settings):
            engine = build_engine()
        real_dispose = engine.dispose

        def dispose():
            try:
                real_dispose()
            finally:
                loop.call_soon_threadsafe(disposed.set)

        try:
            # Fixture mocks startup/live transport; real worker/services are an
            # explicit opt-in INSIDE it so its patches cannot shadow ours.
            async with keyboard_tests.running_app() as (app, pilot):
                with patch("memory.background.build_engine", return_value=engine) as build, \
                        patch.object(engine, "dispose", side_effect=dispose) as disposal, \
                        patch("memory.background.MemoryStore", side_effect=MemoryStore) as memory_factory, \
                        patch("memory.background.embed_memory", wraps=REAL_EMBED_MEMORY) as embed, \
                        patch("memory.background.deduplicate_memory", wraps=REAL_DEDUPLICATE_MEMORY) as dedup, \
                        patch("memory.background.extract_memories") as extract, \
                        patch("memory.background.summarize_conversation") as summarize, \
                        patch("memory.embed_memory.OpenAI") as embedding_api, \
                        patch("memory.deduplicate_memory.OpenAI") as dedup_api, \
                        patch("memory.extract_memories.OpenAI") as extraction_api, \
                        patch("memory.summarize.OpenAI") as summary_api:
                    client = embedding_api.return_value.__enter__.return_value
                    client.embeddings.create.return_value = embedding_tests.response_fixture()
                    self.assertEqual(self.store.get_unsummarized_conversation_ids(
                        app.persistence.conversation_id), [])
                    app.summary_worker = SUMMARY_WORKER(app)
                    await asyncio.wait_for(app.summary_worker.wait(), 5)
                    await asyncio.wait_for(disposed.wait(), 5)
                    self.assertIsNot(engine, app.persistence.store.engine)
                    saved = self.memories.get_memory(memory.id)
                    self.assertIsNotNone(saved.embedding)
                    self.assertEqual(saved.embedding_model, embedding.MEMORY_EMBEDDING_MODEL)
                    self.assertIsNone(saved.duplicate_of)
                    self.assertIsNotNone(saved.dedup_checked_at.tzinfo)
                    self.assertEqual((saved.id, saved.source_message_id, saved.kind, saved.text,
                                      saved.evidence, saved.created_at),
                                     (memory.id, source.id, memory.kind, memory.text,
                                      memory.evidence, memory.created_at))
                    self.assertEqual(embed.call_count, 1)
                    self.assertEqual(dedup.call_count, 1)
                    self.assertIs(embed.call_args.args[0].engine, engine)
                    self.assertIs(dedup.call_args.args[0], embed.call_args.args[0])
                    client.embeddings.create.assert_called_once_with(
                        model=embedding.MEMORY_EMBEDDING_MODEL, input=memory.text,
                        dimensions=1536, encoding_format="float",
                    )
                    embedding_api.assert_called_once_with(max_retries=0)
                    embedding_api.return_value.__exit__.assert_called_once_with(None, None, None)
                    disposed.clear()
                    app.summary_worker = SUMMARY_WORKER(app)
                    await asyncio.wait_for(app.summary_worker.wait(), 5)
                    await asyncio.wait_for(disposed.wait(), 5)
                    self.assertEqual(build.call_args_list, [call(), call()])
                    self.assertEqual(memory_factory.call_args_list, [call(engine), call(engine)])
                    self.assertEqual(disposal.call_count, 2)
                    self.assertEqual(embed.call_count, 1)
                    self.assertEqual(dedup.call_count, 1)
                    embedding_api.assert_called_once_with(max_retries=0)
                    client.embeddings.create.assert_called_once()
                    for mock in (extract, summarize, dedup_api, extraction_api, summary_api):
                        mock.assert_not_called()
                    again = self.memories.get_memory(memory.id)
                    self.assertEqual((again.embedding, again.embedding_created_at, again.dedup_checked_at),
                                     (saved.embedding, saved.embedding_created_at, saved.dedup_checked_at))
                    read_summary = self.store.get_summary(self.conversation_id)
                    self.assertEqual((read_summary.text, read_summary.created_at),
                                     (summary.text, summary.created_at))
                    message = self.store.get_messages(self.conversation_id)[0]
                    self.assertEqual((message.id, message.text, message.created_at),
                                     (source.id, source.text, source.created_at))
                    entries = "\n".join(app.activity.entries)
                    for private in (source.text, candidate.text, summary.text):
                        self.assertNotIn(private, entries)
                    app.persistence.load_startup_memory_context.assert_not_called()
        finally:
            real_dispose()


if __name__ == "__main__":
    unittest.main()