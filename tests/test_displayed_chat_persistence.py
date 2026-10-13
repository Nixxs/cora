import asyncio
import os
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, call, patch
from uuid import uuid4

import psycopg
from psycopg import sql

from ui.conversation import Conversation
from storage.conversation_store import ConversationStore
from storage.database import build_engine
from storage.keyboard_persistence import KeyboardPersistence
from migrate import apply_migrations, database_settings
from test_keyboard_persistence import running_app, submit
from test_written_answers import CODE, output_event


class DisplayedChatTests(unittest.TestCase):
    def setUp(self):
        self.record = MagicMock()
        self.timer = MagicMock(return_value=MagicMock())
        self.conversation = Conversation(MagicMock(), self.timer, MagicMock(), self.record)

    def test_voice_chunks_save_once_on_display_flush_not_each_delta_or_redraw(self):
        conversation = self.conversation
        conversation.append_transcript("You", "Hel")
        conversation.append_transcript("You", "lo there. ")
        conversation.redraw_log()
        self.record.assert_not_called()
        self.timer.call_args.args[1]()
        conversation.flush_transcript()
        conversation.redraw_log()
        self.record.assert_called_once_with("user", "Hello there.", "voice")
        conversation.append_transcript("Cora", "Hi.")
        conversation.write_log("You: Already saved keyboard text")
        conversation.write_log("")
        self.assertEqual(self.record.call_args_list, [
            call("user", "Hello there.", "voice"), call("assistant", "Hi.", "voice"),
        ])

    def test_only_visible_written_answers_save_without_routing_marker_or_duplicates(self):
        conversation = self.conversation
        for event in (
            output_event("plain", "A spoken clarification."),
            output_event("commentary", CODE, phase="commentary"),
            output_event("partial", CODE, status="incomplete"),
        ):
            conversation.display_written_item(event.event["item"])
        conversation.display_written_item({"id": "tool", "type": "function_call"})
        self.record.assert_not_called()
        answer = output_event("written", "<!-- cora:screen -->\n" + CODE).event["item"]
        conversation.display_written_item(answer)
        conversation.display_written_item(answer)
        conversation.redraw_log()
        self.record.assert_called_once_with("assistant", CODE, "screen")

    def test_blank_transcript_is_not_saved(self):
        self.conversation.append_transcript("You", "  ")
        self.conversation.flush_transcript()
        self.record.assert_not_called()

    def test_adapter_reuses_keyboard_conversation_and_marks_voice_as_provisional(self):
        store = MagicMock()
        persistence = KeyboardPersistence(store)
        typed, voice, screen = uuid4(), uuid4(), uuid4()
        persistence.save_message(typed, "Typed input")
        persistence.save_chat_message(voice, "user", "Spoken input", "voice")
        persistence.save_chat_message(screen, "assistant", CODE, "screen")
        self.assertEqual(store.save_message.call_args_list, [
            call(persistence.conversation_id, message_id=typed, role="user", text="Typed input",
                 channel="keyboard", status="completed", delivery_status="pending"),
            call(persistence.conversation_id, message_id=voice, role="user", text="Spoken input",
                 channel="voice", status="incomplete"),
            call(persistence.conversation_id, message_id=screen, role="assistant", text=CODE,
                 channel="screen", status="completed"),
        ])


