"""Idle timing and optional standby heads-up coordination."""

from dataclasses import dataclass
from math import isfinite
from os import getenv
from uuid import uuid4


def read_idle_timeout() -> float:
    """Seconds of inactivity before standby; zero disables the timer."""
    value = float(getenv("CORA_IDLE_TIMEOUT_SECONDS", "300"))
    if not isfinite(value) or value < 0:
        raise ValueError("Idle timeout must be finite and nonnegative")
    return value


@dataclass
class StandbyAlert:
    event_id: str
    started_at: float
    accepted: bool = False
    last_output_at: float | None = None
    quiet_since: float | None = None


class InactivityController:
    def __init__(
        self, *, timeout_seconds: float, farewell_enabled: bool,
        clock, can_standby, speaker_muted, transition_blockers,
        send_alert, enter_standby, write_activity,
    ):
        self.timeout_seconds = timeout_seconds
        self.farewell_enabled = farewell_enabled
        self.clock = clock
        self.can_standby = can_standby
        self.speaker_muted = speaker_muted
        self.transition_blockers = transition_blockers
        self.send_alert = send_alert
        self.enter_standby = enter_standby
        self.write_activity = write_activity
        self.last_activity = clock()
        self.was_busy = False
        self.alert = None
        self.alert_worker = None

    def record_activity(self) -> None:
        self.last_activity = self.clock()

    def check(self) -> None:
        if self.alert is not None:
            self.check_alert()
            return
        if self.timeout_seconds <= 0 or not self.can_standby():
            return
        if self.transition_blockers():
            # No time spent doing work or playing speech counts toward idleness.
            self.record_activity()
            self.was_busy = True
            return
        if self.was_busy:
            self.record_activity()
            self.was_busy = False
            return
        if self.clock() - self.last_activity >= self.timeout_seconds:
            if self.farewell_enabled and not self.speaker_muted():
                alert = StandbyAlert(f"standby_alert_{uuid4().hex}", self.clock())
                self.alert = alert
                self.write_activity("Standby heads-up; interact to stay active.", "Standby heads-up")
                self.alert_worker = self.send_alert(alert)
                return
            self.write_activity("Idle timeout reached; entering standby.", "Automatic standby")
            self.enter_standby()

    def cancel_alert(self, reason: str = "", *, cancel_send: bool = True) -> None:
        if self.alert is None:
            return
        self.alert = None
        if cancel_send and self.alert_worker is not None and not self.alert_worker.is_finished:
            self.alert_worker.cancel()
        self.record_activity()
        self.was_busy = False
        if reason:
            self.write_activity(reason, "Standby cancelled")

    def record_output(self) -> None:
        if self.alert is not None:
            self.alert.last_output_at = self.clock()
            self.alert.quiet_since = None

    def acknowledge(self, event_id: str | None) -> None:
        if self.alert is not None and event_id == self.alert.event_id:
            self.alert.accepted = True

    def check_alert(self) -> None:
        alert = self.alert
        if alert is None:
            return
        if not self.can_standby() or self.timeout_seconds <= 0:
            self.cancel_alert()
            return
        blockers = self.transition_blockers()
        if any(name not in ("standby heads-up", "audio playback", "recent speech") for name in blockers):
            self.cancel_alert("New work; staying active.")
            return
        now = self.clock()
        if now - alert.started_at >= 30:
            # No authoritative speech-done event exists. Never close on timeout.
            self.cancel_alert("Heads-up wait timed out; staying active.")
            return
        if not alert.accepted or alert.last_output_at is None:
            return
        if "audio playback" in blockers or now - alert.last_output_at < 2:
            alert.quiet_since = None
            return
        if alert.quiet_since is None:
            alert.quiet_since = now
            return
        if now - alert.quiet_since >= 3:
            self.alert = None
            self.write_activity("Heads-up finished; entering standby.", "Automatic standby")
            self.enter_standby()