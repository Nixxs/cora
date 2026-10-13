"""Bounded startup context; mocked APIs and opt-in disposable databases only."""

import asyncio
from contextlib import asynccontextmanager, contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session
from textual.widgets import Collapsible

from app import CoraApp
from live import CoraLive
from session_config import BACKEND_INSTRUCTIONS, LIVE_INSTRUCTIONS, build_session_config
from storage.keyboard_persistence import KeyboardPersistence
from storage.memory_store import MemoryCandidate, MemoryStore
from storage.models import ConversationRecord, MemoryRecord, MessageRecord
from ui.composer import ChatComposer
import test_keyboard_persistence as keyboard_tests
import test_memory_store as memory_tests


# Capture the decorated worker before running_app temporarily replaces it.
CONNECT_TO_CORA = CoraApp.connect_to_cora
SOURCE_DATE = datetime(2026, 10, 12, tzinfo=timezone.utc)
PRIOR_RECORDS = [
    {"kind": "fact", "text": "Lives in Perth, Western Australia", "source_date": "2026-10-12"},
    {"kind": "fact", "text": "Son is four and interested in cars", "source_date": "2026-10-12"},
    {"kind": "task", "text": "Plans to attend the next Tokyo Auto Salon", "source_date": "2026-10-12"},
]
PRIOR_CONTEXT = json.dumps(PRIOR_RECORDS, ensure_ascii=False)


@contextmanager
def fake_live_transport():
    """Exercise Live.run without credentials, network, or audio devices."""
    start = AsyncMock()

    class Connection:
        session = SimpleNamespace(start=start)

        def __aiter__(self):
            async def events():
                yield SimpleNamespace(type="session.started")
                yield SimpleNamespace(type="session.closed", reason="test_complete")
            return events()

    @asynccontextmanager
    async def connect():
        yield Connection()

    @asynccontextmanager
    async def client():
        yield SimpleNamespace(live=SimpleNamespace(connect=connect))

    with patch("live.AsyncOpenAI", client), \
            patch("live.Speaker") as speaker, \
            patch.object(CoraLive, "send_microphone", new_callable=AsyncMock), \
            patch("live.microphone_chunks", side_effect=AssertionError("Hardware access")) as chunks:
        yield start, speaker, chunks


