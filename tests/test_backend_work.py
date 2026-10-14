import unittest

from backend_work import BackendWorkTracker


class BackendWorkTrackerTests(unittest.TestCase):
    def setUp(self):
        self.tracker = BackendWorkTracker()

    def response(self, kind, *, response_id="response-1", delegation_id="delegation-1",
                 typed_requests_pending=False, **payload):
        event = {"type": kind, **payload}
        if response_id is not None:
            event["response_id"] = response_id
        return self.tracker.handle_response_event(
            delegation_id, event, typed_requests_pending=typed_requests_pending,
        )

    def test_delegation_metadata_resolves_item_without_response_id(self):
        self.tracker.add_delegation("delegation-1", "response-1")
        resolved = self.response("response.output_item.done", response_id=None,
                                 item={"type": "function_call"})
        self.assertEqual(resolved, "response-1")
        self.assertEqual(self.tracker.pending, {"delegation-1", "response-1"})
        self.assertEqual(self.tracker.tool_response_ids, {"response-1"})

    def test_lifecycle_can_start_without_created_event(self):
        for first in ("response.created", "response.in_progress", "response.output_item.done",
                      "response.completed", "response.failed", "response.incomplete"):
            with self.subTest(first=first):
                self.tracker.clear()
                self.tracker.start_typed_request()
                self.response(first)
                self.assertNotIn("typed-request", self.tracker.pending)
                if first in ("response.created", "response.in_progress", "response.output_item.done"):
                    self.assertEqual(self.tracker.pending, {"delegation-1", "response-1"})
                    self.response("response.completed")
                self.assertFalse(self.tracker.pending)
                self.assertFalse(self.tracker.response_ids)

    def test_pending_acknowledgement_keeps_typed_request_marker(self):
        self.tracker.start_typed_request()
        self.response("response.completed", typed_requests_pending=True)
        self.assertEqual(self.tracker.pending, {"typed-request"})
        self.tracker.finish_typed_request()
        self.tracker.finish_typed_request()
        self.assertFalse(self.tracker.pending)

    def test_tool_completion_waits_for_continuation(self):
        self.response("response.created")
        self.response("response.output_item.done", item={"type": "function_call"})
        self.response("response.completed")
        self.assertEqual(self.tracker.pending, {"delegation-1"})
        self.assertEqual(self.tracker.response_ids, {"delegation-1": "response-1"})
        self.assertFalse(self.tracker.tool_response_ids)
        self.response("response.created", response_id="continuation")
        self.response("response.completed", response_id="continuation")
        self.assertFalse(self.tracker.pending)
        self.assertFalse(self.tracker.response_ids)

    def test_failed_or_incomplete_tool_response_does_not_wait_for_continuation(self):
        for terminal in ("response.failed", "response.incomplete"):
            with self.subTest(terminal=terminal):
                self.response("response.output_item.done", item={"type": "function_call"})
                self.response(terminal)
                self.assertFalse(self.tracker.pending)
                self.assertFalse(self.tracker.response_ids)
                self.assertFalse(self.tracker.tool_response_ids)

    def test_nested_response_id_takes_precedence_and_unrelated_work_remains(self):
        self.tracker.add_delegation("other", "other-response")
        resolved = self.response("response.created", response={"id": "nested"})
        self.assertEqual(resolved, "nested")
        self.response("response.completed", response={"id": "nested"})
        self.assertEqual(self.tracker.pending, {"other"})
        self.assertEqual(self.tracker.response_ids, {"other": "other-response"})

    def test_unrelated_events_do_not_clear_typed_request(self):
        for event_type in ("error", "response.web_search_call.completed", "unknown"):
            with self.subTest(event_type=event_type):
                self.tracker.start_typed_request()
                self.response(event_type, response_id=None)
                self.assertEqual(self.tracker.pending, {"typed-request"})

    def test_tool_without_any_response_metadata_does_not_invent_a_response_id(self):
        self.response("response.output_item.done", response_id=None, item={"type": "function_call"})
        self.assertEqual(self.tracker.pending, {"delegation-1"})
        self.assertEqual(self.tracker.tool_response_ids, {None})
        self.response("response.completed", response_id=None)
        self.assertFalse(self.tracker.pending)
        self.assertFalse(self.tracker.tool_response_ids)

    def test_clear_resets_all_state_without_affecting_other_instances(self):
        other = BackendWorkTracker()
        other.start_typed_request()
        self.tracker.start_typed_request()
        self.response("response.output_item.done", item={"type": "function_call"})
        self.tracker.clear()
        self.assertFalse(self.tracker.pending)
        self.assertFalse(self.tracker.response_ids)
        self.assertFalse(self.tracker.tool_response_ids)
        self.assertEqual(other.pending, {"typed-request"})


if __name__ == "__main__":
    unittest.main()