class DisplayedChatAppTests(unittest.IsolatedAsyncioTestCase):
    async def test_app_saves_both_voice_roles_and_screen_text_without_resaving_keyboard(self):
        async with running_app() as (app, pilot):
            await submit(app, pilot, "A typed question")
            app.handle_live_event(SimpleNamespace(type="session.input_transcript.delta", delta="Hel"))
            app.handle_live_event(SimpleNamespace(type="session.input_transcript.delta", delta="lo."))
            app.handle_live_event(SimpleNamespace(type="session.output_transcript.delta", delta="Hi."))
            app.conversation.flush_transcript()
            app.handle_live_event(output_event("ordinary", "Hi."))
            answer = output_event("screen", CODE)
            app.handle_live_event(answer)
            app.handle_live_event(answer)
            app.activity.write("Not conversation data", "Ready")
            app.conversation.redraw_log()
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            app.persistence.save_message.assert_called_once()
            saved = app.persistence.save_chat_message.call_args_list
            self.assertEqual([entry.args[1:] for entry in saved], [
                ("user", "Hello.", "voice"), ("assistant", "Hi.", "voice"),
                ("assistant", CODE, "screen"),
            ])
            self.assertEqual(len({entry.args[0] for entry in saved}), 3)

    async def test_save_failure_keeps_display_and_does_not_leak_private_errors(self):
        async with running_app() as (app, pilot):
            app.persistence.save_chat_message.side_effect = RuntimeError("private SQL and text")
            app.conversation.append_transcript("Cora", "Still visible.")
            app.conversation.flush_transcript()
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            self.assertEqual(app.conversation.log_entries, ["Cora: Still visible.", ""])
            self.assertIn("Chat save failed", "\n".join(app.activity.entries))
            self.assertNotIn("private SQL and text", "\n".join(app.activity.entries))
            app.live.send_text.assert_not_called()

    async def test_shutdown_flushes_final_text_and_waits_for_saves_before_engine_close(self):
        async with running_app() as (app, pilot):
            started = asyncio.Event()
            release = Event()
            loop = asyncio.get_running_loop()
            order = []

            def save(message_id, role, text, channel):
                if role == "user":
                    loop.call_soon_threadsafe(started.set)
                    if not release.wait(timeout=5):
                        raise RuntimeError("Test did not release save")
                order.append(text)

            app.persistence.save_chat_message.side_effect = save
            app.persistence.finish_conversation.side_effect = lambda: order.append("conversation closed")
            app.persistence.close.side_effect = lambda: order.append("engine closed")
            app.conversation.append_transcript("You", "Wait for the save.")
            app.conversation.flush_transcript()
            app.conversation.append_transcript("Cora", "Last answer")

            async def close_live():
                app.handle_live_event(SimpleNamespace(type="session.output_transcript.delta", delta="."))

            app.live.close.side_effect = close_live
            try:
                await asyncio.wait_for(started.wait(), 2)
                await pilot.click("#activity-panel")
                with patch.object(app, "exit") as exit_app:
                    close = asyncio.create_task(app.action_disconnect_and_quit())
                    await asyncio.sleep(0)
                    app.persistence.finish_conversation.assert_not_called()
                    app.persistence.close.assert_not_called()
                    release.set()
                    await asyncio.wait_for(close, 3)
                    self.assertEqual(order, [
                        "Wait for the save.", "Last answer.", "conversation closed", "engine closed",
                    ])
                    self.assertTrue(app.chat_recording_closed)
                    app.record_chat_message("assistant", "Too late", "voice")
                    self.assertEqual(app.persistence.save_chat_message.call_count, 2)
                    exit_app.assert_called_once()
            finally:
                release.set()


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class DisplayedChatDatabaseTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        settings = database_settings()
        admin = psycopg.connect(**settings, autocommit=True)
        cls.addClassCleanup(admin.close)
        database = "cora_test_displayed_" + uuid4().hex
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
        cls.addClassCleanup(admin.execute, sql.SQL("DROP DATABASE {}").format(sql.Identifier(database)))
        cls.settings = {**settings, "dbname": database}
        with psycopg.connect(**cls.settings, autocommit=True) as connection:
            apply_migrations(connection)
        with patch("storage.database.database_settings", return_value=cls.settings):
            cls.engine = build_engine()
        cls.addClassCleanup(cls.engine.dispose)

    async def test_visible_chat_commits_to_one_conversation_and_survives_engine_reopen(self):
        persistence = KeyboardPersistence(ConversationStore(self.engine))
        async with running_app(persistence) as (app, pilot):
            await submit(app, pilot, "Typed input")
            app.handle_live_event(SimpleNamespace(type="session.input_transcript.delta", delta="Spoken input"))
            app.handle_live_event(SimpleNamespace(type="session.output_transcript.delta", delta="Here is the code."))
            answer = output_event("written", CODE)
            app.handle_live_event(answer)
            app.handle_live_event(answer)
            with patch.object(app, "exit"):
                await app.action_disconnect_and_quit()
        self.engine.dispose()
        with patch("storage.database.database_settings", return_value=self.settings):
            reopened = build_engine()
        try:
            conversation = ConversationStore(reopened).get_conversation(persistence.conversation_id)
            self.assertEqual(conversation.status, "closed")
            self.assertIsNotNone(conversation.ended_at.tzinfo)
            records = ConversationStore(reopened).get_messages(persistence.conversation_id)
            self.assertEqual([(record.role, record.text, record.channel, record.status) for record in records], [
                ("user", "Typed input", "keyboard", "completed"),
                ("user", "Spoken input", "voice", "incomplete"),
                ("assistant", CODE, "screen", "completed"),
                ("assistant", "Here is the code.", "voice", "incomplete"),
            ])
            self.assertEqual([record.sequence_number for record in records], [1, 2, 3, 4])
            self.assertEqual(records[0].delivery_status, "sent")
            self.assertTrue(all(record.delivery_status == "not_applicable" for record in records[1:]))
            self.assertGreaterEqual(conversation.ended_at, max(record.created_at for record in records))
        finally:
            reopened.dispose()


if __name__ == "__main__":
    unittest.main()