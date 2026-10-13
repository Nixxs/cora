import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from textual.widgets import Button

from app import CoraApp
from live import CoraLive
from session_config import build_session_config
from test_audio_controls import live_transport
from test_keyboard_persistence import running_app
from ui.composer import ChatComposer


CONNECT = CoraApp.connect_to_cora


def backend_event(kind, response_id="response-1", delegation_id="delegation-1", **kwargs):
    return SimpleNamespace(type="response.event", delegation_id=delegation_id, event={
        "type": kind, "response": {"id": response_id}, "response_id": response_id, **kwargs,
    })


@asynccontextmanager
async def restarting_app():
    connections = []
    speakers = []

    class Transport:
        def __init__(self):
            self.events = asyncio.Queue()
            self.session = SimpleNamespace(
                start=AsyncMock(side_effect=self.start), close=AsyncMock(side_effect=self.end),
                thinking=SimpleNamespace(append=AsyncMock()),
            )
            self.response = SimpleNamespace(
                item=SimpleNamespace(create=AsyncMock()), create=AsyncMock(),
            )
            self.close = AsyncMock(side_effect=self.end)

        async def start(self, **kwargs):
            await self.events.put(SimpleNamespace(type="session.started"))

        async def end(self):
            await self.events.put(SimpleNamespace(type="session.closed", reason="close_requested"))

        def __aiter__(self):
            async def receive():
                while True:
                    yield await self.events.get()
            return receive()

    @asynccontextmanager
    async def connect():
        transport = Transport()
        connections.append(transport)
        yield transport

    @asynccontextmanager
    async def client():
        yield SimpleNamespace(live=SimpleNamespace(connect=connect))

    def speaker():
        result = MagicMock()
        result.pending = bytearray()
        result.chunks.empty.return_value = True
        result.get_playback_level.return_value = 0.0
        result.is_playback_busy.return_value = False
        speakers.append(result)
        return result

    async def microphone(*args):
        await asyncio.Event().wait()

    with patch("live.AsyncOpenAI", client), patch("live.Speaker", side_effect=speaker), \
            patch.object(CoraLive, "send_microphone", side_effect=microphone):
        async with running_app() as (app, pilot):
            started = asyncio.Queue()
            handle_event = app.handle_live_event

            def handle(event):
                handle_event(event)
                if event.type == "session.started":
                    started.put_nowait(None)

            app.handle_live_event = handle
            app.connect_to_cora = lambda: CONNECT(app)
            app.live.close = CoraLive.close.__get__(app.live)
            app.live.send_text = CoraLive.send_text.__get__(app.live)
            app.connection_worker = app.connect_to_cora()
            await asyncio.wait_for(started.get(), 5)
            await pilot.pause()
            try:
                yield app, pilot, connections, speakers, started
            finally:
                await app.live.close()
                await asyncio.wait_for(app.connection_worker.wait(), 2)


class ReconnectHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_visible_history_includes_voice_keyboard_and_screen_not_activity_or_draft(self):
        async with running_app() as (app, pilot):
            app.conversation.write_log("You: A typed question")
            app.conversation.append_transcript("Cora", "A spoken answer")
            app.conversation.flush_transcript()
            app.conversation.display_written_item({
                "id": "written", "type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": "<!-- cora:screen -->\nA written answer"}],
            })
            app.activity.write("Private tool details", "Tool")
            app.query_one("#chat-input", ChatComposer).load_text("Unsent draft")
            history = app.conversation.get_reconnect_history()
            self.assertEqual([item["role"] for item in history], ["user", "assistant", "assistant"])
            self.assertEqual([item["content"][0]["text"] for item in history], [
                "A typed question", "A spoken answer", "A written answer",
            ])
            config = build_session_config([], conversation_history=history)
            self.assertEqual(config["input"], history)
            self.assertIn("Do not replay answers", config["instructions"])
            self.assertIn("repeat past tool actions", config["delegation"]["responses"]["instructions"])
            config["input"][0]["content"][0]["text"] = "changed"
            self.assertEqual(history[0]["content"][0]["text"], "A typed question")

    async def test_history_keeps_recent_messages_with_bounded_character_and_message_counts(self):
        async with running_app() as (app, pilot):
            app.conversation.log_entries = [f"You: message-{index}" for index in range(50)]
            history = app.conversation.get_reconnect_history()
            self.assertEqual(len(history), 40)
            self.assertEqual(history[0]["content"][0]["text"], "message-10")
            app.conversation.log_entries = ["You: old", "Cora: " + "界" * 9000]
            history = app.conversation.get_reconnect_history()
            self.assertEqual(len(history), 1)
            self.assertEqual(len(history[0]["content"][0]["text"].encode("utf-8")), 6000)


class ReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_queued_typed_requests_are_tracked_until_their_own_acknowledgements(self):
        live = CoraLive()
        async with live_transport(live) as (connection, speaker, emit):
            await live.send_text("First request")
            await live.send_text("Second request")
            first, second = [call.kwargs["event_id"] for call in connection.response.create.call_args_list]
            self.assertEqual(live.pending_text_requests, {first, second})
            await emit("response.event", event={"type": "response.created"}, client_event_id="other-task")
            self.assertEqual(live.pending_text_requests, {first, second})
            await emit("response.event", event={"type": "response.created"}, client_event_id=first)
            self.assertEqual(live.pending_text_requests, {second})
            await emit("error", error=SimpleNamespace(client_event_id=second))
            self.assertEqual(live.pending_text_requests, set())

    async def test_failed_new_connection_leaves_chat_draft_and_controls_available(self):
        async with running_app() as (app, pilot):
            app.startup_complete = True
            app.connect_to_cora = lambda: CONNECT(app)
            app.live.run = AsyncMock(side_effect=RuntimeError("Could not open socket"))
            composer = app.query_one("#chat-input", ChatComposer)
            composer.load_text("Draft stays here")
            app.conversation.write_log("You: Existing chat")
            app.action_reconnect()
            await asyncio.wait_for(app.reconnect_worker.wait(), 2)
            await asyncio.wait_for(app.connection_worker.wait(), 2)
            self.assertEqual(str(app.query_one("#chat-status", Button).label), "○ Disconnected")
            self.assertFalse(composer.disabled)
            self.assertEqual(composer.text, "Draft stays here")
            self.assertEqual(app.conversation.log_entries, ["You: Existing chat"])
            app.live.send_text.assert_not_called()

    async def test_new_reader_waits_for_old_reader_cleanup(self):
        async with running_app() as (app, pilot):
            entered, release = asyncio.Event(), asyncio.Event()

            async def cleanup():
                entered.set()
                await release.wait()

            app.live.ready = True
            app.connection_worker = MagicMock(is_finished=False, wait=AsyncMock(side_effect=cleanup))
            with patch.object(app, "connect_to_cora") as connect:
                app.action_reconnect()
                await asyncio.wait_for(entered.wait(), 2)
                connect.assert_not_called()
                app.action_reconnect()
                release.set()
                await asyncio.wait_for(app.reconnect_worker.wait(), 2)
                connect.assert_called_once()

    async def test_repeated_reconnect_preserves_app_state_and_restores_history_without_requests(self):
        async with restarting_app() as (app, pilot, connections, speakers, started):
            self.assertTrue(app.live.ready)
            await app.live.set_microphone_muted(True)
            app.live.set_speaker_muted(True)
            app.live.memory_context = "[]"
            app.conversation.write_log("You: Remember the blue door")
            app.conversation.write_log("Cora: The blue door is noted")
            await pilot.pause()
            composer = app.query_one("#chat-input", ChatComposer)
            composer.load_text("Unsent question")
            pending = ("Unsent question", uuid4())
            app.pending_keyboard_message = pending
            app.tools.handled_call_ids.add("already-run")
            owners = (app.persistence, app.conversation, app.tools, app.live)
            conversation_id = app.persistence.conversation_id
            history = app.conversation.get_reconnect_history()
            for expected_count in (2, 3):
                await pilot.press("ctrl+r")
                await asyncio.wait_for(app.reconnect_worker.wait(), 2)
                await asyncio.wait_for(started.get(), 5)
                await pilot.pause()
                self.assertEqual(len(connections), expected_count)
                old, new = connections[-2:]
                old.session.close.assert_awaited_once()
                self.assertEqual(new.session.start.call_args.kwargs["session"]["input"], history)
                new.response.item.create.assert_not_awaited()
                new.response.create.assert_not_awaited()
                self.assertTrue(app.live.ready)
                self.assertFalse(app.live.closed.is_set())
                self.assertEqual(str(app.query_one("#chat-status", Button).label), "● Active")
                self.assertFalse(composer.disabled)
                self.assertEqual(composer.text, "Unsent question")
                self.assertEqual(app.pending_keyboard_message, pending)
                self.assertTrue(app.live.microphone_muted)
                self.assertTrue(app.live.speaker_muted)
                self.assertEqual((app.persistence, app.conversation, app.tools, app.live), owners)
                self.assertEqual(app.persistence.conversation_id, conversation_id)
                self.assertEqual(app.tools.handled_call_ids, {"already-run"})
                self.assertEqual(app.conversation.get_reconnect_history(), history)
            app.persistence.mark_interrupted_conversations.assert_called_once()
            app.persistence.load_startup_memory_context.assert_called_once()
            app.persistence.finish_conversation.assert_not_called()
            app.persistence.close.assert_not_called()
            self.assertTrue(all(s.close.called for s in speakers[:-1]))
            await app.live.send_text("A fresh question")
            connections[-1].response.create.assert_awaited_once()
            app.handle_live_event(backend_event("response.created"))
            app.handle_live_event(backend_event("response.completed"))

    async def test_known_pending_work_and_recent_speech_refuse_reconnect(self):
        async with running_app() as (app, pilot):
            app.live.ready = True
            with patch.object(app, "reconnect_live") as reconnect, patch.object(app, "notify") as notify:
                cases = [
                    ("backend", lambda: app.pending_backend_work.add("response")),
                    ("keyboard", lambda: setattr(app, "keyboard_worker", MagicMock(is_finished=False))),
                    ("recent speech", lambda: app.handle_live_event(SimpleNamespace(
                        type="session.output_transcript.delta", delta="Still speaking",
                    ))),
                ]
                for name, arrange in cases:
                    with self.subTest(name):
                        arrange()
                        app.action_reconnect()
                        reconnect.assert_not_called()
                        self.assertFalse(app.reconnecting)
                        self.assertIn("pending work", notify.call_args.args[0])
                        app.pending_backend_work.clear()
                        app.keyboard_worker = None
                        app.last_live_activity = 0.0
                    app.live.pending_text_requests.add("awaiting-ack")
                    app.action_reconnect()
                    reconnect.assert_not_called()
                    app.live.pending_text_requests.clear()
                blocked = asyncio.Event()

                async def tool():
                    await blocked.wait()

                worker = app.run_worker(tool(), group="tool-actions")
                app.action_reconnect()
                reconnect.assert_not_called()
                blocked.set()
                await worker.wait()
                speaker = MagicMock(pending=bytearray(b"queued"))
                app.live.speaker = speaker
                app.action_reconnect()
                reconnect.assert_not_called()
                app.live.speaker = None

    async def test_tool_completion_requires_continuation_and_disconnect_does_not_retry_work(self):
        async with running_app() as (app, pilot):
            with patch.object(app.tools, "handle_function_call") as handle:
                app.handle_live_event(backend_event("response.created"))
                app.handle_live_event(backend_event("response.output_item.done", item={
                    "type": "function_call", "name": "search_memory", "call_id": "call-1",
                }))
                app.handle_live_event(backend_event("response.completed"))
                self.assertEqual(app.pending_backend_work, {"delegation-1"})
                app.handle_live_event(backend_event("response.created", response_id="response-2"))
                app.handle_live_event(backend_event("response.completed", response_id="response-2"))
                self.assertEqual(app.pending_backend_work, set())
                handle.assert_called_once()
                app.pending_backend_work.add("lost-response")
                with patch.object(app, "notify") as notify:
                    app.handle_live_event(SimpleNamespace(type="session.closed", reason="connection_lost"))
                    self.assertIn("will not retry", notify.call_args.args[0])
                self.assertFalse(app.pending_backend_work)
                app.live.send_text.assert_not_called()

    async def test_reconnect_is_single_flight_and_quit_during_close_cannot_start_a_new_session(self):
        async with running_app() as (app, pilot):
            entered, release = asyncio.Event(), asyncio.Event()

            async def close():
                entered.set()
                await release.wait()

            app.live.close = AsyncMock(side_effect=close)
            with patch.object(app, "connect_to_cora") as connect, patch.object(app, "exit"):
                app.action_reconnect()
                await asyncio.wait_for(entered.wait(), 2)
                app.action_reconnect()
                self.assertTrue(app.query_one("#chat-input", ChatComposer).disabled)
                quit_task = asyncio.create_task(app.action_disconnect_and_quit())
                await pilot.pause()
                release.set()
                await asyncio.wait_for(quit_task, 2)
                connect.assert_not_called()
                self.assertTrue(app.closing)
                self.assertFalse(app.reconnecting)

    async def test_failed_close_does_not_start_new_reader_or_log_private_errors(self):
        async with running_app() as (app, pilot):
            app.live.close = AsyncMock(side_effect=RuntimeError("private transport data"))
            with patch.object(app, "connect_to_cora") as connect:
                app.action_reconnect()
                await asyncio.wait_for(app.reconnect_worker.wait(), 2)
                connect.assert_not_called()
                self.assertFalse(app.reconnecting)
                self.assertFalse(app.query_one("#chat-input", ChatComposer).disabled)
                self.assertIn("Reconnect failed (RuntimeError)", "\n".join(app.activity.entries))
                self.assertNotIn("private transport data", "\n".join(app.activity.entries))

    async def test_transport_loss_warns_then_allows_reconnect_without_repeating_startup(self):
        async with running_app() as (app, pilot):
            app.startup_complete = True
            app.live.run = AsyncMock(side_effect=RuntimeError("transport lost"))
            app.pending_backend_work.add("lost-response")
            worker = CONNECT(app)
            await asyncio.wait_for(worker.wait(), 2)
            self.assertFalse(app.pending_backend_work)
            self.assertTrue(any("will not be replayed" in entry for entry in app.activity.entries))
            app.persistence.mark_interrupted_conversations.assert_not_called()


if __name__ == "__main__":
    unittest.main()