"""Microphone/speaker status display and playback-level animation."""

from collections import deque

from textual.widgets import Button, Static


class AudioStatus:
    def __init__(
        self, mic: Button, speaker: Button, indicator: Static, live,
        set_timer, set_interval, is_closing, on_controls_updated,
    ):
        self.mic = mic
        self.speaker = speaker
        self.indicator = indicator
        self.live = live
        self.set_timer = set_timer
        self.is_closing = is_closing
        self.on_controls_updated = on_controls_updated
        self.mic_working = False
        self.mic_idle_timer = None
        self.speaker_levels = deque("────────", maxlen=8)
        self.speaker_level_timer = set_interval(0.05, self.update_speaking_animation)

    def update_controls(self) -> None:
        self.mic.label = (
            "🎙 Muted" if self.live.microphone_muted
            else "🎙 Listening" if self.mic_working else "🎙× Mic"
        )
        self.mic.tooltip = "Unmute microphone" if self.live.microphone_muted else "Mute microphone"
        self.mic.set_class(self.live.microphone_muted, "audio-muted")
        # Button.label repaints but does not invalidate its cached auto width.
        self.mic.refresh(layout=True)
        self.speaker.label = "🔇 Muted" if self.live.speaker_muted else "🔊 Speaker"
        self.speaker.tooltip = "Unmute speaker" if self.live.speaker_muted else "Mute speaker"
        self.speaker.set_class(self.live.speaker_muted, "audio-muted")
        self.speaker.refresh(layout=True)
        self.on_controls_updated()
        self.update_speaking_animation()

    def update_speaking_animation(self) -> None:
        if not self.indicator.is_mounted:
            return
        muted = self.live.speaker_muted
        level = (
            self.live.speaker.get_playback_level()
            if not self.is_closing() and not muted and self.live.speaker is not None else 0.0
        )
        if level > 0:
            # A gentle gain makes ordinary speech visible without changing playback.
            self.speaker_levels.append("▁▂▃▄▅▆▇█"[min(7, int(level ** 0.5 * 8))])
        else:
            self.speaker_levels.clear()
            self.speaker_levels.extend("────────")
        text = "Muted" if muted else "".join(self.speaker_levels)
        if self.indicator.content != text:
            # This fixed-width display never needs to relayout the conversation.
            self.indicator.update(text, layout=False)
        self.indicator.set_class(muted, "audio-muted")

    def speech_received(self) -> None:
        if self.live.microphone_muted:
            return
        self.mic.label = "🎙 Speech received"
        self.mic.refresh(layout=True)
        if self.mic_idle_timer is not None:
            self.mic_idle_timer.stop()
        # Transcript silence is a UI hint, not a confirmed end of speech.
        self.mic_idle_timer = self.set_timer(1.5, self.reset_mic_indicator)

    def reset_mic_indicator(self) -> None:
        self.mic_idle_timer = None
        self.update_controls()

    def handle_mic_status(self, working: bool) -> None:
        self.mic_working = working
        if self.mic_idle_timer is not None:
            self.mic_idle_timer.stop()
            self.mic_idle_timer = None
        self.update_controls()

    def stop(self) -> None:
        self.speaker_level_timer.stop()
        if self.mic_idle_timer is not None:
            self.mic_idle_timer.stop()
            self.mic_idle_timer = None