import base64
from struct import pack
from threading import Thread
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from textual.widgets import Button, Static

from devices.speaker import Speaker
from test_keyboard_persistence import running_app


class PlaybackLevelTests(unittest.TestCase):
    def test_level_follows_played_samples_not_queued_audio_and_includes_silent_padding(self):
        speaker = Speaker()
        with patch("devices.speaker.sd.RawOutputStream") as stream, \
                patch("devices.speaker.monotonic", return_value=10.0):
            stream.return_value.active = True
            speaker.play(base64.b64encode(pack("<hh", 16384, -16384)).decode("ascii"))
            self.assertEqual(speaker.get_playback_level(), 0.0)
            output = bytearray(8)
            speaker.callback(output, 4, None, None)
            self.assertEqual(output, pack("<hhhh", 16384, -16384, 0, 0))
            self.assertAlmostEqual(speaker.get_playback_level(), (0.5 ** 2 / 2) ** 0.5)
            speaker.callback(output, 4, None, None)
            self.assertEqual(speaker.get_playback_level(), 0.0)
            speaker.close()

    def test_full_scale_negative_sample_and_stale_or_stopped_callbacks(self):
        speaker = Speaker()
        speaker.stream = MagicMock(active=True)
        speaker.chunks.put(pack("<h", -32768))
        with patch("devices.speaker.monotonic", return_value=10.0):
            speaker.callback(bytearray(2), 1, None, None)
            self.assertEqual(speaker.get_playback_level(), 1.0)
        with patch("devices.speaker.monotonic", return_value=10.2):
            self.assertEqual(speaker.get_playback_level(), 0.0)
        speaker.stream.active = False
        with patch("devices.speaker.monotonic", return_value=10.0):
            self.assertEqual(speaker.get_playback_level(), 0.0)
        speaker.close()
        self.assertEqual(speaker.get_playback_level(), 0.0)

    def test_audio_thread_only_publishes_level_and_close_resets_snapshot(self):
        speaker = Speaker()
        speaker.stream = MagicMock(active=True)
        speaker.chunks.put(pack("<h", 8192) * 480)
        output = bytearray(960)
        thread = Thread(target=speaker.callback, args=(output, 480, None, None))
        thread.start()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertAlmostEqual(speaker.get_playback_level(), 0.25)
        speaker.close()
        self.assertEqual(speaker._playback_level, (0.0, 0.0))


class SpeakingAnimationTests(unittest.IsolatedAsyncioTestCase):
    async def test_timer_tolerates_indicator_removal_during_teardown(self):
        async with running_app() as (app, pilot):
            await app.query_one("#speaker-level", Static).remove()
            app.update_speaking_animation()
            await pilot.pause(0.1)

    async def test_idle_indicator_does_not_repaint_or_trigger_layout(self):
        async with running_app() as (app, pilot):
            indicator = app.query_one("#speaker-level", Static)
            with patch.object(indicator, "update", wraps=indicator.update) as update:
                app.update_speaking_animation()
                app.update_speaking_animation()
                update.assert_not_called()
                app.live.speaker = SimpleNamespace(get_playback_level=lambda: 1.0)
                app.update_speaking_animation()
                update.assert_called_once_with("───────█", layout=False)

    async def test_timer_renders_levels_and_clears_on_silence_disconnect_and_mute(self):
        async with running_app() as (app, pilot):
            indicator = app.query_one("#speaker-level", Static)
            level = MagicMock(return_value=0.25)
            app.live.speaker = SimpleNamespace(get_playback_level=level, close=MagicMock())
            await pilot.pause(0.1)
            rendered = indicator.render_line(0).text
            self.assertEqual(len(rendered), 8)
            self.assertIn("▅", rendered)
            level.return_value = 0.0
            await pilot.pause(0.1)
            self.assertEqual(indicator.render_line(0).text, "────────")
            level.return_value = 0.25
            await pilot.pause(0.1)
            await pilot.click("#speaker-status")
            self.assertEqual(indicator.render_line(0).text.strip(), "Muted")
            self.assertTrue(indicator.has_class("audio-muted"))
            self.assertEqual(list(app.speaker_levels), list("────────"))
            await pilot.pause(0.25)
            await pilot.click("#speaker-status")
            self.assertFalse(indicator.has_class("audio-muted"))
            level.return_value = 0.0
            await pilot.pause(0.1)
            self.assertEqual(indicator.render_line(0).text, "────────")
            app.live.speaker = None
            await pilot.pause(0.1)
            self.assertEqual(indicator.render_line(0).text, "────────")

    async def test_silence_and_mute_keep_fixed_width_and_existing_labels_visible(self):
        async with running_app() as (app, pilot):
            await pilot.resize_terminal(55, 25)
            app.handle_mic_status(True)
            await pilot.pause()
            indicator = app.query_one("#speaker-level", Static)
            mic = app.query_one("#mic-status", Button)
            speaker = app.query_one("#speaker-status", Button)
            original = indicator.region
            self.assertEqual(original.width, 8)
            self.assertEqual(original.height, 1)
            self.assertEqual(original.y, speaker.region.y)
            self.assertLessEqual(original.right, app.size.width)
            self.assertIn("Listening", mic.render_line(0).text)
            self.assertIn("Speaker", speaker.render_line(0).text)
            await pilot.click("#speaker-status")
            self.assertEqual(indicator.region, original)
            self.assertEqual(indicator.render_line(0).text.strip(), "Muted")

    async def test_shutdown_stops_timer_and_flattens_bars_without_changing_chat(self):
        async with running_app() as (app, pilot):
            app.live.speaker = SimpleNamespace(get_playback_level=lambda: 1.0)
            await pilot.pause(0.1)
            indicator = app.query_one("#speaker-level", Static)
            self.assertIn("█", indicator.render_line(0).text)
            before = list(app.conversation.log_entries)
            with patch.object(app.speaker_level_timer, "stop", wraps=app.speaker_level_timer.stop) as stop, \
                    patch.object(app, "exit"):
                await app.action_disconnect_and_quit()
                stop.assert_called_once_with()
                await pilot.pause()
            self.assertEqual(indicator.render_line(0).text, "────────")
            self.assertEqual(app.conversation.log_entries, before)


if __name__ == "__main__":
    unittest.main()