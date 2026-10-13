"""Background memory plumbing; model clients are mocked, DB tests are opt-in."""

import asyncio
import json
import os
from threading import Event, get_ident
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, call, patch
from uuid import uuid4

from textual.widgets import Collapsible

from app import CoraApp
from storage.database import build_engine
from storage.models import MemoryRecord
import test_background_summaries as background_tests
import test_keyboard_persistence as keyboard_tests
import test_memory_store as memory_tests


# running_app replaces decorated methods: capture these before entering it.
SUMMARY_WORKER = CoraApp.summarize_previous_conversations
STARTUP_WORKER = CoraApp.check_interrupted_conversations


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
            with patch("app.build_engine", return_value=background_engine) as build, \
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


if __name__ == "__main__":
    unittest.main()