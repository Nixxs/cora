import asyncio
from contextlib import asynccontextmanager
import os
from threading import Event
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import psycopg
from psycopg import sql
from textual.widgets import Collapsible

from app import CoraApp
from composer import ChatComposer
from conversation_store import ConversationStore
from database import build_engine
from keyboard_persistence import KeyboardPersistence
from migrate import apply_migrations, database_settings


class KeyboardPersistenceTests(unittest.TestCase):
    def test_adapter_is_lazy_owns_its_engine_and_reuses_conversation_id(self):
        engine = MagicMock()
        store = MagicMock()
        with patch("keyboard_persistence.build_engine", return_value=engine) as build, \
                patch("keyboard_persistence.ConversationStore", return_value=store):
            persistence = KeyboardPersistence()
            build.assert_not_called()
            message_id = uuid4()
            persistence.save_message(message_id, "Hello")
            persistence.save_message(message_id, "Hello")
            build.assert_called_once()
            store.create_conversation.assert_called_with(persistence.conversation_id)
            store.save_message.assert_called_with(
                persistence.conversation_id, message_id=message_id, role="user", text="Hello",
                channel="keyboard", status="completed", delivery_status="pending",
            )
            persistence.record_delivery(message_id, "sent")
            store.set_delivery_status.assert_called_once_with(persistence.conversation_id, message_id, "sent")
            persistence.close()
            persistence.close()
            engine.dispose.assert_called_once()

    def test_injected_store_engine_is_not_disposed_and_no_early_delivery_write(self):
        store = MagicMock()
        persistence = KeyboardPersistence(store)
        persistence.close()
        store.engine.dispose.assert_not_called()
        with self.assertRaises(RuntimeError):
            KeyboardPersistence().record_delivery(uuid4(), "sent")

    def test_finish_closes_existing_conversation_without_initialising_unused_storage(self):
        with patch("keyboard_persistence.build_engine") as build:
            unused = KeyboardPersistence()
            unused.finish_conversation()
            unused.close()
            build.assert_not_called()
        store = MagicMock()
        persistence = KeyboardPersistence(store)
        persistence.finish_conversation()
        store.close_conversation.assert_called_once_with(persistence.conversation_id)
        store.create_conversation.assert_not_called()
        store.engine.dispose.assert_not_called()

    def test_startup_check_opens_storage_without_creating_current_conversation(self):
        engine = MagicMock()
        store = MagicMock()
        store.interrupt_active_conversations.return_value = 2
        with patch("keyboard_persistence.build_engine", return_value=engine) as build, \
                patch("keyboard_persistence.ConversationStore", return_value=store):
            persistence = KeyboardPersistence()
            self.assertEqual(persistence.mark_interrupted_conversations(), 2)
            store.interrupt_active_conversations.assert_called_once_with(persistence.conversation_id)
            store.create_conversation.assert_not_called()
            persistence.save_message(uuid4(), "First message after startup")
            build.assert_called_once()
            store.create_conversation.assert_called_once_with(persistence.conversation_id)
            persistence.close()
            engine.dispose.assert_called_once()


@asynccontextmanager
async def running_app(persistence=None):
    with patch.object(CoraApp, "connect_to_cora", return_value=None):
        app = CoraApp()
        async with app.run_test(size=(90, 35)) as pilot:
            if persistence is None:
                persistence = MagicMock()
                persistence.conversation_id = uuid4()
            app.persistence = persistence
            app.live.send_text = AsyncMock()
            app.live.close = AsyncMock()
            yield app, pilot


async def submit(app, pilot, message):
    app.query_one("#chat-input", ChatComposer).load_text(message)
    await pilot.press("enter")
    await asyncio.wait_for(app.workers.wait_for_complete(), 3)
    await pilot.pause()


class KeyboardSubmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_save_precedes_send_and_delivery_write_then_draft_clears(self):
        async with running_app() as (app, pilot):
            order = MagicMock()
            order.attach_mock(app.persistence.save_message, "save")
            order.attach_mock(app.live.send_text, "send")
            order.attach_mock(app.persistence.record_delivery, "delivery")
            await submit(app, pilot, "Hello Cora")
            self.assertEqual([call[0] for call in order.mock_calls], ["save", "send", "delivery"])
            message_id = app.persistence.save_message.call_args.args[0]
            app.persistence.record_delivery.assert_called_once_with(message_id, "sent")
            self.assertEqual(app.query_one("#chat-input", ChatComposer).text, "")
            self.assertEqual(app.conversation.log_entries, ["You: Hello Cora", ""])
            self.assertIsNone(app.pending_keyboard_message)

    async def test_database_failure_keeps_draft_redacts_error_and_reuses_id_on_retry(self):
        async with running_app() as (app, pilot):
            app.persistence.save_message.side_effect = RuntimeError("private connection details")
            await submit(app, pilot, "Keep this draft")
            app.live.send_text.assert_not_called()
            self.assertEqual(app.query_one("#chat-input", ChatComposer).text, "Keep this draft")
            self.assertFalse(app.query_one("#chat-input", ChatComposer).disabled)
            first_id = app.persistence.save_message.call_args.args[0]
            self.assertNotIn("private connection details", "\n".join(app.activity.entries))
            app.persistence.save_message.side_effect = None
            await submit(app, pilot, "Keep this draft")
            self.assertEqual(app.persistence.save_message.call_args.args[0], first_id)
            app.live.send_text.assert_awaited_once_with("Keep this draft")

    async def test_send_failure_records_failed_and_keeps_draft_without_auto_resend(self):
        async with running_app() as (app, pilot):
            app.live.send_text.side_effect = RuntimeError("Offline")
            await submit(app, pilot, "Keep after send failure")
            message_id = app.persistence.save_message.call_args.args[0]
            app.persistence.record_delivery.assert_called_once_with(message_id, "failed")
            self.assertEqual(app.query_one("#chat-input", ChatComposer).text, "Keep after send failure")
            self.assertEqual(app.conversation.log_entries, [])
            app.live.send_text.assert_awaited_once()

    async def test_delivery_write_failure_does_not_keep_already_sent_draft(self):
        async with running_app() as (app, pilot):
            app.persistence.record_delivery.side_effect = RuntimeError("private SQL parameters")
            await submit(app, pilot, "Already sent")
            app.live.send_text.assert_awaited_once_with("Already sent")
            self.assertEqual(app.query_one("#chat-input", ChatComposer).text, "")
            self.assertEqual(app.conversation.log_entries, ["You: Already sent", ""])
            entries = "\n".join(app.activity.entries)
            self.assertIn("could not".lower(), entries.lower())
            self.assertNotIn("private SQL parameters", entries)

    async def test_slow_database_does_not_block_ui_and_shutdown_waits_without_sending(self):
        async with running_app() as (app, pilot):
            started = asyncio.Event()
            release = Event()
            loop = asyncio.get_running_loop()

            def slow_save(*args):
                loop.call_soon_threadsafe(started.set)
                if not release.wait(timeout=5):
                    raise RuntimeError("Test did not release database work")

            app.persistence.save_message.side_effect = slow_save
            composer = app.query_one("#chat-input", ChatComposer)
            composer.load_text("Wait for this save")
            try:
                await pilot.press("enter")
                await asyncio.wait_for(started.wait(), 2)
                await pilot.click("#activity-panel")
                self.assertFalse(app.query_one("#activity-panel", Collapsible).collapsed)
                with patch.object(app, "exit") as exit_app:
                    close = asyncio.create_task(app.action_disconnect_and_quit())
                    await asyncio.sleep(0)
                    app.live.close.assert_not_called()
                    app.persistence.close.assert_not_called()
                    release.set()
                    await asyncio.wait_for(close, 3)
                    app.live.close.assert_awaited_once()
                    app.persistence.close.assert_called_once()
                    app.live.send_text.assert_not_called()
                    exit_app.assert_called_once()
            finally:
                release.set()

    async def test_shutdown_closes_conversation_once_before_disposal(self):
        async with running_app() as (app, pilot):
            order = MagicMock()
            order.attach_mock(app.live.close, "live_close")
            order.attach_mock(app.persistence.finish_conversation, "finish")
            order.attach_mock(app.persistence.close, "dispose")
            with patch.object(app, "exit") as exit_app:
                await app.action_disconnect_and_quit()
                await app.action_disconnect_and_quit()
                self.assertEqual([entry[0] for entry in order.mock_calls], ["live_close", "finish", "dispose"])
                exit_app.assert_called_once()

    async def test_failed_conversation_close_still_disposes_and_exits_without_leaking_error(self):
        async with running_app() as (app, pilot):
            app.persistence.finish_conversation.side_effect = RuntimeError("private credentials and SQL")
            with patch.object(app, "exit") as exit_app:
                await app.action_disconnect_and_quit()
                app.persistence.close.assert_called_once()
                exit_app.assert_called_once()
            entries = "\n".join(app.activity.entries)
            self.assertIn("Conversation close failed (RuntimeError)", entries)
            self.assertNotIn("private credentials and SQL", entries)


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class KeyboardDatabaseTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        settings = database_settings()
        admin = psycopg.connect(**settings, autocommit=True)
        cls.addClassCleanup(admin.close)
        database = "cora_test_keyboard_" + uuid4().hex
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
        cls.addClassCleanup(admin.execute, sql.SQL("DROP DATABASE {}").format(sql.Identifier(database)))
        cls.settings = {**settings, "dbname": database}
        with psycopg.connect(**cls.settings, autocommit=True) as connection:
            apply_migrations(connection)
        with patch("database.database_settings", return_value=cls.settings):
            cls.engine = build_engine()
        cls.addClassCleanup(cls.engine.dispose)

    async def test_retry_after_committed_save_error_does_not_duplicate_real_message(self):
        store = ConversationStore(self.engine)
        persistence = KeyboardPersistence(store)
        original_save = persistence.save_message

        def committed_but_unconfirmed(message_id, message):
            original_save(message_id, message)
            raise RuntimeError("Save acknowledgement lost")

        async with running_app(persistence) as (app, pilot):
            with patch.object(persistence, "save_message", side_effect=committed_but_unconfirmed):
                await submit(app, pilot, "Retry the uncertain save")
            app.live.send_text.assert_not_called()
            first = store.get_messages(persistence.conversation_id)[0]
            await submit(app, pilot, "Retry the uncertain save")
            app.live.send_text.assert_awaited_once()
            records = store.get_messages(persistence.conversation_id)
            self.assertEqual(len(records), 1)
            self.assertEqual((records[0].id, records[0].created_at), (first.id, first.created_at))
            self.assertEqual(records[0].delivery_status, "sent")

    async def test_real_app_submission_commits_before_send_and_reads_back_after_close(self):
        store = ConversationStore(self.engine)
        persistence = KeyboardPersistence(store)
        async with running_app(persistence) as (app, pilot):
            async def check_saved_before_send(message):
                records = await asyncio.to_thread(store.get_messages, persistence.conversation_id)
                self.assertEqual(records[-1].text, message)
                self.assertEqual(records[-1].delivery_status, "pending")

            app.live.send_text.side_effect = check_saved_before_send
            await submit(app, pilot, "First typed message")
            await submit(app, pilot, "Second typed message")
            with patch.object(app, "exit"):
                await app.action_disconnect_and_quit()
        self.engine.dispose()
        with patch("database.database_settings", return_value=self.settings):
            reopened = build_engine()
        try:
            records = ConversationStore(reopened).get_messages(persistence.conversation_id)
            self.assertEqual([record.text for record in records], ["First typed message", "Second typed message"])
            self.assertEqual([record.sequence_number for record in records], [1, 2])
            self.assertTrue(all(record.channel == "keyboard" and record.role == "user" for record in records))
            self.assertTrue(all(record.status == "completed" and record.delivery_status == "sent" for record in records))
        finally:
            reopened.dispose()


if __name__ == "__main__":
    unittest.main()