"""Connecting Live, standby and reconnecting: part of CoraApp (see app.py)."""

import asyncio
from time import monotonic

from textual.widgets import Button
from textual import work

from ui.composer import ChatComposer
from inactivity import StandbyAlert
from app_areas.shared import CONVERSATION_END_STANDBY_SECONDS


# Cora's own standby request waits for her sign-off; give up rather than surprise the person later.
REQUESTED_STANDBY_TIMEOUT_SECONDS = 30


class LiveSessionMixin:
    """Connecting Live, standby and reconnecting."""

    def check_inactivity(self) -> None:
        self.inactivity.check()

    @work(group="standby-alert")
    async def send_standby_alert(self, alert: StandbyAlert) -> None:
        try:
            await asyncio.wait_for(self.live.send_standby_alert(alert.event_id), timeout=5)
        except Exception as error:
            if self.inactivity.alert is alert:
                self.cancel_standby_alert(
                    f"Heads-up send failed ({type(error).__name__}); staying active.", cancel_send=False,
                )

    def cancel_standby_alert(self, reason: str = "", *, cancel_send: bool = True) -> None:
        self.inactivity.cancel_alert(reason, cancel_send=cancel_send)

    @work
    async def connect_to_cora(self) -> None:
        self.set_connection_state("connecting")

        try:
            if not self.startup_complete:
                self.startup_worker = self.check_interrupted_conversations()
                await self.startup_worker.wait()
                self.startup_complete = True
                self.summary_worker = self.summarize_previous_conversations()
                await self.wait_for_services()
                # Signal starts on its own time; it never holds up Live.
                self.signal_startup = self.start_signal_if_enabled()
                self.check_email_account()
                self.restart_webhooks()
                self.daily_backup()
                await self.apply_standby_settings()
                if await self.starts_in_standby():
                    self.start_in_standby()
                    return
            else:
                # Shutdown waits on startup_worker, so reuse it for later context reloads.
                self.startup_worker = self.load_session_context()
                await self.startup_worker.wait()
            if self.closing:
                return
            # This connect sends Cora's tools as they are now.
            self.service_tools_changed = False
            await self.live.run(
                self.handle_live_event,
                self.handle_mic_status,
                self.handle_audio_status,
            )
        except Exception as error:
            self.set_connection_state("disconnected", "Connection failed. See Activity for details.")
            self.activity.write(f"Connection error: {error}", "Connection failed")
        else:
            self.set_connection_state("disconnected")
            self.activity.write("Connection closed", "Disconnected")
        finally:
            self.report_interrupted_work()
            self.conversation.flush_transcript()
            if self.wake.reconnecting:
                self.wake.reconnecting = False
                self.wake.failed = True
                if self.wake.connection_timer is not None:
                    self.wake.connection_timer.stop()
                await self.stop_wake_listener()
                self.activity.write("Wake connection ended; buffered audio discarded.", "Wake cancelled")

    async def apply_standby_settings(self) -> None:
        """Settings > General's standby settings, once at launch; one never set there keeps .env's value."""
        try:
            saved = await self.conversation_persistence.call_database(self.persistence.load_standby_settings)
        except Exception as error:
            self.activity.write(f"Standby settings unavailable ({type(error).__name__}); using .env.", "Settings")
            return
        if saved["idle_minutes"] is not None:
            self.inactivity.timeout_seconds = saved["idle_minutes"] * 60.0
        if saved["farewell"] is not None:
            self.inactivity.farewell_enabled = saved["farewell"]
        if saved["wake_word"] is not None and saved["wake_word"] != self.wake.enabled:
            self.wake.enabled = saved["wake_word"]
            self.update_wake_control()

    @work(group="model-settings-save")
    async def save_wake_word(self, on: bool) -> None:
        try:
            await self.conversation_persistence.call_database(self.persistence.save_wake_word, on)
        except Exception as error:
            self.activity.write(f"Wake setting save failed ({type(error).__name__}).", "Settings error")

    async def starts_in_standby(self) -> bool:
        """Settings > General: whether she starts in standby at launch (active if it can't be read)."""
        try:
            return await self.conversation_persistence.call_database(self.persistence.load_start_mode) == "standby"
        except Exception as error:
            self.activity.write(f"Start setting unavailable ({type(error).__name__}); starting active.", "Settings")
            return False

    def start_in_standby(self) -> None:
        """Launch without connecting to Live: she waits, as after standby, for the wake word, a
        message or the status button. Waking connects with the usual context reload."""
        self.standby = True
        self.handle_mic_status(False)
        self.set_connection_state("standby")
        self.activity.write("Started in standby (Settings > General); not connected to Live.", "Standby")
        self.start_wake_listener()

    def action_reconnect(self) -> None:
        self.record_idle_activity(user_activity=True)
        self.request_live_transition(restart=True)

    def action_standby(self) -> None:
        if self.live.ready and not self.standby:
            self.request_live_transition(restart=False)

    def request_standby(self) -> None:
        """Cora asked for standby: enter it once her sign-off and other work finish."""
        self.requested_standby_deadline = monotonic() + REQUESTED_STANDBY_TIMEOUT_SECONDS
        if self.requested_standby_timer is None:
            self.requested_standby_timer = self.set_interval(0.5, self.check_requested_standby)

    def stop_requested_standby(self) -> None:
        if self.requested_standby_timer is not None:
            self.requested_standby_timer.stop()
            self.requested_standby_timer = None

    def check_requested_standby(self) -> None:
        if self.closing or self.reconnecting or self.standby or not self.live.ready:
            self.stop_requested_standby()
        elif monotonic() >= self.requested_standby_deadline:
            self.stop_requested_standby()
            self.activity.write("Requested standby timed out; staying active.", "Standby cancelled")
        elif not self.live_transition_blockers():
            self.stop_requested_standby()
            self.action_standby()

    def live_transition_blockers(self) -> list[str]:
        """Share exactly the same safety checks for manual and automatic standby."""
        connecting = (
            self.connection_worker is not None and not self.connection_worker.is_finished
            and not self.live.ready
        )
        keyboard_busy = self.keyboard_worker is not None and not self.keyboard_worker.is_finished
        observation_busy = self.attention.busy
        tools_busy = any(
            worker.group == "tool-actions" and not worker.is_finished for worker in self.workers
        )
        # The desk speaker, or a device listening on the web page.
        playback_busy = self.live.playback_busy()
        return [name for name, busy in (
            ("connection setup", connecting),
            ("typed submission", keyboard_busy),
            ("observation send", observation_busy),
            ("tool execution", tools_busy),
            ("backend response", bool(self.backend_work.pending)),
            ("typed acknowledgement", bool(self.live.pending_text_requests)),
            ("audio playback", playback_busy),
            ("recent speech", self.live.ready and monotonic() - self.last_live_activity < 2.0),
            ("standby heads-up", self.inactivity.alert is not None),
        ) if busy]

    def request_live_transition(self, restart: bool, *, from_wake: bool = False) -> bool:
        if self.closing or self.reconnecting:
            return False
        blockers = self.live_transition_blockers()
        self.live.trace(
            "transition.request", restart=restart, blockers=blockers,
            playback_busy="audio playback" in blockers,
        )
        if blockers:
            action = "reconnect" if restart else "enter standby"
            reason = ", ".join(blockers)
            self.activity.write(f"Cannot {action}: {reason}.", "Live busy")
            self.notify(
                f"Wait for speech and pending work to finish, then {action}. Waiting for: {reason}.",
                severity="warning",
            )
            return False
        self.reconnecting = True
        self.wake.reconnecting = from_wake
        if not from_wake:
            self.wake.failed = False
            self.wake.response_deadline = None
        self.query_one("#chat-input", ChatComposer).disabled = True
        # Privacy mute remains available while opening speech is being buffered.
        self.query_one("#speaker-status", Button).disabled = True
        self.set_connection_state("connecting" if restart and self.standby else "disconnecting")
        self.reconnect_worker = self.reconnect_live(restart=restart)
        # The worker may finish before this returns, so report the request, not self.reconnecting.
        return True

    @work(group="live-reconnect")
    async def reconnect_live(self, restart: bool = True) -> None:
        try:
            if not self.wake.reconnecting:
                await self.stop_wake_listener()
            self.set_connection_state("connecting" if restart and self.standby else "disconnecting")
            await self.live.stop_microphone()
            # Closing/cleanup must finish before another reader can own the transport.
            await self.live.close()
            if self.connection_worker is not None:
                await self.connection_worker.wait()
            if self.closing:
                return
            self.conversation.flush_transcript()
            await self.conversation_persistence.wait_for_saves()
            if self.closing:
                return
            self.live.conversation_history = self.conversation.get_reconnect_history()
            self.last_live_activity = 0.0
            self.stop_conversation_end_timer()
            if not restart:
                if self.live.connection is not None or not self.live.session_close_confirmed:
                    raise RuntimeError("Live close was not confirmed")
                self.standby = True
                self.handle_mic_status(False)
                self.set_connection_state("standby")
                self.activity.write("Live closed; local app and chat remain open", "Standby")
                self.send_unsaid_messages()
                self.conversation_end_timer = self.set_timer(
                    CONVERSATION_END_STANDBY_SECONDS, self.end_standby_conversation,
                )
                return
            self.standby = False
            self.activity.write("Restarting Live with recent chat context", "Reconnecting")
            self.connection_worker = self.connect_to_cora()
        except Exception as error:
            if self.wake.reconnecting:
                self.wake.reconnecting = False
                self.wake.failed = True
                await self.stop_wake_listener()
            summary = "Reconnect failed" if restart else "Standby failed"
            self.activity.write(f"{summary} ({type(error).__name__})", summary)
            if self.live.connection is None:
                self.standby = False
                self.set_connection_state("disconnected", f"{summary}. See Activity for details.")
            self.notify(f"{summary}. Check Activity; no requests were replayed.", severity="error")
        finally:
            self.reconnecting = False
            if self.standby and not self.closing:
                self.set_connection_state("standby")
                self.start_wake_listener()
            self.query_one("#chat-input", ChatComposer).disabled = self.closing
            self.query_one("#mic-status", Button).disabled = self.closing
            self.query_one("#speaker-status", Button).disabled = self.closing
            if not self.closing:
                self.query_one("#chat-input", ChatComposer).focus()

    def stop_conversation_end_timer(self) -> None:
        if self.conversation_end_timer is not None:
            self.conversation_end_timer.stop()
            self.conversation_end_timer = None

    @work(group="conversation-end")
    async def end_standby_conversation(self) -> None:
        """A long standby ends the conversation, so its memories and summary form now."""
        self.conversation_end_timer = None
        if not self.standby or self.closing or self.reconnecting:
            return
        if await self.conversation_persistence.end_conversation() and not self.closing:
            # Photos belong to the conversation they came in.
            self.kept_images.clear()
            self.web_pictures.clear()
            # And so does where the person was.
            self.whereabouts.forget()
            self.process_finished_conversations()

    @work(group="database-startup")
    async def load_session_context(self) -> None:
        await self.conversation_persistence.load_session_context()

    def handle_live_event(self, event) -> None:
        if event.type in (
            "session.input_transcript.delta", "session.output_transcript.delta",
        ) and event.delta:
            self.last_live_activity = monotonic()
            user_activity = event.type == "session.input_transcript.delta"
            if user_activity:
                user_activity = self.wake.has_request(event.delta)
            self.record_idle_activity(user_activity=user_activity)
            self.inactivity.record_output()
        elif event.type == "session.output_audio.delta" and self.live.playback_busy():
            # Output audio is a continuous stream: silent chunks are not speech.
            self.last_live_activity = monotonic()
            self.record_idle_activity()
            self.inactivity.record_output()
        if event.type == "session.input_transcript.delta":
            self.person_is_talking()

        self.live_events.handle(event)

    def handle_live_session_event(self, event) -> None:
        if event.type == "session.started":
            self.wake.session_started()
            self.record_idle_activity()
            self.inactivity.was_busy = False
            self.set_connection_state("active")
            self.activity.write("Live session started", "Connected")
            kind = self.pending_greeting
            # An unknown last talk must not become a stranger introduction.
            if kind == "launch" and self.conversation_persistence.last_talked_loaded:
                self.send_greeting(kind)  # clears pending_greeting once it is sent
            else:
                self.pending_greeting = None
                if kind in ("welcome_back", "phone"):
                    self.send_greeting(kind)

        elif event.type == "session.commentary.appended":
            self.inactivity.acknowledge(event.client_event_id)

        elif event.type == "error":
            # A rejected command does not necessarily end the Live session.
            self.activity.write(f"API error: {event.error.message}", "API error")
            command_id = getattr(event.error, "client_event_id", None)
            if self.inactivity.alert is not None and command_id == self.inactivity.alert.event_id:
                self.cancel_standby_alert("Heads-up rejected; staying active.")
            if command_id:
                if command_id.startswith("typed_response_") and not self.live.pending_text_requests:
                    self.backend_work.finish_typed_request()
                    self.record_idle_activity()
                self.activity.write(f"Rejected command: id={command_id}", "API error")

        elif event.type == "session.closed":
            self.report_interrupted_work()
            self.set_connection_state("disconnected")
            self.conversation.flush_transcript()
            self.activity.write(f"Session ended: {event.reason}", "Disconnected")

    def set_connection_state(self, state: str, detail: str = "") -> None:
        labels = {
            "disconnected": "○ Disconnected",
            "standby": "○ Standby",
            "connecting": "◌ Connecting…",
            "active": "● Active",
            "disconnecting": "◌ Disconnecting…",
        }
        button = self.query_one("#chat-status", Button)
        button.label = labels[state]
        for name in labels:
            button.set_class(name == state, f"state-{name}")
        button.tooltip = detail or {
            "active": "Click to close Live and enter standby.",
            "standby": "Live is closed. Click to resume, or press Ctrl+R.",
            "disconnected": "Live is disconnected. Ctrl+R to reconnect.",
        }.get(state, f"Live is {state}.")
        button.disabled = state not in ("active", "standby") or self.closing or self.reconnecting
        # The web page shows the desk's state too (set up later in on_mount than the first call).
        feed = getattr(self, "web_feed", None)
        if feed is not None:
            feed.set_status(state)
        # Connected while a page talks (Talk woke her): she's told where the person is now.
        if state == "active" and getattr(self, "web_voice", None) is not None:
            self.web_voice_connected()
        # A page talking to her can't once she's in standby or disconnected: its call ends.
        if state in ("standby", "disconnected") and getattr(self, "web_voice", None) is not None:
            self.web_talk_lost_live()
