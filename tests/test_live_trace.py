import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from live import CoraLive
from storage.live_trace import LiveTrace
from test_audio_controls import live_transport
from test_keyboard_persistence import running_app
from ui.composer import ChatComposer


class LiveTraceTests(unittest.IsolatedAsyncioTestCase):
    async def test_trace_correlates_sends_and_reports_delegation_exclusion_without_content(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "live-trace.log"
            trace = LiveTrace(path)
            live = CoraLive(on_trace=trace.write)
            async with live_transport(live) as (connection, speaker, emit):
                await live.send_text("PRIVATE_TYPED_TEXT")
                request_id = connection.response.create.call_args.kwargs["event_id"]
                context_id = connection.session.thinking.append.call_args.kwargs["event_id"]
                await emit("session.thinking.appended", client_event_id=context_id, content="PRIVATE_CONTEXT")
                await emit("session.input_transcript.delta", delta="PRIVATE_TRANSCRIPT", start_ms=1, end_ms=2)
                await emit("session.output_audio.delta", delta="PRIVATE_AUDIO")
                await emit("session.delegation.created", delegation=SimpleNamespace(
                    id="delegation", target="responses", response_id="announced-response",
                ))
                await emit("response.event", delegation_id="delegation", event={
                    "type": "response.created", "response": {
                        "id": "announced-response", "instructions": "PRIVATE_INSTRUCTIONS",
                    },
                })
                await emit("response.event", delegation_id="delegation", event={
                    "type": "response.output_text.delta", "delta": "PRIVATE_OUTPUT",
                })
                await emit("response.event", delegation_id="delegation", event={
                    "type": "response.output_item.done", "response_id": "announced-response",
                    "item": {
                        "id": "item", "type": "function_call", "call_id": "call",
                        "arguments": "PRIVATE_ARGUMENTS", "output": "PRIVATE_TOOL_RESULT",
                    },
                })
                await emit("response.event", delegation_id="delegation", event={
                    "type": "response.completed", "response": {"id": "announced-response"},
                })
                # Record the current defect, do not alter matching to make it pass.
                self.assertEqual(live.pending_text_requests, {request_id})
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("PRIVATE_", raw)
            records = [json.loads(line) for line in raw.splitlines()]
            self.assertEqual([record["sequence"] for record in records], list(range(1, len(records) + 1)))
            sends = [record for record in records if record["stage"] == "send.before"]
            self.assertEqual([record["event_type"] for record in sends], [
                "session.thinking.append", "response.item.create", "response.create",
            ])
            self.assertEqual(sends[-1]["client_event_id"], request_id)
            self.assertEqual(sends[-1]["submission_id"], context_id)
            created = [record for record in records if record.get("backend_event_type") == "response.created"]
            self.assertEqual([record["stage"] for record in created], ["receive.before", "receive.after"])
            self.assertTrue(created[0]["response_already_seen"])
            self.assertEqual(created[1]["pending_text_requests"], [request_id])
            self.assertEqual(records[-1]["stage"], "transport.cleaned_up")
            self.assertEqual(records[-1]["pending_text_requests"], [])

    async def test_file_failure_does_not_change_sending_or_response_processing(self):
        with TemporaryDirectory() as directory:
            trace = LiveTrace(Path(directory) / "missing-directory" / "trace.log")
            live = CoraLive(on_trace=trace.write)
            async with live_transport(live) as (connection, speaker, emit):
                self.assertTrue(trace.failed)
                await live.send_text("Still sent")
                connection.response.create.assert_awaited_once()
                request_id = connection.response.create.call_args.kwargs["event_id"]
                await emit("response.event", client_event_id=request_id, event={"type": "response.completed"})
                self.assertFalse(live.pending_text_requests)

    async def test_failed_send_records_command_id_without_exception_message(self):
        with TemporaryDirectory() as directory:
            trace = LiveTrace(Path(directory) / "trace.log")
            live = CoraLive(on_trace=trace.write)
            async with live_transport(live) as (connection, speaker, emit):
                connection.response.create = AsyncMock(side_effect=RuntimeError("PRIVATE_ERROR_DETAIL"))
                with self.assertRaises(RuntimeError):
                    await live.send_text("PRIVATE_TYPED_TEXT")
                self.assertFalse(live.pending_text_requests)
            raw = trace.path.read_text(encoding="utf-8")
            self.assertNotIn("PRIVATE_", raw)
            failure = next(json.loads(line) for line in raw.splitlines() if json.loads(line)["stage"] == "send.failed")
            self.assertEqual(failure["error_type"], "RuntimeError")
            self.assertTrue(failure["client_event_id"].startswith("typed_response_"))

    async def test_app_trace_records_actual_pending_ids_and_standby_blockers(self):
        with TemporaryDirectory() as directory:
            async with running_app() as (app, pilot):
                self.assertIsNone(app.live_trace)
                app.live_trace = LiveTrace(Path(directory) / "trace.log")
                composer = app.query_one("#chat-input", ChatComposer)
                composer.load_text("PRIVATE_TYPED_TEXT")
                await pilot.press("enter")
                await asyncio.wait_for(app.keyboard_worker.wait(), 3)
                app.live.ready = True
                app.live.pending_text_requests.add("typed-awaiting")
                with patch.object(app, "notify"):
                    app.action_standby()
                self.assertFalse(app.reconnecting)
                records = [json.loads(line) for line in app.live_trace.path.read_text(encoding="utf-8").splitlines()]
                self.assertEqual([record["stage"] for record in records], ["typed.submit", "typed.sent", "transition.request"])
                self.assertEqual(records[-1]["pending_backend_work"], ["typed-request"])
                self.assertEqual(records[-1]["pending_text_requests"], ["typed-awaiting"])
                self.assertIn("typed acknowledgement", records[-1]["blockers"])
                self.assertNotIn("PRIVATE_", app.live_trace.path.read_text(encoding="utf-8"))

    def test_new_launch_replaces_previous_trace_and_invalid_record_disables_writer(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "trace.log"
            path.write_text("Previous launch", encoding="utf-8")
            trace = LiveTrace(path)
            trace.write("app.started")
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 1)
            trace.write("bad.record", invalid=object())
            self.assertTrue(trace.failed)
            trace.write("ignored")
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 1)


if __name__ == "__main__":
    unittest.main()