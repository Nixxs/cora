"""Route Live content and backend events independently of the Textual app."""


class LiveEventHandler:
    def __init__(
        self, *, live, backend_work, conversation, audio_status, tools,
        write_activity, cancel_standby_alert, record_idle_activity, handle_session_event,
    ):
        self.live = live
        self.backend_work = backend_work
        self.conversation = conversation
        self.audio_status = audio_status
        self.tools = tools
        self.write_activity = write_activity
        self.cancel_standby_alert = cancel_standby_alert
        self.record_idle_activity = record_idle_activity
        self.handle_session_event = handle_session_event

    def handle(self, event) -> None:
        if event.type == "session.input_transcript.delta":
            self.audio_status.speech_received()
            self.conversation.append_transcript("You", event.delta)
        elif event.type == "session.output_transcript.delta":
            self.conversation.append_transcript("Cora", event.delta)
        elif event.type == "session.thinking.appended":
            command_id = event.client_event_id
            if command_id and command_id.startswith("typed_context_"):
                self.write_activity(
                    f"Live accepted typed context: id={command_id}",
                    "Typed context accepted",
                )
        elif event.type == "session.delegation.created":
            self.cancel_standby_alert("New backend work; staying active.")
            delegation = event.delegation
            self.backend_work.add_delegation(delegation.id, getattr(delegation, "response_id", None))
            self.write_activity(
                f"Delegation requested: "
                f"target={delegation.target}, id={delegation.id}",
                "Delegation requested",
            )
        elif event.type == "response.event":
            self.handle_backend_event(event)
        else:
            # Connection, wake and standby state remain owned by the app.
            self.handle_session_event(event)

    def handle_backend_event(self, event) -> None:
        # Live wraps backend events in a dictionary, not an event object.
        backend_event = event.event
        event_type = backend_event.get("type")
        response_id = self.backend_work.handle_response_event(
            event.delegation_id, backend_event,
            typed_requests_pending=bool(self.live.pending_text_requests),
        )
        if event_type in ("response.created", "response.in_progress", "response.output_item.done"):
            self.cancel_standby_alert("New backend work; staying active.")
        elif event_type in ("response.completed", "response.failed", "response.incomplete"):
            self.record_idle_activity()
        if event_type == "response.output_item.done":
            # Live lifecycle snapshots omit output; completed items carry it.
            item = backend_event.get("item") or {}
            if item.get("type") == "function_call":
                self.tools.handle_function_call(item)
            else:
                self.conversation.display_written_item(item)
        elif event_type in (
            "response.created", "response.completed", "response.failed", "response.incomplete", "error",
        ):
            response = backend_event.get("response") or {}
            summary = {
                "response.created": "Luna started",
                "response.completed": "Luna response complete",
                "response.failed": "Backend failed",
                "response.incomplete": "Backend incomplete",
                "error": "Backend error",
            }[event_type]
            self.write_activity(
                f"Backend {event_type}: "
                f"delegation_id={event.delegation_id}, "
                f"response_id={response_id}, "
                f"client_event_id={getattr(event, 'client_event_id', None)}, "
                f"typed_pending={len(self.live.pending_text_requests)}, "
                f"backend_pending={len(self.backend_work.pending)}",
                summary,
            )
            error = response.get("error") or backend_event.get("error")
            if error:
                self.write_activity(f"Backend error: {error}", "Backend error")
            if event_type == "error" and not error:
                self.write_activity(
                    f"Backend error: {backend_event.get('message')}", "Backend error",
                )
            details = response.get("incomplete_details")
            if details:
                self.write_activity(f"Backend incomplete: {details}", "Backend incomplete")
        elif event_type in (
            "response.web_search_call.in_progress",
            "response.web_search_call.searching",
            "response.web_search_call.completed",
        ):
            summary = {
                "response.web_search_call.in_progress": "Search started",
                "response.web_search_call.searching": "Searching the web",
                "response.web_search_call.completed": "Search complete",
            }[event_type]
            self.write_activity(
                f"Search {event_type}: "
                f"delegation_id={event.delegation_id}, "
                f"item_id={backend_event.get('item_id')}",
                summary,
            )