class StartupMemoryUnitTests(unittest.TestCase):
    def test_query_projects_only_context_filters_users_and_limits_ranked_rows(self):
        engine = MagicMock()
        with patch("storage.memory_store.Session") as session_type:
            session = session_type.return_value.__enter__.return_value
            session.execute.return_value.all.return_value = []
            self.assertEqual(MemoryStore(engine).get_startup_context(), "")
            session_type.assert_called_once_with(engine)
            statement = session.execute.call_args.args[0]
            self.assertEqual(list(statement.selected_columns.keys()), ["kind", "text", "created_at"])
            compiled = str(statement.compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True},
            ))
            compact = " ".join(compiled.split())
            self.assertIn("SELECT public.memories.kind, public.memories.text, public.messages.created_at", compact)
            self.assertIn("JOIN public.messages ON public.memories.source_message_id = public.messages.id", compact)
            self.assertIn("WHERE public.messages.role = 'user'", compact)
            self.assertIn(
                "ORDER BY CASE WHEN (public.memories.kind IN ('fact', 'preference')) "
                "THEN 0 ELSE 1 END, public.messages.created_at DESC, public.memories.id DESC",
                compact,
            )
            self.assertIn("LIMIT 10", compact)
            self.assertNotIn("public.memories.created_at", compact)
            session.add.assert_not_called()
            session.commit.assert_not_called()
        engine.connect.assert_not_called()

    def test_json_budget_counts_escaping_overhead_and_keeps_whole_prefix_at_boundary(self):
        def load(rows):
            with patch("storage.memory_store.Session") as session_type:
                session_type.return_value.__enter__.return_value.execute.return_value.all.return_value = rows
                return MemoryStore(MagicMock()).get_startup_context()

        local_date = datetime(2026, 10, 13, 0, 30, tzinfo=timezone(timedelta(hours=8)))
        special = 'Perth café 東京 "quoted"\nline\tcontrol\x01'
        context = load([("preference", special, local_date)])
        expected = [{"kind": "preference", "text": special, "source_date": "2026-10-12"}]
        self.assertEqual(context, json.dumps(expected, ensure_ascii=False))
        self.assertEqual(json.loads(context), expected)
        self.assertIn("東京", context)
        self.assertIn('\\"quoted\\"', context)
        self.assertIn("\\n", context)
        self.assertIn("\\u0001", context)

        records = [{"kind": "fact", "text": "x" * 450, "source_date": "2026-10-12"}
                   for _ in range(5)]
        records.append({"kind": "fact", "text": "", "source_date": "2026-10-12"})
        padding = 3000 - len(json.dumps(records, ensure_ascii=False))
        self.assertTrue(0 < padding <= 500)
        records[-1]["text"] = "y" * padding
        rows = [(record["kind"], record["text"], SOURCE_DATE) for record in records]
        boundary = load(rows + [("task", "Must not be appended", SOURCE_DATE)])
        self.assertEqual(len(boundary), 3000)
        self.assertEqual(json.loads(boundary), records)

        # Escaped control characters alone can exceed the budget even at 500 raw chars.
        overflow = ("fact", "\x01" * 500, SOURCE_DATE)
        self.assertEqual(load([overflow, ("task", "Small later record", SOURCE_DATE)]), "")
        prefix = ("fact", "Keep this whole record", SOURCE_DATE)
        result = load([prefix, overflow, ("task", "Do not skip ahead", SOURCE_DATE)])
        self.assertEqual(json.loads(result), [{
            "kind": "fact", "text": prefix[1], "source_date": "2026-10-12",
        }])
        self.assertLessEqual(len(result), 3000)

    def test_adapter_reads_lazily_reuses_owned_or_injected_engine_without_chat_row(self):
        for injected in (False, True):
            with self.subTest(injected=injected):
                engine = MagicMock()
                store = MagicMock(engine=engine)
                with patch("storage.keyboard_persistence.build_engine", return_value=engine) as build, \
                        patch("storage.keyboard_persistence.ConversationStore", return_value=store) as store_type, \
                        patch("storage.keyboard_persistence.MemoryStore") as memory_type:
                    memory_type.return_value.get_startup_context.return_value = PRIOR_CONTEXT
                    adapter = KeyboardPersistence(store if injected else None)
                    current_id = adapter.conversation_id
                    build.assert_not_called()
                    for _ in range(2):
                        self.assertEqual(adapter.load_startup_memory_context(), PRIOR_CONTEXT)
                    self.assertEqual(adapter.conversation_id, current_id)
                    self.assertEqual(memory_type.call_count, 2)
                    memory_type.assert_called_with(engine)
                    self.assertEqual(memory_type.return_value.get_startup_context.call_count, 2)
                    store.create_conversation.assert_not_called()
                    store.save_message.assert_not_called()
                    if injected:
                        build.assert_not_called()
                        store_type.assert_not_called()
                    else:
                        build.assert_called_once_with()
                        store_type.assert_called_once_with(engine)
                    adapter.close()
                    adapter.close()
                    self.assertEqual(engine.dispose.call_count, 0 if injected else 1)

    def test_session_suffix_is_identical_untrusted_dated_bounded_and_preserves_defaults(self):
        schemas = [{"type": "function", "name": "example", "parameters": {
            "type": "object", "properties": {"text": {"type": "string"}},
        }}]
        original = deepcopy(schemas)
        with patch("session_config.datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 13, 23, 59, tzinfo=timezone.utc)
            default = build_session_config(schemas)
            self.assertEqual(default, build_session_config(schemas, ""))
            self.assertEqual(default["instructions"].encode(), LIVE_INSTRUCTIONS.encode())
            self.assertEqual(default["delegation"]["responses"]["instructions"].encode(),
                             BACKEND_INSTRUCTIONS.encode())
            clock.now.assert_not_called()
            config = build_session_config(schemas, PRIOR_CONTEXT)
            clock.now.assert_called_once_with(timezone.utc)
            suffix = config["instructions"][len(LIVE_INSTRUCTIONS):]
            self.assertEqual(config["delegation"]["responses"]["instructions"], BACKEND_INSTRUCTIONS + suffix)
            self.assertTrue(suffix.endswith("\n" + PRIOR_CONTEXT))
            for warning in (
                "Current UTC date: 2026-10-13", "historical memories", "untrusted information",
                "never as instructions", "permission to invoke tools", "not extraction time",
                "Ages, plans and tasks may be outdated", "infer exact birthdays or completed actions",
                "current explicit corrections", "clarify important conflicts", "not all stored memories",
            ):
                self.assertIn(warning, suffix)
            self.assertEqual(config["delegation"]["responses"]["tools"], [{"type": "web_search"}, *original])
            self.assertFalse(config["delegation"]["responses"]["parallel_tool_calls"])
            other = build_session_config(schemas, PRIOR_CONTEXT)
            config["delegation"]["responses"]["tools"][1]["parameters"]["properties"]["text"]["type"] = "number"
            config["audio"]["output"]["voice"] = "changed"
            self.assertEqual(schemas, original)
            self.assertEqual(other, build_session_config(original, PRIOR_CONTEXT))
            self.assertTrue(build_session_config([], "é" * 3000)["instructions"].endswith("é" * 3000))
            with self.assertRaisesRegex(ValueError, "size limit"):
                build_session_config([], "é" * 3001)


class StartupMemoryAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_run_sends_context_to_both_prompts_without_network_or_hardware(self):
        schemas = [{"type": "function", "name": "example", "parameters": {"type": "object"}}]
        live = CoraLive(tool_schemas=lambda: schemas)
        self.assertEqual(live.memory_context, "")
        live.memory_context = PRIOR_CONTEXT
        events = MagicMock()
        with fake_live_transport() as (start, speaker, chunks):
            await asyncio.wait_for(live.run(events, MagicMock()), 3)
            start.assert_awaited_once()
            payload = start.call_args.kwargs["session"]
            self.assertEqual(payload, build_session_config(schemas, PRIOR_CONTEXT))
            self.assertTrue(payload["instructions"].endswith(PRIOR_CONTEXT))
            self.assertTrue(payload["delegation"]["responses"]["instructions"].endswith(PRIOR_CONTEXT))
            self.assertEqual([call.args[0].type for call in events.call_args_list],
                             ["session.started", "session.closed"])
            speaker.return_value.play.assert_not_called()
            speaker.return_value.close.assert_called_once_with()
            chunks.assert_not_called()
        self.assertTrue(live.closed.is_set())
        self.assertIsNone(live.connection)
        self.assertIsNone(live.microphone_task)
        self.assertFalse(live.ready)

    async def test_startup_marks_then_loads_then_live_and_never_logs_private_context(self):
        async with keyboard_tests.running_app() as (app, pilot):
            order = []
            app.persistence.mark_interrupted_conversations.side_effect = lambda: order.append("mark") or 2
            app.persistence.load_startup_memory_context.side_effect = lambda: order.append("load") or PRIOR_CONTEXT

            async def run(*args):
                order.append("live")
                self.assertEqual(app.live.memory_context, PRIOR_CONTEXT)
                payload = build_session_config(app.live.tool_schemas(), app.live.memory_context)
                self.assertTrue(payload["instructions"].endswith(PRIOR_CONTEXT))
                self.assertTrue(payload["delegation"]["responses"]["instructions"].endswith(PRIOR_CONTEXT))

            app.live.run = AsyncMock(side_effect=run)
            worker = CONNECT_TO_CORA(app)
            app.connection_worker = worker
            await asyncio.wait_for(worker.wait(), 3)
            self.assertEqual(order, ["mark", "load", "live"])
            app.live.run.assert_awaited_once()
            activity = "\n".join(app.activity.entries)
            self.assertIn("Tagged 2", activity)
            self.assertIn(f"{len(PRIOR_CONTEXT)} characters", activity)
            for record in PRIOR_RECORDS:
                self.assertNotIn(record["text"], activity)
            self.assertEqual(app.conversation.log_entries, [])
            app.persistence.save_message.assert_not_called()
            app.persistence.save_chat_message.assert_not_called()
            app.persistence.create_conversation.assert_not_called()

    async def test_memory_failure_resets_stale_context_warns_redacts_and_still_connects(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.live.memory_context = "stale private memory"
            app.persistence.mark_interrupted_conversations.return_value = 0
            app.persistence.load_startup_memory_context.side_effect = RuntimeError("private SQL and credentials")

            async def run(*args):
                self.assertEqual(app.live.memory_context, "")

            app.live.run = AsyncMock(side_effect=run)
            with patch.object(app, "notify") as notify:
                worker = CONNECT_TO_CORA(app)
                app.connection_worker = worker
                await asyncio.wait_for(worker.wait(), 3)
                app.live.run.assert_awaited_once()
                notify.assert_called_once_with(
                    "Could not load saved memories; connecting without them.", severity="warning",
                )
            activity = "\n".join(app.activity.entries)
            self.assertIn("Startup memory load failed (RuntimeError)", activity)
            self.assertNotIn("private SQL and credentials", activity)
            self.assertNotIn("stale private memory", activity)
            app.persistence.save_message.assert_not_called()

    async def test_slow_memory_read_keeps_ui_usable_and_shutdown_waits_before_disposal(self):
        async with keyboard_tests.running_app() as (app, pilot):
            started = asyncio.Event()
            release = Event()
            loop = asyncio.get_running_loop()
            order = []
            app.persistence.mark_interrupted_conversations.return_value = 0
            app.live.memory_context = "stale context"
            app.live.run = AsyncMock()

            def slow_read():
                order.append("read_started")
                loop.call_soon_threadsafe(started.set)
                if not release.wait(timeout=5):
                    raise RuntimeError("Test did not release memory read")
                order.append("read_finished")
                return PRIOR_CONTEXT

            app.persistence.load_startup_memory_context.side_effect = slow_read
            app.persistence.close.side_effect = lambda: order.append("dispose")
            close = None
            worker = CONNECT_TO_CORA(app)
            app.connection_worker = worker
            try:
                await asyncio.wait_for(started.wait(), 2)
                self.assertEqual(app.live.memory_context, "")
                composer = app.query_one("#chat-input", ChatComposer)
                self.assertFalse(composer.disabled)
                composer.load_text("Draft remains usable")
                await pilot.click("#activity-panel")
                self.assertFalse(app.query_one("#activity-panel", Collapsible).collapsed)
                self.assertEqual(composer.text, "Draft remains usable")
                with patch.object(app, "exit") as exit_app:
                    close = asyncio.create_task(app.action_disconnect_and_quit())
                    await asyncio.sleep(0)
                    self.assertTrue(app.closing)
                    self.assertFalse(close.done())
                    app.live.close.assert_not_called()
                    app.persistence.close.assert_not_called()
                    release.set()
                    await asyncio.wait_for(close, 3)
                    app.live.close.assert_awaited_once()
                    app.persistence.close.assert_called_once()
                    exit_app.assert_called_once()
                self.assertEqual(order, ["read_started", "read_finished", "dispose"])
                self.assertEqual(app.live.memory_context, "")
                app.live.run.assert_not_called()
                app.persistence.save_message.assert_not_called()
                app.persistence.save_chat_message.assert_not_called()
                app.persistence.create_conversation.assert_not_called()
                self.assertNotIn(PRIOR_RECORDS[0]["text"], "\n".join(app.activity.entries))
            finally:
                release.set()
                if close is not None and not close.done():
                    await asyncio.wait_for(close, 3)
                if not worker.is_finished:
                    await asyncio.wait_for(worker.wait(), 3)


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class StartupMemoryDatabaseTests(unittest.IsolatedAsyncioTestCase):
    # Reuse fixture methods via the module, never inherit/import its TestCase.
    # All writes below target its unique cora_test_* database, never personal data.
    setUpClass = classmethod(memory_tests.MemoryStoreDatabaseTests.setUpClass.__func__)

    def setUp(self):
        memory_tests.MemoryStoreDatabaseTests.setUp(self)
        self.assertTrue(self.settings["dbname"].startswith("cora_test_"))

    def seed(self, entries):
        """Insert evidence-valid user memories with explicit source/extraction dates."""
        sources = []
        for index, (kind, content, source_date) in enumerate(entries):
            source = self.store.save_message(
                self.conversation_id, message_id=UUID(int=1000 + index), role="user",
                text=content, channel="voice", status="incomplete",
            )
            sources.append(source)
        self.store.close_conversation(self.conversation_id)
        self.memories.save_memories(self.conversation_id, [
            MemoryCandidate(source.id, kind, content, content)
            for source, (kind, content, _) in zip(sources, entries)
        ])
        with Session(self.engine) as session, session.begin():
            for index, (source, (_, _, source_date)) in enumerate(zip(sources, entries)):
                session.execute(update(MessageRecord).where(MessageRecord.id == source.id).values(created_at=source_date))
                session.execute(update(MemoryRecord).where(MemoryRecord.source_message_id == source.id).values(
                    id=UUID(int=2000 + index),
                    created_at=SOURCE_DATE - timedelta(days=index),
                ))
        return sources

    async def test_database_ranks_categories_source_recency_and_id_ties_before_limit_ten(self):
        self.assertEqual(self.memories.get_startup_context(), "")
        entries = []
        for index in range(12):
            kind = ("fact", "preference", "decision", "task")[index % 4]
            # Newer sources have OLDER extraction timestamps; rows 8/9 tie.
            day = 9 if index in (8, 9) else index
            entries.append((kind, f"Unique {kind} memory {index}", SOURCE_DATE + timedelta(days=day)))
        self.seed(entries)
        # A very recent assistant-linked row must not leak into startup context.
        assistant = self.store.save_message(
            self.conversation_id, message_id=UUID(int=9999), role="assistant", text="Assistant claim",
            channel="voice", status="incomplete",
        )
        with Session(self.engine) as session, session.begin():
            session.add(MemoryRecord(source_message_id=assistant.id, kind="fact", text="Assistant claim",
                                     evidence="Assistant claim"))
        expected = sorted(enumerate(entries), key=lambda item: (
            0 if item[1][0] in ("fact", "preference") else 1,
            -item[1][2].timestamp(), -(2000 + item[0]),
        ))[:10]
        context = self.memories.get_startup_context()
        records = json.loads(context)
        self.assertEqual(len(records), 10)
        self.assertEqual(records, [
            {"kind": kind, "text": content, "source_date": date.date().isoformat()}
            for _, (kind, content, date) in expected
        ])
        self.assertEqual([record["text"] for record in records[:2]],
                         [entries[9][1], entries[8][1]])
        self.assertLessEqual(len(context), 3000)
        self.assertNotIn("Assistant claim", context)

    async def test_database_budget_preserves_complete_records_and_stops_before_later_small_row(self):
        entries = [
            ("fact", "First café 東京 \"quote\"\n" + "x" * 450, SOURCE_DATE + timedelta(days=2)),
            ("preference", "\x01" * 500, SOURCE_DATE + timedelta(days=1)),
            ("task", "Small later task must not be selected", SOURCE_DATE),
        ]
        self.seed(entries)
        context = self.memories.get_startup_context()
        self.assertEqual(json.loads(context), [{
            "kind": "fact", "text": entries[0][1], "source_date": entries[0][2].date().isoformat(),
        }])
        self.assertLessEqual(len(context), 3000)
        self.assertIn("東京", context)
        self.assertNotIn(entries[2][1], context)
        self.assertEqual(len(self.memories.get_memories(self.conversation_id)), 3)

    async def test_prior_chat_context_reaches_actual_live_start_without_creating_current_chat(self):
        self.seed([(record["kind"], record["text"], SOURCE_DATE) for record in PRIOR_RECORDS])
        context = self.memories.get_startup_context()
        # Same-date facts use descending memory UUID; tasks follow both facts.
        self.assertEqual(json.loads(context), [PRIOR_RECORDS[1], PRIOR_RECORDS[0], PRIOR_RECORDS[2]])
        persistence = KeyboardPersistence(self.store)
        async with keyboard_tests.running_app(persistence) as (app, pilot):
            self.assertEqual(app.live.memory_context, "")
            with fake_live_transport() as (start, speaker, chunks):
                worker = CONNECT_TO_CORA(app)
                app.connection_worker = worker
                await asyncio.wait_for(worker.wait(), 3)
                start.assert_awaited_once()
                payload = start.call_args.kwargs["session"]
                self.assertEqual(app.live.memory_context, context)
                self.assertEqual(payload, build_session_config(app.tools.schemas(), context))
                self.assertTrue(payload["instructions"].endswith(context))
                self.assertTrue(payload["delegation"]["responses"]["instructions"].endswith(context))
                chunks.assert_not_called()
            with patch.object(app, "exit"):
                await app.action_disconnect_and_quit()
        self.assertIsNone(self.store.get_conversation(persistence.conversation_id))
        self.assertEqual(len(self.store.get_messages(self.conversation_id)), 3)
        self.assertEqual(len(self.memories.get_memories(self.conversation_id)), 3)
        self.assertIsNone(self.store.get_summary(self.conversation_id))
        with Session(self.engine) as session:
            self.assertEqual(list(session.scalars(select(ConversationRecord.id))), [self.conversation_id])
        for record in PRIOR_RECORDS:
            self.assertNotIn(record["text"], "\n".join(app.activity.entries))


if __name__ == "__main__":
    unittest.main()