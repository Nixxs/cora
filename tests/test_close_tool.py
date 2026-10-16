import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from openai.types.live import ResponseEvent
from openai.types.responses import ResponseOutputItemDoneEvent

from app import CoraApp
from session_config import build_session_config


def example_schema(name):
    return {"type": "function", "name": name, "parameters": {"type": "object"}}


def tool_event(call_id="call_close", name="close_app", arguments="{}", status="completed"):
    payload = ResponseOutputItemDoneEvent.model_validate({
        "type": "response.output_item.done", "output_index": 0, "sequence_number": 1,
        "item": {
            "type": "function_call", "id": f"item_{call_id}",
            "name": name, "call_id": call_id, "arguments": arguments, "status": status,
        },
    })
    return ResponseEvent.model_validate({
        "type": "response.event", "event_id": f"event_{call_id}",
        "delegation_id": "delegation_close", "event": payload.model_dump(),
    })


class CloseToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        delay = patch("tools.close_app.sleep", new_callable=AsyncMock)
        self.delay = delay.start()
        self.addCleanup(delay.stop)

    async def test_valid_call_runs_once_and_returns_correct_result(self):
        with patch.object(CoraApp, "connect_to_cora", return_value=None):
            app = CoraApp()
            async with app.run_test(size=(90, 35)):
                create = AsyncMock()
                app.live.connection = SimpleNamespace(response=SimpleNamespace(item=SimpleNamespace(create=create)))
                with patch.object(app, "action_disconnect_and_quit", new_callable=AsyncMock) as close:
                    order = MagicMock()
                    order.attach_mock(create, "result")
                    order.attach_mock(self.delay, "delay")
                    order.attach_mock(close, "close")
                    event = tool_event()
                    app.handle_live_event(event)
                    app.handle_live_event(event)
                    app.handle_live_event(tool_event(call_id="second_call"))
                    await asyncio.wait_for(app.workers.wait_for_complete(), 2)
                    close.assert_awaited_once()
                    create.assert_awaited_once()
                    self.delay.assert_awaited_once_with(3)
                    self.assertEqual([entry[0] for entry in order.mock_calls], ["result", "delay", "close"])
                    result = create.call_args.kwargs["item"]
                    self.assertEqual(result["type"], "function_call_output")
                    self.assertEqual(result["call_id"], "call_close")
                    self.assertEqual(json.loads(result["output"]), {"status": "shutting_down"})

    async def test_invalid_calls_and_partial_events_do_not_close(self):
        with patch.object(CoraApp, "connect_to_cora", return_value=None):
            app = CoraApp()
            async with app.run_test(size=(90, 35)):
                with patch.object(app, "action_disconnect_and_quit", new_callable=AsyncMock) as close:
                    for event in (
                        tool_event(name="unknown_tool"),
                        tool_event(arguments="not JSON"),
                        tool_event(arguments='{"unexpected": true}'),
                        tool_event(arguments="[]"),
                        tool_event(arguments="null"),
                        tool_event(call_id=""),
                        tool_event(status="incomplete"),
                        tool_event(status="in_progress"),
                    ):
                        app.handle_live_event(event)
                    event = tool_event()
                    event.event["type"] = "response.output_item.added"
                    app.handle_live_event(event)
                    await app.workers.wait_for_complete()
                    close.assert_not_called()
                    self.assertFalse(app.tools.handled_call_ids)

    async def test_registry_dispatches_additional_tools_with_arguments(self):
        with patch.object(CoraApp, "connect_to_cora", return_value=None):
            app = CoraApp()
            async with app.run_test(size=(90, 35)):
                validate_echo = MagicMock()
                echo = AsyncMock()
                validate_other = MagicMock()
                other = AsyncMock()
                app.tools.register(example_schema("echo"), validate_echo, echo)
                app.tools.register(example_schema("other_tool"), validate_other, other)
                with patch.object(app, "action_disconnect_and_quit", new_callable=AsyncMock) as close:
                    first = tool_event(call_id="echo_1", name="echo", arguments='{"text": "hello"}')
                    app.handle_live_event(first)
                    app.handle_live_event(first)
                    app.handle_live_event(tool_event(
                        call_id="echo_2", name="echo", arguments='{"text": "again"}'
                    ))
                    app.handle_live_event(tool_event(
                        call_id="other_1", name="other_tool", arguments='{"count": 3}'
                    ))
                    await asyncio.wait_for(app.workers.wait_for_complete(), 2)
                    self.assertEqual(echo.await_count, 2)
                    echo.assert_any_await("echo_1", {"text": "hello"})
                    echo.assert_any_await("echo_2", {"text": "again"})
                    self.assertEqual(validate_echo.call_count, 2)
                    other.assert_awaited_once_with("other_1", {"count": 3})
                    validate_other.assert_called_once_with({"count": 3})
                    close.assert_not_called()
                    self.assertFalse(app.closing)
                    self.assertEqual(
                        [schema["name"] for schema in app.live.tool_schemas()],
                        ["close_app", "search_memory", "recall_interest", "echo", "other_tool"],
                    )

    async def test_tool_validation_and_handler_errors_are_isolated(self):
        with patch.object(CoraApp, "connect_to_cora", return_value=None):
            app = CoraApp()
            async with app.run_test(size=(90, 35)):
                validate = MagicMock(side_effect=ValueError("Invalid value"))
                handler = AsyncMock(side_effect=RuntimeError("Tool failed"))
                app.tools.register(example_schema("example"), validate, handler)
                event = tool_event(call_id="example_1", name="example", arguments='{"value": 3}')
                app.handle_live_event(event)
                await app.workers.wait_for_complete()
                handler.assert_not_called()
                self.assertNotIn("example_1", app.tools.handled_call_ids)

                validate.side_effect = None
                app.handle_live_event(event)
                await asyncio.wait_for(app.workers.wait_for_complete(), 2)
                handler.assert_awaited_once_with("example_1", {"value": 3})
                self.assertTrue(any("example failed: Tool failed" in entry for entry in app.activity.entries))
                self.assertFalse(app.closing)

    async def test_result_send_failure_still_closes(self):
        with patch.object(CoraApp, "connect_to_cora", return_value=None):
            app = CoraApp()
            async with app.run_test(size=(90, 35)):
                app.live.connection = SimpleNamespace(response=SimpleNamespace(
                    item=SimpleNamespace(create=AsyncMock(side_effect=RuntimeError("Socket closed")))
                ))
                with patch.object(app, "action_disconnect_and_quit", new_callable=AsyncMock) as close:
                    app.handle_live_event(tool_event())
                    await asyncio.wait_for(app.workers.wait_for_complete(), 2)
                    close.assert_awaited_once()
                    self.assertTrue(any("Socket closed" in entry for entry in app.activity.entries))

    async def test_shared_shutdown_is_idempotent_and_exits_on_cleanup_error(self):
        with patch.object(CoraApp, "connect_to_cora", return_value=None):
            app = CoraApp()
            async with app.run_test(size=(90, 35)):
                app.connection_worker = MagicMock()
                app.live.close = AsyncMock(side_effect=RuntimeError("Cleanup failed"))
                with patch.object(app, "exit") as exit_app:
                    with self.assertRaisesRegex(RuntimeError, "Cleanup failed"):
                        await app.action_disconnect_and_quit()
                    await app.action_disconnect_and_quit()
                    app.live.close.assert_awaited_once()
                    app.connection_worker.cancel.assert_called_once()
                    exit_app.assert_called_once()

    async def test_tool_shutdown_keeps_reader_alive_for_audio_cleanup(self):
        queue = asyncio.Queue()
        order = []
        mic_stopped = asyncio.Event()

        async def send_result(**kwargs):
            order.append("result")

        async def end_session():
            order.append("session_close")
            await queue.put(SimpleNamespace(type="session.closed", reason="client_requested"))

        class Connection:
            session = SimpleNamespace(start=AsyncMock(), close=AsyncMock(side_effect=end_session))
            response = SimpleNamespace(
                item=SimpleNamespace(create=AsyncMock(side_effect=send_result)), create=AsyncMock()
            )

            def __aiter__(self):
                async def events():
                    while True:
                        yield await queue.get()
                return events()

        connection = Connection()

        @asynccontextmanager
        async def connect():
            yield connection

        @asynccontextmanager
        async def client():
            yield SimpleNamespace(live=SimpleNamespace(connect=connect))

        async def microphone(*args):
            try:
                await asyncio.Event().wait()
            finally:
                mic_stopped.set()

        speaker = MagicMock()
        speaker.play.return_value = True
        speaker.get_playback_level.return_value = 0.0
        with patch.object(CoraApp, "connect_to_cora", return_value=None), \
                patch("live.AsyncOpenAI", client), patch("live.Speaker", return_value=speaker):
            app = CoraApp()
            async with app.run_test(size=(90, 35)):
                app.connection_worker = MagicMock()
                app.tools.register(example_schema("example"), MagicMock(), AsyncMock())
                with patch.object(app.live, "send_microphone", side_effect=microphone), \
                        patch.object(app, "exit") as exit_app:
                    await queue.put(SimpleNamespace(type="session.started"))
                    await queue.put(SimpleNamespace(type="session.output_audio.delta", delta="AQI="))
                    await queue.put(tool_event())
                    reader = asyncio.create_task(app.live.run(
                        app.handle_live_event, app.handle_mic_status, app.handle_audio_status
                    ))
                    try:
                        await asyncio.wait_for(reader, 2)
                        await asyncio.wait_for(app.workers.wait_for_complete(), 2)
                    finally:
                        if not reader.done():
                            reader.cancel()
                    self.assertEqual(order, ["result", "session_close"])
                    self.assertTrue(mic_stopped.is_set())
                    speaker.play.assert_called_once_with("AQI=")
                    speaker.close.assert_called_once()
                    self.assertTrue(app.live.closed.is_set())
                    self.assertIsNone(app.live.connection)
                    self.assertFalse(app.live.ready)
                    self.assertIsNone(app.live.microphone_task)
                    exit_app.assert_called_once()
                    connection.response.create.assert_not_called()
                    config = connection.session.start.call_args.kwargs["session"]
                    self.assertEqual(config, build_session_config(app.tools.schemas()))
                    backend = config["delegation"]["responses"]
                    self.assertFalse(backend["parallel_tool_calls"])
                    self.assertEqual(backend["tools"][0], {"type": "web_search"})
                    self.assertEqual(backend["tools"][1:], app.tools.schemas())
                    self.assertEqual(backend["tools"][-1]["name"], "example")
                    tool = backend["tools"][1]
                    self.assertEqual(tool["name"], "close_app")
                    self.assertTrue(tool["strict"])
                    self.assertFalse(tool["parameters"]["additionalProperties"])
                    self.assertEqual(backend["model"], "gpt-6-luna")
                    self.assertEqual(config["model"], "gpt-live-1")
                    self.assertEqual(config["audio"]["format"], {"type": "audio/pcm", "rate": 24000})
                    self.assertEqual(config["audio"]["output"]["voice"], "marin")
                    self.assertIn("explicitly asks", config["instructions"])


if __name__ == "__main__":
    unittest.main()