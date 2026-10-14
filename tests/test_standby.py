import asyncio
import base64
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from openai.types.live.response_event import ResponseEvent
from openai.types.live.delegation_created_event import DelegationCreatedEvent
from textual.widgets import Button, Static

from devices.speaker import Speaker
from test_keyboard_persistence import running_app
from test_reconnect import backend_event, restarting_app
from ui.composer import ChatComposer


class StandbyTests(unittest.IsolatedAsyncioTestCase):
    async def test_captured_typed_delegation_tool_continuation_then_standby(self):
        async with restarting_app() as (app, pilot, connections, speakers, started):
            composer = app.query_one("#chat-input", ChatComposer)
            composer.load_text("Typed request")
            await pilot.press("enter")
            await asyncio.wait_for(app.keyboard_worker.wait(), 3)
            request_id = connections[0].response.create.call_args.kwargs["event_id"]
            await connections[0].events.put(DelegationCreatedEvent(
                type="session.delegation.created", event_id="delegation-event", offset_ms=100,
                client_event_id=request_id, delegation={
                    "id": "typed-delegation", "type": "delegation", "target": "responses",
                    "response_id": "typed-response",
                },
            ))
            await pilot.pause()
            self.assertFalse(app.live.pending_text_requests)
            await connections[0].events.put(backend_event(
                "response.created", response_id="typed-response", delegation_id="typed-delegation",
            ))
            await pilot.pause()
            self.assertNotIn("typed-request", app.backend_work.pending)
            with patch.object(app.tools, "handle_function_call") as handle:
                # The real forwarded item has neither response nor response_id.
                await connections[0].events.put(ResponseEvent(
                    type="response.event", event_id="tool-item", delegation_id="typed-delegation",
                    event={"type": "response.output_item.done", "item": {
                        "type": "function_call", "call_id": "call-1", "name": "search_memory",
                        "arguments": "{}",
                    }},
                ))
                await connections[0].events.put(backend_event(
                    "response.completed", response_id="typed-response", delegation_id="typed-delegation",
                ))
                await pilot.pause()
                handle.assert_called_once()
            self.assertNotIn(None, app.backend_work.tool_response_ids)
            self.assertEqual(app.backend_work.pending, {"typed-delegation"})
            with patch.object(app, "notify") as notify:
                app.action_standby()
                self.assertIn("backend response", notify.call_args.args[0])
                self.assertNotIn("typed acknowledgement", notify.call_args.args[0])
                connections[0].session.close.assert_not_called()
            for kind in ("response.created", "response.completed"):
                await connections[0].events.put(backend_event(
                    kind, response_id="continuation-response", delegation_id="typed-delegation",
                ))
            await pilot.pause()
            self.assertFalse(app.live.pending_text_requests)
            self.assertFalse(app.backend_work.pending)
            self.assertFalse(app.backend_work.tool_response_ids)
            await pilot.click("#chat-status")
            await asyncio.wait_for(app.reconnect_worker.wait(), 3)
            self.assertTrue(app.standby)
            connections[0].session.close.assert_awaited_once()
            connections[0].response.create.assert_awaited_once()

    async def test_tool_item_without_response_id_keeps_delegation_busy(self):
        async with running_app() as (app, pilot):
            with patch.object(app.tools, "handle_function_call"):
                app.handle_live_event(backend_event("response.created"))
                app.handle_live_event(ResponseEvent(
                    type="response.event", event_id="tool", delegation_id="delegation-1",
                    event={"type": "response.output_item.done", "item": {"type": "function_call"}},
                ))
                app.handle_live_event(backend_event("response.completed"))
            self.assertEqual(app.backend_work.pending, {"delegation-1"})
            self.assertFalse(app.backend_work.tool_response_ids)

    async def test_typed_reply_without_created_event_allows_standby_after_voice_chat(self):
        for first_kind in ("response.in_progress", "response.output_item.done", "response.completed"):
            with self.subTest(first_kind=first_kind):
                async with restarting_app() as (app, pilot, connections, speakers, started):
                    # Responses from the same delegation need not all start with created.
                    for kind in ("response.created", "response.completed"):
                        await connections[0].events.put(backend_event(kind, response_id="earlier-voice"))
                    await pilot.pause()
                    composer = app.query_one("#chat-input", ChatComposer)
                    composer.load_text("A typed question after voice chat")
                    await pilot.press("enter")
                    await asyncio.wait_for(app.keyboard_worker.wait(), 3)
                    self.assertTrue(app.live.pending_text_requests)
                    with patch.object(app, "notify") as notify:
                        app.action_standby()
                        self.assertIn("typed acknowledgement", notify.call_args.args[0])
                    if first_kind != "response.completed":
                        payload = {"type": first_kind, "response": {"id": "typed-response"}}
                        if first_kind == "response.output_item.done":
                            payload = {
                                "type": first_kind, "response_id": "typed-response",
                                "item": {"type": "message", "role": "assistant", "content": []},
                            }
                        await connections[0].events.put(ResponseEvent(
                            type="response.event", event_id="typed-start", client_event_id=None,
                            delegation_id="delegation-1", event=payload,
                        ))
                        await pilot.pause()
                        self.assertFalse(app.live.pending_text_requests)
                        with patch.object(app, "notify") as notify:
                            app.action_standby()
                            self.assertIn("backend response", notify.call_args.args[0])
                            connections[0].session.close.assert_not_called()
                    await connections[0].events.put(ResponseEvent(
                        type="response.event", event_id="typed-complete", client_event_id=None,
                        delegation_id="delegation-1", event={
                            "type": "response.completed", "response": {
                                "id": "typed-response", "output": [], "tools": [], "instructions": None,
                            },
                        },
                    ))
                    await pilot.pause()
                    self.assertFalse(app.live.pending_text_requests)
                    self.assertFalse(app.backend_work.pending)
                    self.assertIn("typed_pending=0, backend_pending=0", app.activity.entries[-1])
                    await pilot.click("#chat-status")
                    await asyncio.wait_for(app.reconnect_worker.wait(), 3)
                    self.assertTrue(app.standby)
                    connections[0].session.close.assert_awaited_once()

    async def test_completed_typed_reply_releases_standby_blockers(self):
        for terminal_echo_only in (False, True):
            with self.subTest(terminal_echo_only=terminal_echo_only):
                async with restarting_app() as (app, pilot, connections, speakers, started):
                    composer = app.query_one("#chat-input", ChatComposer)
                    composer.load_text("A typed question")
                    await pilot.press("enter")
                    await asyncio.wait_for(app.keyboard_worker.wait(), 3)
                    request_id = connections[0].response.create.call_args.kwargs["event_id"]
                    self.assertTrue(app.live.pending_text_requests)
                    self.assertIn("typed-request", app.backend_work.pending)
                    created = backend_event("response.created")
                    if terminal_echo_only:
                        created.client_event_id = "unrelated-command"
                    await connections[0].events.put(created)
                    await pilot.pause()
                    with patch.object(app, "notify") as notify:
                        await pilot.click("#chat-status")
                        self.assertIn("backend response", notify.call_args.args[0])
                        self.assertFalse(app.standby)
                        connections[0].session.close.assert_not_called()
                    completed = backend_event("response.completed")
                    if terminal_echo_only:
                        completed.client_event_id = request_id
                    await connections[0].events.put(completed)
                    await pilot.pause(0.25)
                    self.assertFalse(app.live.pending_text_requests)
                    self.assertFalse(app.backend_work.pending)
                    await pilot.click("#chat-status")
                    await asyncio.wait_for(app.reconnect_worker.wait(), 3)
                    self.assertTrue(app.standby)
                    self.assertEqual(str(app.query_one("#chat-status", Button).label), "○ Standby")
                    connections[0].session.close.assert_awaited_once()

    async def test_standby_click_wakes_once_with_context_draft_and_mutes_preserved(self):
        async with restarting_app() as (app, pilot, connections, speakers, started):
            await app.live.set_microphone_muted(True)
            app.live.set_speaker_muted(True)
            app.conversation.write_log("You: Remember the blue notebook")
            composer = app.query_one("#chat-input", ChatComposer)
            composer.load_text("An unsent question")
            pending = (composer.text, uuid4())
            app.pending_keyboard_message = pending
            await pilot.click("#chat-status")
            await asyncio.wait_for(app.reconnect_worker.wait(), 3)
            # Textual suppresses repeated clicks during the button's active effect.
            await pilot.pause(0.25)
            status = app.query_one("#chat-status", Button)
            self.assertEqual(str(status.label), "○ Standby")
            self.assertFalse(status.disabled)
            self.assertIn("Click to resume", status.tooltip)
            history = app.conversation.get_reconnect_history()
            entered, release = asyncio.Event(), asyncio.Event()
            original_run = app.live.run

            async def delayed_run(*args):
                entered.set()
                await release.wait()
                await original_run(*args)

            with patch.object(app.live, "run", new=AsyncMock(side_effect=delayed_run)) as run:
                try:
                    await pilot.click("#chat-status")
                    await asyncio.wait_for(entered.wait(), 3)
                    self.assertEqual(str(status.label), "◌ Connecting…")
                    self.assertTrue(status.disabled)
                    self.assertTrue(status.has_class("state-connecting"))
                    await pilot.click("#chat-status")
                    run.assert_awaited_once()
                    self.assertEqual(len(connections), 1)
                finally:
                    release.set()
                await asyncio.wait_for(started.get(), 5)
                await pilot.pause()
                run.assert_awaited_once()
            self.assertTrue(app.live.ready)
            self.assertFalse(app.standby)
            self.assertEqual(str(status.label), "● Active")
            self.assertFalse(status.disabled)
            self.assertEqual(len(connections), 2)
            self.assertEqual(connections[1].session.start.call_args.kwargs["session"]["input"], history)
            connections[1].response.create.assert_not_called()
            self.assertEqual(composer.text, "An unsent question")
            self.assertEqual(app.pending_keyboard_message, pending)
            self.assertTrue(app.live.microphone_muted)
            self.assertTrue(app.live.speaker_muted)

    async def test_failed_click_wake_shows_disconnected_and_keeps_draft(self):
        async with running_app() as (app, pilot):
            from test_reconnect import CONNECT

            app.startup_complete = True
            app.standby = True
            app.set_connection_state("standby")
            app.connect_to_cora = lambda: CONNECT(app)
            app.live.run = AsyncMock(side_effect=RuntimeError("Transport failed"))
            composer = app.query_one("#chat-input", ChatComposer)
            composer.load_text("My draft")
            await pilot.click("#chat-status")
            await asyncio.wait_for(app.reconnect_worker.wait(), 3)
            await asyncio.wait_for(app.connection_worker.wait(), 3)
            status = app.query_one("#chat-status", Button)
            self.assertEqual(str(status.label), "○ Disconnected")
            self.assertTrue(status.disabled)
            self.assertFalse(app.standby)
            self.assertEqual(composer.text, "My draft")
            self.assertFalse(composer.disabled)
            app.live.send_text.assert_not_called()

    async def test_non_silent_queue_blocks_standby_before_playback_starts(self):
        async with running_app() as (app, pilot):
            app.live.ready = True
            speaker = Speaker()
            app.live.speaker = speaker
            with patch("devices.speaker.sd.RawOutputStream"), patch.object(app, "notify") as notify:
                try:
                    speaker.play(base64.b64encode(bytes(960)).decode("ascii"))
                    speaker.play(base64.b64encode(b"\x00\x20" * 480).decode("ascii"))
                    self.assertEqual(speaker.get_playback_level(), 0.0)
                    app.action_standby()
                    app.live.close.assert_not_called()
                    self.assertIn("audio playback", notify.call_args.args[0])
                    self.assertIn("audio playback", app.activity.entries[-1])
                finally:
                    speaker.close()
                    app.live.speaker = None

    async def test_silent_output_stream_and_queued_silence_do_not_block_standby(self):
        async with running_app() as (app, pilot):
            app.live.ready = True
            speaker = Speaker()
            app.live.speaker = speaker
            silence = base64.b64encode(bytes(960)).decode("ascii")
            with patch("devices.speaker.sd.RawOutputStream"), \
                    patch("app.monotonic", return_value=100.0), \
                    patch.object(app, "reconnect_live", return_value=None) as transition:
                try:
                    speaker.play(silence)
                    app.handle_live_event(SimpleNamespace(type="session.output_audio.delta", delta=silence))
                    app.action_standby()
                    transition.assert_called_once_with(restart=False)
                    self.assertEqual(app.last_live_activity, 0.0)
                finally:
                    speaker.close()
                    app.live.speaker = None

    async def test_active_click_closes_live_keeps_local_state_and_ctrl_r_resumes(self):
        async with restarting_app() as (app, pilot, connections, speakers, started):
            owners = (app.persistence, app.conversation, app.tools, app.live)
            conversation_id = app.persistence.conversation_id
            await app.live.set_microphone_muted(True)
            app.live.set_speaker_muted(True)
            app.conversation.write_log("You: Keep the green notebook")
            app.conversation.append_transcript("Cora", "The notebook is green")
            app.tools.handled_call_ids.add("already-completed")
            composer = app.query_one("#chat-input", ChatComposer)
            composer.load_text("My unsent draft")
            pending = ("My unsent draft", uuid4())
            app.pending_keyboard_message = pending
            status = app.query_one("#chat-status", Button)
            active_colour = status.styles.color
            self.assertFalse(status.disabled)
            with patch.object(app, "exit") as exit_app:
                await pilot.click("#chat-status")
                await asyncio.wait_for(app.reconnect_worker.wait(), 3)
                await pilot.pause()
                self.assertTrue(app.standby)
                self.assertFalse(app.closing)
                self.assertFalse(app.reconnecting)
                self.assertTrue(app.live.session_close_confirmed)
                self.assertIsNone(app.live.connection)
                self.assertIsNone(app.live.speaker)
                self.assertIsNone(app.live.microphone_task)
                self.assertFalse(app.live.ready)
                self.assertEqual(len(connections), 1)
                connections[0].session.close.assert_awaited_once()
                self.assertTrue(speakers[0].close.called)
                self.assertEqual(str(status.label), "○ Standby")
                self.assertFalse(status.disabled)
                self.assertTrue(status.has_class("state-standby"))
                self.assertNotEqual(status.styles.color, active_colour)
                self.assertIn("Ctrl+R", status.tooltip)
                self.assertEqual(app.query_one("#speaker-level", Static).content, "Muted")
                self.assertTrue(app.live.microphone_muted)
                self.assertTrue(app.live.speaker_muted)
                self.assertEqual(composer.text, "My unsent draft")
                self.assertFalse(composer.disabled)
                self.assertEqual(app.pending_keyboard_message, pending)
                self.assertEqual((app.persistence, app.conversation, app.tools, app.live), owners)
                self.assertEqual(app.persistence.conversation_id, conversation_id)
                app.persistence.save_chat_message.assert_called_once()
                app.persistence.finish_conversation.assert_not_called()
                app.persistence.close.assert_not_called()
                exit_app.assert_not_called()

                with patch.object(app, "notify") as notify:
                    await pilot.press("enter")
                    self.assertIn("Ctrl+R", notify.call_args.args[0])
                app.persistence.save_message.assert_not_called()
                connections[0].response.create.assert_not_called()
                self.assertEqual(composer.text, "My unsent draft")
                self.assertEqual(app.pending_keyboard_message, pending)
                history = app.conversation.get_reconnect_history()
                await pilot.press("ctrl+r")
                await asyncio.wait_for(app.reconnect_worker.wait(), 3)
                await asyncio.wait_for(started.get(), 5)
                await pilot.pause()
                self.assertFalse(app.standby)
                self.assertFalse(app.live.session_close_confirmed)
                self.assertTrue(app.live.ready)
                self.assertEqual(str(status.label), "● Active")
                self.assertFalse(status.disabled)
                self.assertEqual(len(connections), 2)
                self.assertEqual(connections[1].session.start.call_args.kwargs["session"]["input"], history)
                connections[1].response.create.assert_not_called()
                self.assertEqual(composer.text, "My unsent draft")

    async def test_known_pending_work_blocks_standby_without_closing_live(self):
        async with running_app() as (app, pilot):
            app.live.ready = True
            cases = [
                ("backend", lambda: app.backend_work.pending.add("pending")),
                ("typed acknowledgement", lambda: app.live.pending_text_requests.add("typed")),
                ("keyboard submission", lambda: setattr(app, "keyboard_worker", MagicMock(is_finished=False))),
                ("recent speech", lambda: app.handle_live_event(SimpleNamespace(
                    type="session.output_transcript.delta", delta="Speaking",
                ))),
            ]
            with patch.object(app, "notify") as notify:
                for name, arrange in cases:
                    with self.subTest(name):
                        arrange()
                        app.action_standby()
                        app.live.close.assert_not_called()
                        self.assertFalse(app.standby)
                        self.assertFalse(app.reconnecting)
                        self.assertIn("enter standby", notify.call_args.args[0])
                        app.backend_work.clear()
                        app.live.pending_text_requests.clear()
                        app.keyboard_worker = None
                        app.last_live_activity = 0.0

    async def test_close_failure_or_unconfirmed_close_is_not_standby(self):
        for error in (None, RuntimeError("private close details")):
            with self.subTest(error=type(error).__name__):
                async with running_app() as (app, pilot):
                    app.live.ready = True
                    app.live.close = AsyncMock(side_effect=error)
                    with patch.object(app, "connect_to_cora") as connect:
                        app.action_standby()
                        await asyncio.wait_for(app.reconnect_worker.wait(), 3)
                        connect.assert_not_called()
                    self.assertFalse(app.standby)
                    self.assertFalse(app.reconnecting)
                    self.assertEqual(str(app.query_one("#chat-status", Button).label), "○ Disconnected")
                    self.assertIn("Standby failed", "\n".join(app.activity.entries))
                    self.assertNotIn("private close details", "\n".join(app.activity.entries))
                    app.persistence.finish_conversation.assert_not_called()
                    app.persistence.close.assert_not_called()

    async def test_quit_during_standby_close_does_not_claim_standby_or_restart(self):
        async with running_app() as (app, pilot):
            app.live.ready = True
            entered, release = asyncio.Event(), asyncio.Event()

            async def close():
                entered.set()
                await release.wait()

            app.live.close = AsyncMock(side_effect=close)
            with patch.object(app, "connect_to_cora") as connect, patch.object(app, "exit") as exit_app:
                app.action_standby()
                await asyncio.wait_for(entered.wait(), 3)
                app.action_standby()
                app.action_reconnect()
                self.assertEqual(app.live.close.await_count, 1)
                self.assertTrue(app.query_one("#chat-status", Button).disabled)
                quit_task = asyncio.create_task(app.action_disconnect_and_quit())
                await pilot.pause()
                release.set()
                await asyncio.wait_for(quit_task, 3)
                self.assertFalse(app.standby)
                self.assertTrue(app.closing)
                connect.assert_not_called()
                exit_app.assert_called_once()


