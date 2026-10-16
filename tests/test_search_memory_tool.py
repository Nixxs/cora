"""Search tool boundaries and actual headless routing; no real APIs or database."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
from threading import Event, get_ident
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, call, patch
from uuid import uuid4

from openai.types.live import ResponseEvent
from openai.types.responses import ResponseOutputItemDoneEvent

from app import CoraApp
from live import CoraLive
from memory.store import MemoryMatch
from tools import create_tool_registry
from tools.search_memory import ERROR_RESULT, SCHEMA, SearchMemoryTool, validate_arguments


def memory_match(**changes):
    return MemoryMatch(**{
        "memory_id": uuid4(), "source_message_id": uuid4(), "conversation_id": uuid4(),
        "kind": "fact", "text": "PRIVATE remembered text",
        "source_created_at": datetime(2026, 10, 13, 1, tzinfo=timezone(timedelta(hours=8))),
        "distance": 0.125, **changes,
    })


def search_event():
    payload = ResponseOutputItemDoneEvent.model_validate({
        "type": "response.output_item.done", "output_index": 0, "sequence_number": 1,
        "item": {
            "type": "function_call", "id": "item_search", "name": "search_memory",
            "call_id": "call_search", "arguments": '{"query":"PRIVATE question","limit":null}',
            "status": "completed",
        },
    })
    return ResponseEvent.model_validate({
        "type": "response.event", "event_id": "event_search",
        "delegation_id": "delegation_search", "event": payload.model_dump(),
    })


class SearchMemoryToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Guard every search boundary, including regressions that bypass our mocks.
        for target in ("memory.search_memory.OpenAI", "memory.embed_memory.OpenAI"):
            guard = patch(target, side_effect=AssertionError("Real API forbidden"))
            guard.start()
            self.addCleanup(guard.stop)
        for target, attribute in (
            ("tools.search_memory.build_engine", "build"),
            ("tools.search_memory.MemoryStore", "store_type"),
            ("tools.search_memory.search_memory", "search"),
        ):
            guard = patch(target)
            setattr(self, attribute, guard.start())
            self.addCleanup(guard.stop)
        self.engine = MagicMock()
        self.build.return_value = self.engine
        self.search.return_value = [memory_match()]
        self.log = MagicMock()
        self.closing = False
        self.connection = SimpleNamespace(response=SimpleNamespace(
            item=SimpleNamespace(create=AsyncMock()), create=AsyncMock(),
        ))
        self.current = self.connection
        self.tool = SearchMemoryTool(lambda: self.current, lambda: self.closing, self.log)

    def result(self):
        item = self.connection.response.item.create.call_args.kwargs["item"]
        self.assertEqual(item["type"], "function_call_output")
        self.assertEqual(item["call_id"], "call_search")
        return json.loads(item["output"])

    def test_strict_schema_and_local_convenience_defaults(self):
        self.assertEqual(SCHEMA["name"], "search_memory")
        self.assertTrue(SCHEMA["strict"])
        parameters = SCHEMA["parameters"]
        self.assertFalse(parameters["additionalProperties"])
        self.assertEqual(parameters["required"], ["query", "limit"])
        self.assertEqual(parameters["properties"]["query"], {
            "type": "string", "minLength": 1, "maxLength": 1000,
        })
        limit = parameters["properties"]["limit"]
        self.assertEqual(limit["type"], ["integer", "null"])
        self.assertEqual((limit["minimum"], limit["maximum"]), (1, 20))
        self.assertIn("default of 8", limit["description"])
        for arguments in ({"query": " q "}, {"query": "q", "limit": None},
                          {"query": "x" * 1000, "limit": 1}, {"query": "q", "limit": 20}):
            validate_arguments(arguments)
        registry = create_tool_registry(
            get_connection=lambda: self.current, is_closing=lambda: self.closing,
            shutdown=AsyncMock(), write_debug=self.log, schedule=MagicMock(),
        )
        self.assertEqual([schema["name"] for schema in registry.schemas()],
                         ["close_app", "search_memory", "recall_interest"])
        self.assertEqual(registry.schemas()[1], SCHEMA)
        self.build.assert_not_called()

    async def test_success_is_bounded_dated_private_and_off_event_loop(self):
        matches = [memory_match(text="x" * 900) for _ in range(25)]
        thread_ids = []
        self.search.side_effect = lambda *args, **kwargs: thread_ids.append(get_ident()) or matches
        order = MagicMock()
        order.attach_mock(self.connection.response.item.create, "output")
        order.attach_mock(self.connection.response.create, "continue_response")
        await self.tool.handle("call_search", {"query": "  PRIVATE question  ", "limit": 20})
        self.assertNotEqual(thread_ids, [get_ident()])
        self.build.assert_called_once_with()
        self.store_type.assert_called_once_with(self.engine)
        self.search.assert_called_once_with(self.store_type.return_value, "PRIVATE question", limit=20)
        self.engine.dispose.assert_called_once_with()
        result = self.result()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["matches"]), 20)
        self.assertLess(len(json.dumps(result)), 18000)
        first = result["matches"][0]
        self.assertEqual(first, {
            "memory_id": str(matches[0].memory_id),
            "source_message_id": str(matches[0].source_message_id),
            "conversation_id": str(matches[0].conversation_id), "kind": "fact",
            "text": "x" * 500, "source_date": "2026-10-12",
            "distance": 0.125, "label": "historical_untrusted",
        })
        for warning in ("not exhaustive", "unembedded", "duplicates", "corrections",
                        "plans and ages", "not confidence", "not guaranteed"):
            self.assertIn(warning, result["note"])
        self.assertEqual([call[0] for call in order.mock_calls], ["output", "continue_response"])
        self.connection.response.create.assert_awaited_once_with()
        self.assertEqual(self.log.call_args_list, [
            call("Memory search requested: call_id=call_search", "Searching memories"),
            call("Memory search complete: call_id=call_search, count=20", "Memory search complete"),
        ])

    async def test_empty_results_and_null_or_missing_limits_continue_once(self):
        self.search.return_value = []
        for arguments in ({"query": "q"}, {"query": "q", "limit": None}):
            with self.subTest(arguments=arguments):
                self.search.reset_mock()
                self.connection.response.item.create.reset_mock()
                self.connection.response.create.reset_mock()
                await self.tool.handle("call_search", arguments)
                self.search.assert_called_once_with(self.store_type.return_value, "q", limit=8)
                result = self.result()
                self.assertEqual((result["status"], result["matches"]), ("ok", []))
                self.assertIn("not exhaustive", result["note"])
                self.connection.response.create.assert_awaited_once_with()

    async def test_invalid_arguments_are_rejected_before_dispatch_or_api(self):
        schedule = MagicMock()
        registry = create_tool_registry(
            get_connection=lambda: self.current, is_closing=lambda: False,
            shutdown=AsyncMock(), write_debug=self.log, schedule=schedule,
        )
        invalid = [{}, {"query": "q", "extra": "PRIVATE"}]
        invalid += [{"query": value} for value in ("", " \n\t", "x" * 1001, None, True, 4, [], {})]
        invalid += [{"query": "PRIVATE", "limit": value}
                    for value in (True, False, 1.0, 0, 21, -1, "8", [], {})]
        for index, arguments in enumerate(invalid):
            with self.subTest(index=index), self.assertRaises(ValueError):
                validate_arguments(arguments)
            registry.handle_function_call({
                "name": "search_memory", "call_id": f"invalid_{index}",
                "arguments": json.dumps(arguments), "status": "completed",
            })
        schedule.assert_not_called()
        self.assertFalse(registry.handled_call_ids)
        self.build.assert_not_called()
        self.search.assert_not_called()
        self.connection.response.item.create.assert_not_called()
        self.assertNotIn("PRIVATE", str(self.log.call_args_list))

    async def test_search_failures_redact_and_continue_with_error_and_dispose(self):
        for boundary in (self.build, self.store_type, self.search, self.engine.dispose):
            with self.subTest(boundary=boundary):
                self.engine.dispose.reset_mock()
                self.connection.response.item.create.reset_mock()
                self.connection.response.create.reset_mock()
                self.log.reset_mock()
                boundary.side_effect = RuntimeError("PRIVATE query, memory, credentials")
                try:
                    await self.tool.handle("call_search", {"query": "PRIVATE question"})
                    self.assertEqual(self.result(), ERROR_RESULT)
                    self.connection.response.item.create.assert_awaited_once()
                    self.connection.response.create.assert_awaited_once_with()
                    self.log.assert_any_call("Memory search failed (RuntimeError)", "Tool error")
                    self.assertNotIn("PRIVATE", str(self.log.call_args_list))
                    if boundary is not self.build:
                        self.engine.dispose.assert_called_once_with()
                finally:
                    boundary.side_effect = None

    async def test_closing_or_absent_connection_prevents_search(self):
        for closing, current in ((True, self.connection), (False, None)):
            self.closing, self.current = closing, current
            await self.tool.handle("call_search", {"query": "q"})
        self.build.assert_not_called()
        self.search.assert_not_called()
        self.connection.response.item.create.assert_not_called()
        self.connection.response.create.assert_not_called()

    async def test_blocked_thread_is_nonblocking_and_stale_results_are_discarded(self):
        for change in ("closing", "connection", "disconnected"):
            with self.subTest(change=change):
                self.closing, self.current = False, self.connection
                self.engine.dispose.reset_mock()
                started, release = Event(), Event()

                def blocked(*args, **kwargs):
                    started.set()
                    if not release.wait(3):
                        raise TimeoutError("Test did not release thread")
                    return [memory_match()]

                self.search.side_effect = blocked
                task = asyncio.create_task(self.tool.handle("call_search", {"query": "q"}))
                try:
                    self.assertTrue(await asyncio.to_thread(started.wait, 2))
                    heartbeat = asyncio.Event()
                    asyncio.get_running_loop().call_soon(heartbeat.set)
                    await asyncio.wait_for(heartbeat.wait(), 1)
                    self.assertFalse(task.done())
                    if change == "closing":
                        self.closing = True
                    else:
                        self.current = object() if change == "connection" else None
                finally:
                    release.set()
                    await asyncio.wait_for(task, 3)
                self.engine.dispose.assert_called_once_with()
        self.connection.response.item.create.assert_not_called()
        self.connection.response.create.assert_not_called()

    async def test_transport_failures_and_post_output_guards_never_retry_or_leak(self):
        for boundary in ("output", "continuation", "closing", "connection"):
            with self.subTest(boundary=boundary):
                self.closing, self.current = False, self.connection
                output, continuation = self.connection.response.item.create, self.connection.response.create
                output.reset_mock(side_effect=True)
                continuation.reset_mock(side_effect=True)
                self.log.reset_mock()
                if boundary == "output":
                    output.side_effect = RuntimeError("PRIVATE output payload")
                elif boundary == "continuation":
                    continuation.side_effect = RuntimeError("PRIVATE response payload")
                else:
                    async def change_connection(**kwargs):
                        if boundary == "closing":
                            self.closing = True
                        else:
                            self.current = object()
                    output.side_effect = change_connection
                await self.tool.handle("call_search", {"query": "PRIVATE question"})
                output.assert_awaited_once()
                self.assertEqual(continuation.await_count, 1 if boundary == "continuation" else 0)
                if boundary in ("output", "continuation"):
                    self.log.assert_any_call("Memory search failed (RuntimeError)", "Tool error")
                else:
                    self.assertEqual(self.log.call_count, 2)
                self.assertNotIn("PRIVATE", str(self.log.call_args_list))
        # Even a broken logging callback must not escape to generic registry logging.
        self.closing, self.current = False, self.connection
        self.connection.response.item.create.side_effect = RuntimeError("PRIVATE")
        self.log.side_effect = RuntimeError("PRIVATE logging failure")
        await self.tool.handle("call_search", {"query": "q"})

    async def test_cancellation_does_not_kill_thread_or_leak_owned_engine(self):
        started, release, disposed = Event(), Event(), Event()

        def blocked(*args, **kwargs):
            started.set()
            if not release.wait(3):
                raise TimeoutError("Test did not release thread")
            return []

        self.search.side_effect = blocked
        self.engine.dispose.side_effect = disposed.set
        task = asyncio.create_task(self.tool.handle("call_search", {"query": "q"}))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(disposed.is_set())
        finally:
            release.set()
            self.assertTrue(await asyncio.to_thread(disposed.wait, 2))
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.engine.dispose.assert_called_once_with()
        self.connection.response.item.create.assert_not_called()
        self.connection.response.create.assert_not_called()

    async def test_actual_headless_live_routing_schema_dedup_and_output_order(self):
        continued = asyncio.Event()
        order = []

        async def output(**kwargs):
            order.append("output")

        async def continuation():
            order.append("continuation")
            continued.set()

        class Connection:
            session = SimpleNamespace(start=AsyncMock())
            response = SimpleNamespace(
                item=SimpleNamespace(create=AsyncMock(side_effect=output)),
                create=AsyncMock(side_effect=continuation),
            )

            def __aiter__(self):
                async def events():
                    yield SimpleNamespace(type="session.started")
                    event = search_event()
                    yield event
                    yield event
                    await asyncio.wait_for(continued.wait(), 3)
                    yield SimpleNamespace(type="session.closed", reason="test_complete")
                return events()

        connection = Connection()

        @asynccontextmanager
        async def connect():
            yield connection

        @asynccontextmanager
        async def client():
            yield SimpleNamespace(live=SimpleNamespace(connect=connect))

        with patch.object(CoraApp, "connect_to_cora", return_value=None), \
                patch("live.AsyncOpenAI", client), patch("live.Speaker") as speaker, \
                patch.object(CoraLive, "send_microphone", new_callable=AsyncMock), \
                patch("live.microphone_chunks", side_effect=AssertionError("Hardware forbidden")):
            speaker.return_value.get_playback_level.return_value = 0.0
            app = CoraApp()
            async with app.run_test(size=(90, 35)):
                await asyncio.wait_for(app.live.run(
                    app.handle_live_event, app.handle_mic_status, app.handle_audio_status,
                ), 5)
                await asyncio.wait_for(app.workers.wait_for_complete(), 3)
                tools = connection.session.start.call_args.kwargs["session"]["delegation"]["responses"]["tools"]
                self.assertEqual(tools[0], {"type": "web_search"})
                self.assertEqual([tool["name"] for tool in tools[1:]],
                                 ["close_app", "search_memory", "recall_interest"])
                self.assertEqual(tools[2], SCHEMA)
                self.search.assert_called_once_with(self.store_type.return_value, "PRIVATE question", limit=8)
                self.engine.dispose.assert_called_once_with()
                connection.response.item.create.assert_awaited_once()
                connection.response.create.assert_awaited_once_with()
                item = connection.response.item.create.call_args.kwargs["item"]
                self.assertEqual(item["call_id"], "call_search")
                self.assertEqual(json.loads(item["output"])["status"], "ok")
                self.assertEqual(order, ["output", "continuation"])
                self.assertEqual(app.tools.handled_call_ids, {"call_search"})
                self.assertFalse(app.closing)
                self.assertEqual(app.conversation.log_entries, [])
                self.assertNotIn("PRIVATE", "\n".join(app.activity.entries))


if __name__ == "__main__":
    unittest.main()