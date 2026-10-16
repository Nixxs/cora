import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from threading import Event, get_ident
import unittest
from unittest.mock import AsyncMock, MagicMock, call, patch
from uuid import UUID, uuid4

from sqlalchemy import text
from textual.widgets import Collapsible

from app import CoraApp
from storage.conversation_store import ConversationStore
from storage.database import build_engine
from storage.keyboard_persistence import KeyboardPersistence
from ui.composer import ChatComposer
import test_conversation_summaries as summary_tests
import test_keyboard_persistence as keyboard_tests
import test_summarize as summarize_tests


# running_app patches both decorated methods; keep the originals before entry.
SUMMARY_WORKER = CoraApp.summarize_previous_conversations
CONNECT_WORKER = CoraApp.connect_to_cora


@asynccontextmanager
async def mocked_pass(app, ids, *, blocked=False):
    loop = asyncio.get_running_loop()
    started, disposed = asyncio.Event(), asyncio.Event()
    release = Event()
    engine = MagicMock()
    store = MagicMock(spec=ConversationStore)
    store.get_unsummarized_conversation_ids.return_value = ids
    threads = []

    def summarize(store_arg, conversation_id):
        threads.append(get_ident())
        loop.call_soon_threadsafe(started.set)
        if blocked and not release.wait(timeout=5):
            raise TimeoutError("Test did not release summary")

    engine.dispose.side_effect = lambda: loop.call_soon_threadsafe(disposed.set)
    with patch("memory.background.build_engine", return_value=engine) as build, \
            patch("memory.background.ConversationStore", return_value=store) as construct, \
            patch("memory.background.summarize_conversation", side_effect=summarize) as summary, \
            patch("memory.background.extract_memories", return_value=[]) as extraction:
        summary.extraction = extraction
        try:
            yield engine, store, summary, started, disposed, threads, build, construct
        finally:
            release.set()
            # Worker.wait() after cancellation does not join its executor thread.
            if app.summary_worker is not None:
                await asyncio.wait_for(disposed.wait(), 5)


class BackgroundSummaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_or_ordered_pass_uses_existing_callable_off_thread_and_disposes(self):
        main_thread = get_ident()
        for ids in ([], [uuid4(), uuid4()]):
            with self.subTest(ids=ids):
                async with keyboard_tests.running_app() as (app, pilot):
                    async with mocked_pass(app, ids) as context:
                        engine, store, summary, started, disposed, threads, build, construct = context
                        app.summary_worker = SUMMARY_WORKER(app)
                        await asyncio.wait_for(app.summary_worker.wait(), 5)
                        await asyncio.wait_for(disposed.wait(), 5)
                        self.assertEqual(app.summary_worker.group, "memory-summary")
                        build.assert_called_once_with()
                        construct.assert_called_once_with(engine)
                        store.get_unsummarized_conversation_ids.assert_called_once_with(
                            app.persistence.conversation_id,
                        )
                        self.assertEqual(summary.call_args_list, [call(store, id_) for id_ in ids])
                        self.assertEqual(summary.extraction.call_args_list, [call(store, id_) for id_ in ids])
                        self.assertEqual(len(threads), len(ids))
                        self.assertTrue(all(thread != main_thread for thread in threads))
                        engine.dispose.assert_called_once_with()
                        entries = "\n".join(app.activity.entries)
                        for id_ in ids:
                            self.assertIn(f"Summary saved: conversation_id={id_}", entries)

    async def test_pass_failure_stops_later_ids_redacts_details_and_disposes(self):
        first, later = uuid4(), uuid4()
        async with keyboard_tests.running_app() as (app, pilot):
            async with mocked_pass(app, [first, later]) as context:
                engine, store, summary, started, disposed, threads, build, construct = context
                summary.side_effect = RuntimeError("private transcript, SQL parameters, API credentials")
                app.summary_worker = SUMMARY_WORKER(app)
                await asyncio.wait_for(app.summary_worker.wait(), 5)
                await asyncio.wait_for(disposed.wait(), 5)
                summary.assert_called_once_with(store, first)
                engine.dispose.assert_called_once_with()
                entries = "\n".join(app.activity.entries)
                self.assertIn("Background memory pass stopped (RuntimeError).", entries)
                for secret in ("private transcript", "SQL parameters", "API credentials"):
                    self.assertNotIn(secret, entries)

    async def test_automatic_startup_live_keyboard_and_ui_proceed_while_summary_blocked(self):
        live_started, live_release = asyncio.Event(), asyncio.Event()
        async with keyboard_tests.running_app() as (app, pilot):
            app.persistence.mark_interrupted_conversations.return_value = 0

            async def live_run(*args):
                app.persistence.mark_interrupted_conversations.assert_called_once()
                live_started.set()
                await live_release.wait()

            app.live.run = AsyncMock(side_effect=live_run)
            try:
                async with mocked_pass(app, [uuid4()], blocked=True) as context:
                    engine, store, summary, started, disposed, threads, build, construct = context
                    with patch.object(CoraApp, "summarize_previous_conversations", SUMMARY_WORKER):
                        app.connection_worker = CONNECT_WORKER(app)
                        await asyncio.wait_for(started.wait(), 5)
                        await asyncio.wait_for(live_started.wait(), 5)
                        app.persistence.mark_interrupted_conversations.assert_called_once()
                        self.assertFalse(disposed.is_set())
                        await pilot.click("#activity-panel")
                        self.assertFalse(app.query_one("#activity-panel", Collapsible).collapsed)
                        composer = app.query_one("#chat-input", ChatComposer)
                        composer.load_text("Keyboard remains responsive")
                        composer.focus()
                        await pilot.press("enter")
                        await asyncio.wait_for(app.keyboard_worker.wait(), 5)
                        app.live.send_text.assert_awaited_once_with("Keyboard remains responsive")
                        app.persistence.save_message.assert_called_once()
                        app.persistence.record_delivery.assert_called_once()
                        self.assertEqual(composer.text, "")
                        self.assertFalse(composer.disabled)
                        self.assertIn("You: Keyboard remains responsive", app.conversation.log_entries)
                        self.assertFalse(disposed.is_set())
            finally:
                live_release.set()
                if app.connection_worker is not None:
                    await asyncio.wait_for(app.connection_worker.wait(), 5)

    async def test_shutdown_does_not_wait_for_inflight_summary_or_start_next_conversation(self):
        first, later = uuid4(), uuid4()
        async with keyboard_tests.running_app() as (app, pilot):
            async with mocked_pass(app, [first, later], blocked=True) as context:
                engine, store, summary, started, disposed, threads, build, construct = context
                self.assertIsNot(engine, app.persistence.store.engine)
                app.summary_worker = SUMMARY_WORKER(app)
                await asyncio.wait_for(started.wait(), 5)
                with patch.object(app, "exit") as exit_app:
                    await asyncio.wait_for(app.action_disconnect_and_quit(), 3)
                    exit_app.assert_called_once_with()
                self.assertTrue(app.closing)
                self.assertTrue(app.summary_worker.is_cancelled)
                app.live.close.assert_awaited_once()
                app.persistence.finish_conversation.assert_called_once_with()
                app.persistence.close.assert_called_once_with()
                self.assertFalse(disposed.is_set())
                engine.dispose.assert_not_called()
                summary.assert_called_once_with(store, first)
            # The helper releases and joins via disposal, not cancelled Worker.wait().
            engine.dispose.assert_called_once_with()
            summary.assert_called_once_with(store, first)
            entries = "\n".join(app.activity.entries)
            self.assertNotIn("Summary saved", entries)
            self.assertNotIn("Memory processing failed", entries)


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class BackgroundSummaryDatabaseTests(unittest.IsolatedAsyncioTestCase):
    # Borrow disposable database setup only; module aliases avoid duplicate discovery.
    setUpClass = classmethod(summary_tests.SummaryDatabaseTests.setUpClass.__func__)
    setUp = summary_tests.SummaryDatabaseTests.setUp

    async def test_query_filters_eligibility_and_orders_by_start_then_id(self):
        old, tie_first, tie_last = uuid4(), UUID(int=1), UUID(int=2)
        current, empty_closed, empty_interrupted, summarised = [uuid4() for _ in range(4)]

        def add_message(conversation_id):
            self.store.save_message(
                conversation_id, message_id=uuid4(), role="user",
                text="Saved historical chunk", channel="voice", status="incomplete",
            )

        self.store.create_conversation(tie_first)
        add_message(tie_first)
        self.store.create_conversation(empty_interrupted)
        self.store.interrupt_active_conversations(self.conversation_id)
        add_message(self.conversation_id)  # Nonempty active row remains ineligible.
        for conversation_id in (old, tie_last, current, empty_closed, summarised):
            self.store.create_conversation(conversation_id)
            if conversation_id != empty_closed:
                add_message(conversation_id)
            self.store.close_conversation(conversation_id)
        self.store.save_summary(summarised, "Already summarised")
        past = datetime(2020, 1, 1, tzinfo=timezone.utc)
        with self.engine.begin() as connection:
            for conversation_id, started_at in (
                (old, past), (tie_first, past + timedelta(days=1)),
                (tie_last, past + timedelta(days=1)),
            ):
                connection.execute(text(
                    "UPDATE public.conversations SET started_at = :started_at WHERE id = :id",
                ), {"id": conversation_id, "started_at": started_at})
        self.assertEqual(
            self.store.get_unsummarized_conversation_ids(current), [old, tie_first, tie_last],
        )
        self.assertEqual(self.store.get_conversation(self.conversation_id).status, "active")
        self.assertEqual(self.store.get_conversation(empty_interrupted).status, "interrupted")

    async def test_startup_tags_previous_run_commits_summary_and_second_pass_skips_model(self):
        previous = self.conversation_id
        saved_message = self.store.save_message(
            previous, message_id=uuid4(), role="user", text="Prefer small development steps.",
            channel="voice", status="incomplete",
        )
        current = KeyboardPersistence(self.store)
        # A separate real engine, configured ONLY for the disposable fixture database.
        with patch("storage.database.database_settings", return_value=self.settings):
            background_engine = build_engine()
        loop = asyncio.get_running_loop()
        disposed = asyncio.Event()
        real_dispose = background_engine.dispose
        threads = []

        def dispose():
            real_dispose()
            loop.call_soon_threadsafe(disposed.set)

        def model_response(**request):
            threads.append(get_ident())
            return summarize_tests.response_fixture("The user prefers small development steps.")

        try:
            with patch("memory.background.build_engine", return_value=background_engine) as build, \
                    patch.object(background_engine, "dispose", side_effect=dispose), \
                    patch("memory.summarize.OpenAI") as factory, \
                    patch("memory.background.extract_memories", return_value=[]) as extraction:
                client = MagicMock()
                client.responses.create.side_effect = model_response
                factory.return_value.__enter__.return_value = client
                async with keyboard_tests.running_app(current) as (app, pilot):
                    app.live.run = AsyncMock()
                    try:
                        with patch.object(CoraApp, "summarize_previous_conversations", SUMMARY_WORKER):
                            app.connection_worker = CONNECT_WORKER(app)
                            await asyncio.wait_for(app.connection_worker.wait(), 5)
                            await asyncio.wait_for(disposed.wait(), 5)
                        app.live.run.assert_awaited_once()
                        self.assertIsNot(background_engine, self.engine)
                        conversation = self.store.get_conversation(previous)
                        self.assertEqual(conversation.status, "interrupted")
                        self.assertIsNone(conversation.ended_at)
                        first = self.store.get_summary(previous)
                        self.assertIsNotNone(first)
                        self.assertEqual(first.text, "The user prefers small development steps.")
                        self.assertIsNone(self.store.get_conversation(current.conversation_id))
                        payload = json.loads(client.responses.create.call_args.kwargs["input"])
                        self.assertEqual(payload["conversation_status"], "interrupted")
                        self.assertEqual(payload["messages"][0]["status"], "incomplete")
                        self.assertNotEqual(threads[0], get_ident())
                        disposed.clear()
                        app.summary_worker = SUMMARY_WORKER(app)
                        await asyncio.wait_for(disposed.wait(), 5)
                        self.assertEqual(build.call_count, 2)
                        client.responses.create.assert_called_once()
                        factory.assert_called_once_with(max_retries=0)
                        factory.return_value.__exit__.assert_called_once_with(None, None, None)
                        extraction.assert_called_once_with(unittest.mock.ANY, previous)
                        again = self.store.get_summary(previous)
                        self.assertEqual((again.text, again.created_at), (first.text, first.created_at))
                    finally:
                        if app.summary_worker is not None:
                            await asyncio.wait_for(disposed.wait(), 5)
                        with patch.object(app, "exit"):
                            await app.action_disconnect_and_quit()
                    self.assertIsNone(self.store.get_conversation(current.conversation_id))
            message = self.store.get_messages(previous)[0]
            self.assertEqual((message.id, message.text, message.status),
                             (saved_message.id, saved_message.text, saved_message.status))
        finally:
            real_dispose()


if __name__ == '__main__':
    unittest.main()