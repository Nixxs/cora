"""Track pending backend work without UI, transport, or tool execution."""


class BackendWorkTracker:
    def __init__(self):
        self.pending = set()
        self.response_ids = {}
        self.tool_response_ids = set()

    def start_typed_request(self) -> None:
        self.pending.add("typed-request")

    def finish_typed_request(self) -> None:
        self.pending.discard("typed-request")

    def add_delegation(self, delegation_id: str, response_id: str | None = None) -> None:
        self.pending.add(delegation_id)
        if response_id:
            self.response_ids[delegation_id] = response_id

    def handle_response_event(
        self, delegation_id: str | None, event: dict, *, typed_requests_pending: bool,
    ) -> str | None:
        event_type = event.get("type")
        response_id = (event.get("response") or {}).get("id") or event.get("response_id")
        # Forwarded output items can omit response_id; their delegation
        # identifies the response announced by metadata/lifecycle events.
        if response_id and delegation_id is not None:
            self.response_ids[delegation_id] = response_id
        elif not response_id:
            response_id = self.response_ids.get(delegation_id)
        if event_type in (
            "response.created", "response.in_progress", "response.output_item.done",
            "response.completed", "response.failed", "response.incomplete",
        ):
            if not typed_requests_pending:
                self.finish_typed_request()
        if event_type in ("response.created", "response.in_progress", "response.output_item.done"):
            self.pending.add(delegation_id)
            self.pending.add(response_id or delegation_id)
        elif event_type in ("response.completed", "response.failed", "response.incomplete"):
            self.pending.discard(response_id or delegation_id)
            # A completed response with function calls still needs a tool continuation.
            if event_type != "response.completed" or response_id not in self.tool_response_ids:
                self.pending.discard(delegation_id)
                self.response_ids.pop(delegation_id, None)
            self.tool_response_ids.discard(response_id)
        if event_type == "response.output_item.done":
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                self.pending.add(delegation_id)
                self.tool_response_ids.add(response_id)
        return response_id

    def clear(self) -> None:
        self.pending.clear()
        self.tool_response_ids.clear()
        self.response_ids.clear()