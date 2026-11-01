"""Wake word listening, microphone and speaker controls, and the speaking animation: part of CoraApp (see app.py)."""

from textual.worker import WorkerError
from textual import work

from wake.listener import WakeListener


class VoiceMixin:
    """Wake word listening, microphone and speaker controls, and the speaking animation."""

    def update_wake_control(self) -> None:
        self.wake.update_control()

    def start_wake_listener(self) -> None:
        self.wake.start_listener()

    async def stop_wake_listener(self) -> None:
        await self.wake.stop_listener()

    def handle_wake_status(self, working: bool, message: str) -> None:
        self.wake.handle_status(working, message)

    def handle_wake_diagnostic(self, message: str) -> None:
        self.wake.handle_diagnostic(message)

    def handle_wake(self, listener: WakeListener) -> bool:
        return self.wake.handle_wake(listener)

    def request_wake_connection(self) -> bool:
        return self.request_live_transition(restart=True, from_wake=True)

    @work(group="wake-abort")
    async def abort_wake_connection(self) -> None:
        if not self.wake.reconnecting or self.live.ready or self.closing:
            return
        self.wake.reconnecting = False
        self.wake.failed = True
        self.wake.response_deadline = None
        if self.reconnect_worker is not None and not self.reconnect_worker.is_finished:
            self.reconnect_worker.cancel()
            try:
                await self.reconnect_worker.wait()
            except WorkerError:
                pass
        await self.stop_wake_listener()
        if self.connection_worker is not None:
            self.connection_worker.cancel()
            try:
                await self.connection_worker.wait()
            except WorkerError:
                pass
        if self.live.connection is None:
            self.standby = True
            self.set_connection_state("standby")
        self.activity.write("Wake connection failed/timed out; audio discarded. Resume manually or toggle Wake to retry.", "Wake cancelled")
        # No automatic retries after a connection/capture failure.

    def check_wake_timeout(self) -> None:
        self.wake.check_timeout()

    def update_audio_controls(self) -> None:
        self.audio_status.update_controls()

    def voice_bands(self) -> tuple[tuple[float, ...], tuple[float, ...]]:
        """Cora's playback and the person's microphone loudness bands, for the dashboard."""
        speaker = self.live.speaker
        playing = speaker.get_playback_bands() if speaker is not None and not self.live.speaker_muted else ()
        return playing, self.live.get_microphone_bands()

    def update_speaking_animation(self) -> None:
        self.audio_status.update_speaking_animation()

    def reset_mic_indicator(self) -> None:
        self.audio_status.reset_mic_indicator()

    def handle_audio_status(self, working: bool, message: str) -> None:
        if not working:
            self.cancel_standby_alert("Audio unavailable; staying active.")
        self.activity.write(
            message,
            "Audio playback ready" if working else "Audio playback error",
        )

    def handle_mic_status(self, working: bool, message: str = "") -> None:
        self.audio_status.handle_mic_status(working)

        if message:
            self.activity.write(
                f"[mic] {message}",
                "Microphone active" if working else "Microphone stopped",
            )
