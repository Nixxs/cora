from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock

from ui.audio_status import AudioStatus


class AudioStatusTests(unittest.TestCase):
    def setUp(self):
        self.mic = MagicMock()
        self.speaker = MagicMock()
        self.indicator = MagicMock(is_mounted=True, content="────────")
        self.live = SimpleNamespace(microphone_muted=False, speaker_muted=False, speaker=None)
        self.set_timer = MagicMock()
        self.set_interval = MagicMock()
        self.is_closing = MagicMock(return_value=False)
        self.on_controls_updated = MagicMock()
        self.status = AudioStatus(
            self.mic, self.speaker, self.indicator, self.live,
            self.set_timer, self.set_interval, self.is_closing, self.on_controls_updated,
        )

    def test_controller_owns_animation_and_controls_refresh(self):
        self.set_interval.assert_called_once_with(0.05, self.status.update_speaking_animation)
        self.status.handle_mic_status(True)
        self.assertEqual(self.mic.label, "🎙 Listening")
        self.mic.refresh.assert_called_once_with(layout=True)
        self.speaker.refresh.assert_called_once_with(layout=True)
        self.on_controls_updated.assert_called_once_with()
        self.indicator.update.assert_not_called()

    def test_transcript_hint_replaces_timer_and_resets_to_listening(self):
        self.status.handle_mic_status(True)
        self.status.speech_received()
        self.assertEqual(self.mic.label, "🎙 Speech received")
        self.set_timer.assert_called_once_with(1.5, self.status.reset_mic_indicator)
        previous_timer = self.status.mic_idle_timer
        self.status.speech_received()
        previous_timer.stop.assert_called_once_with()
        self.status.reset_mic_indicator()
        self.assertIsNone(self.status.mic_idle_timer)
        self.assertEqual(self.mic.label, "🎙 Listening")

    def test_mute_cancels_hint_and_ignores_late_transcript(self):
        self.status.speech_received()
        timer = self.status.mic_idle_timer
        self.live.microphone_muted = True
        self.status.handle_mic_status(False)
        timer.stop.assert_called_once_with()
        self.assertIsNone(self.status.mic_idle_timer)
        self.status.speech_received()
        self.assertEqual(self.set_timer.call_count, 1)
        self.assertEqual(self.mic.label, "🎙 Muted")

    def test_stop_cleans_up_both_display_timers(self):
        self.status.speech_received()
        timer = self.status.mic_idle_timer
        self.status.stop()
        self.status.speaker_level_timer.stop.assert_called_once_with()
        timer.stop.assert_called_once_with()
        self.assertIsNone(self.status.mic_idle_timer)


if __name__ == "__main__":
    unittest.main()