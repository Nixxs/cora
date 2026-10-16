"""Local wake lifecycle, UI status, and opening-speech handoff."""

from .detector import WAKE_NAMES
from .listener import WakeListener


class WakeController:
    def __init__(
        self, *, live, control, enabled: bool, debug: bool,
        clock, can_listen, is_closing, is_reconnecting, request_connection,
        abort_connection, record_idle_activity, transition_blockers, enter_standby,
        write_activity, set_timer, set_interval,
    ):
        self.live = live
        self.control = control
        self.enabled = enabled
        self.debug = debug
        self.clock = clock
        self.can_listen = can_listen
        self.is_closing = is_closing
        self.is_reconnecting = is_reconnecting
        self.request_connection = request_connection
        self.abort_connection = abort_connection
        self.record_idle_activity = record_idle_activity
        self.transition_blockers = transition_blockers
        self.enter_standby = enter_standby
        self.write_activity = write_activity
        self.set_timer = set_timer
        self.listener = None
        self.listening = False
        self.failed = False
        self.reconnecting = False
        self.retry_at = 0.0
        self.response_deadline = None
        self.transcript = ""
        self.connection_timer = None
        self.idle_timer = set_interval(1.0, self.check_timeout)

    def update_control(self) -> None:
        button = self.control
        if not button.is_mounted:
            return
        failed = self.failed or (self.listener is not None and self.listener.error is not None)
        button.label = "Wake error" if failed else "Wake on" if self.enabled else "Wake off"
        button.tooltip = (
            "Wake listener unavailable. See Activity; toggle off/on to retry." if failed
            else "Privacy muted: all microphone capture is off." if self.live.microphone_muted
            else "Listening locally for Hey Cora; no audio sent to OpenAI." if self.listening
            else "Wake enabled; listens locally only in standby." if self.enabled
            else "Enable local Hey Cora listening in standby."
        )
        button.set_class(self.listening, "wake-listening")
        button.disabled = self.is_closing()
        button.refresh(layout=True)

    def start_listener(self) -> None:
        if (not self.enabled or self.failed or not self.can_listen()
                or self.live.microphone_muted or self.live.connection is not None
                or self.listener is not None):
            self.update_control()
            return
        self.listener = WakeListener(
            self.handle_wake, self.handle_status,
            cooldown=max(0.0, self.retry_at - self.clock()),
            on_debug=self.handle_diagnostic if self.debug else None,
        )
        self.listener.start()
        self.update_control()

    async def stop_listener(self) -> None:
        listener = self.listener
        self.listener = None
        self.listening = False
        if listener is not None:
            await listener.stop()
            if self.live.opening_microphone is listener:
                self.live.opening_microphone = None
        self.update_control()

    def handle_status(self, working: bool, message: str) -> None:
        if self.is_closing():
            return
        self.listening = working
        if not working:
            self.failed = True
        self.write_activity(message, "Local wake ready" if working else "Local wake unavailable")
        self.update_control()
        if not working and self.reconnecting:
            self.abort_connection()

    def handle_diagnostic(self, message: str) -> None:
        if not self.is_closing() and not self.live.microphone_muted and self.enabled:
            # Temporary local-only diagnostics: not chat, database, or Live trace.
            self.write_activity(f"[wake] {message}", "Local wake diagnostics")

    def handle_wake(self, listener: WakeListener) -> bool:
        if (listener is not self.listener or not self.enabled
                or self.live.microphone_muted or not self.can_listen()):
            return False
        self.listening = False
        self.retry_at = self.clock() + 5
        self.live.opening_microphone = listener
        self.record_idle_activity()
        if not self.request_connection():
            self.live.opening_microphone = None
            return False
        self.write_activity("Hey Cora detected locally; buffering opening speech.", "Local wake detected")
        self.connection_timer = self.set_timer(15.0, self.abort_connection)
        self.update_control()
        return True

    def check_timeout(self) -> None:
        deadline = self.response_deadline
        if deadline is None or self.is_closing() or self.is_reconnecting() or not self.live.ready:
            return
        if self.clock() >= deadline and not self.transition_blockers():
            self.response_deadline = None
            self.write_activity("No request after wake; returning to standby.", "Possible false wake")
            self.enter_standby()

    def session_started(self) -> None:
        if self.reconnecting:
            self.reconnecting = False
            self.response_deadline = self.clock() + 10
            self.transcript = ""
            if self.connection_timer is not None:
                self.connection_timer.stop()

    def has_request(self, delta: str) -> bool:
        if self.response_deadline is None:
            return True
        self.transcript += delta
        phrase = " ".join("".join(
            char for char in self.transcript.casefold() if char.isalnum() or char.isspace()
        ).split())
        # A transcript of the wake phrase alone is not a request.
        return bool(phrase) and not any(
            keyword.startswith(phrase) for name in WAKE_NAMES for keyword in (f"hey {name}", name)
        )

    def stop_timers(self) -> None:
        self.idle_timer.stop()
        if self.connection_timer is not None:
            self.connection_timer.stop()