class PlaybackBusyTests(unittest.TestCase):
    def test_silence_is_idle_but_audible_queue_and_callback_remain_busy_until_silence(self):
        speaker = Speaker()
        silence = base64.b64encode(bytes(4)).decode("ascii")
        speech = base64.b64encode(b"\x00\x20" * 2).decode("ascii")
        with patch("devices.speaker.sd.RawOutputStream"):
            speaker.play(silence)
            self.assertFalse(speaker.is_playback_busy())
            speaker.play(speech)
            self.assertTrue(speaker.is_playback_busy())
            speaker.callback(bytearray(4), 2, None, None)
            self.assertTrue(speaker.is_playback_busy())
            speaker.callback(bytearray(4), 2, None, None)
            self.assertTrue(speaker.is_playback_busy())
            speaker.callback(bytearray(4), 2, None, None)
            self.assertFalse(speaker.is_playback_busy())
            speaker.play(speech)
            speaker.close()
            self.assertFalse(speaker.is_playback_busy())
            speaker.play(silence)
            self.assertFalse(speaker.is_playback_busy())
            speaker.close()
    def test_full_queue_rejection_does_not_leave_phantom_audible_samples(self):
        speaker = Speaker()
        silence = base64.b64encode(bytes(2)).decode("ascii")
        with patch("devices.speaker.sd.RawOutputStream"):
            for _ in range(250):
                speaker.play(silence)
            with self.assertRaisesRegex(RuntimeError, "backing up"):
                speaker.play(base64.b64encode(b"\x00\x20").decode("ascii"))
            self.assertFalse(speaker.is_playback_busy())
            speaker.close()


if __name__ == "__main__":
    unittest.main()