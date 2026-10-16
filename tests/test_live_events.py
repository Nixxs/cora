"""Live content routing without a Textual app or network connection."""

from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, call

from backend_work import BackendWorkTracker
from live_events import LiveEventHandler


class LiveEventHandlerTests(unittest.TestCase):
    def setUp(self):
        self.live = SimpleNamespace(pending_text_requests=set())
        self.backend_work = BackendWorkTracker()
        self.conversation = MagicMock()
        self.audio_status = MagicMock()
        self.tools = MagicMock()
        self.write = MagicMock()
        self.cancel_alert = MagicMock()
        self.record_idle = MagicMock()
        self.session_event = MagicMock()
        self.handler = LiveEventHandler(
            live=self.live, backend_work=self.backend_work, conversation=self.conversation,
            audio_status=self.audio_status, tools=self.tools, write_activity=self.write,
            cancel_standby_alert=self.cancel_alert, record_idle_activity=self.record_idle,
            handle_session_event=self.session_event,
        )

    def response(self, kind, **payload):
        self.handler.handle(SimpleNamespace(
            type="response.event", delegation_id="delegation-1", client_event_id="command-1",
            event={"type": kind, "response_id": "response-1", **payload},
        ))

    def test_input_indicator_precedes_user_transcript_and_output_uses_cora_role(self):
        order = MagicMock()
        order.attach_mock(self.audio_status.speech_received, "speech")
        order.attach_mock(self.conversation.append_transcript, "append")
        self.handler.handle(SimpleNamespace(type="session.input_transcript.delta", delta="Hello"))
        self.handler.handle(SimpleNamespace(type="session.output_transcript.delta", delta="Hi"))
        self.assertEqual(order.mock_calls, [
            call.speech(), call.append("You", "Hello"), call.append("Cora", "Hi"),
        ])
        self.session_event.assert_not_called()

    def test_only_typed_context_acknowledgements_are_logged(self):
        for command_id in (None, "other-command", "typed_context_123"):
            self.handler.handle(SimpleNamespace(type="session.thinking.appended", client_event_id=command_id))
        self.write.assert_called_once_with(
            "Live accepted typed context: id=typed_context_123", "Typed context accepted",
        )

    def test_delegation_cancels_alert_before_tracking_and_logging(self):
        self.cancel_alert.side_effect = lambda *_: self.assertFalse(self.backend_work.pending)
        self.handler.handle(SimpleNamespace(
            type="session.delegation.created",
            delegation=SimpleNamespace(id="delegation-1", target="responses", response_id="response-1"),
        ))
        self.cancel_alert.assert_called_once_with("New backend work; staying active.")
        self.assertEqual(self.backend_work.pending, {"delegation-1"})
        self.assertEqual(self.backend_work.response_ids, {"delegation-1": "response-1"})
        self.write.assert_called_once_with(
            "Delegation requested: target=responses, id=delegation-1", "Delegation requested",
        )

    def test_tool_is_tracked_before_execution_and_continuation_remains_pending(self):
        item = {"type": "function_call", "name": "search_memory", "call_id": "call-1"}

        def execute(received):
            self.assertIs(received, item)
            self.assertEqual(self.backend_work.tool_response_ids, {"response-1"})
            self.assertEqual(self.backend_work.pending, {"delegation-1", "response-1"})
            self.cancel_alert.assert_called_once()

        self.tools.handle_function_call.side_effect = execute
        self.response("response.output_item.done", item=item)
        self.response("response.completed")
        self.tools.handle_function_call.assert_called_once_with(item)
        self.conversation.display_written_item.assert_not_called()
        self.assertEqual(self.backend_work.pending, {"delegation-1"})

    def test_completed_items_are_rendered_not_lifecycle_snapshots(self):
        item = {"id": "item-1", "type": "message", "role": "assistant", "content": []}
        self.response("response.output_item.done", item=item)
        self.response("response.completed", response={"id": "response-1", "output": [item]})
        self.conversation.display_written_item.assert_called_once_with(item)
        self.tools.handle_function_call.assert_not_called()

    def test_terminal_events_record_idle_before_logging_and_release_work(self):
        for kind in ("response.completed", "response.failed", "response.incomplete"):
            with self.subTest(kind=kind):
                self.backend_work.start_typed_request()
                order = MagicMock()
                order.attach_mock(self.record_idle, "idle")
                order.attach_mock(self.write, "write")
                self.response(kind)
                self.assertFalse(self.backend_work.pending)
                self.assertEqual(order.mock_calls[0], call.idle())
                self.assertIn("typed_pending=0, backend_pending=0", order.mock_calls[1].args[0])

    def test_backend_errors_and_incomplete_details_preserve_activity_messages(self):
        self.response("response.incomplete", response={
            "id": "response-1", "error": {"code": "failed"},
            "incomplete_details": {"reason": "max_output_tokens"},
        })
        self.assertIn(call("Backend error: {'code': 'failed'}", "Backend error"), self.write.call_args_list)
        self.assertIn(call(
            "Backend incomplete: {'reason': 'max_output_tokens'}", "Backend incomplete",
        ), self.write.call_args_list)
        self.response("error", message="Backend unavailable")
        self.write.assert_called_with("Backend error: Backend unavailable", "Backend error")
        self.session_event.assert_not_called()

    def test_search_progress_does_not_render_chat_or_finish_a_typed_request(self):
        self.backend_work.start_typed_request()
        for kind, summary in (
            ("response.web_search_call.in_progress", "Search started"),
            ("response.web_search_call.searching", "Searching the web"),
            ("response.web_search_call.completed", "Search complete"),
        ):
            self.response(kind, item_id="search-1")
            self.write.assert_called_with(
                f"Search {kind}: delegation_id=delegation-1, item_id=search-1", summary,
            )
        self.assertEqual(self.backend_work.pending, {"typed-request"})
        self.conversation.display_written_item.assert_not_called()

    def test_lifecycle_and_unrecognised_events_are_left_to_app_callback(self):
        for kind in ("session.started", "session.commentary.appended", "error", "session.closed", "unknown"):
            with self.subTest(kind=kind):
                event = SimpleNamespace(type=kind)
                self.handler.handle(event)
                self.session_event.assert_called_with(event)
        self.write.assert_not_called()
        self.record_idle.assert_not_called()
        self.cancel_alert.assert_not_called()


if __name__ == "__main__":
    unittest.main()