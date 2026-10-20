"""The title bar's view button switches between the chat and the dashboard. Mocked APIs only."""

import asyncio
from time import monotonic
import unittest
from unittest.mock import patch

import numpy as np
from textual.widgets import Button

from devices.audio_bands import BAND_COUNT, audio_bands
from live import CoraLive
from session_config import BACKEND_MODEL, MODEL_AREAS
from ui.braille import BrailleGraph
from ui.dashboard import BOX_TITLES, FALL_PER_TICK, DashboardScreen
from ui.model_settings import ModelSettingsScreen
import test_keyboard_persistence as keyboard_tests


class AudioBandTests(unittest.TestCase):
    def test_silence_and_short_chunks_are_empty_bars(self):
        self.assertEqual(audio_bands(bytes(960)), (0.0,) * BAND_COUNT)
        self.assertEqual(audio_bands(b""), (0.0,) * BAND_COUNT)

    def test_a_full_scale_tone_fills_only_its_own_band(self):
        # 1 kHz completes exactly 20 cycles in a 480-sample chunk at 24 kHz.
        tone = (np.sin(2 * np.pi * 1000 * np.arange(480) / 24000) * 32767).astype("<i2").tobytes()
        bands = audio_bands(tone)
        self.assertEqual(len(bands), BAND_COUNT)
        self.assertAlmostEqual(max(bands), 1.0, places=3)
        self.assertEqual(bands[0], 0.0)
        self.assertEqual(bands[-1], 0.0)

    def test_live_bands_are_empty_when_stale_or_muted(self):
        live = CoraLive()
        self.assertEqual(live.get_microphone_bands(), ())
        live._microphone_bands = ((0.5,), monotonic())
        self.assertEqual(live.get_microphone_bands(), (0.5,))
        live.microphone_muted = True
        self.assertEqual(live.get_microphone_bands(), ())
        live.microphone_muted = False
        live._microphone_bands = ((0.5,), monotonic() - 1)
        self.assertEqual(live.get_microphone_bands(), ())


class DashboardScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_view_button_switches_between_chat_and_dashboard(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.assertEqual(str(app.screen.query_one("#view-button", Button).label), "dashboard")

            await pilot.click("#view-button")
            await pilot.pause()
            self.assertIsInstance(app.screen, DashboardScreen)
            self.assertEqual(str(app.screen.query_one("#view-button", Button).label), "chat")

            await pilot.click("#view-button")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, DashboardScreen)
            self.assertEqual(str(app.screen.query_one("#view-button", Button).label), "dashboard")

    async def test_dashboard_lays_out_every_titled_box(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await pilot.click("#view-button")
            await pilot.pause()
            for box_id, title in BOX_TITLES.items():
                box = app.screen.query_one(f"#{box_id}")
                self.assertEqual(box.border_title, title)
                self.assertGreater(box.size.width, 0)
                self.assertGreater(box.size.height, 0)

    async def test_voice_bars_jump_up_then_fall_back(self):
        async with keyboard_tests.running_app() as (app, pilot):
            loud = ((1.0,) * BAND_COUNT, (0.5,) * BAND_COUNT)
            with patch.object(app, "voice_bands", return_value=loud):
                await pilot.click("#view-button")
                await pilot.pause(0.2)
                screen = app.screen
                speaker = screen.query_one("#speaker-graph", BrailleGraph)
                mic = screen.query_one("#mic-graph", BrailleGraph)
                self.assertEqual(speaker.values, (1.0,) * BAND_COUNT)
                self.assertEqual(mic.values, (0.5,) * BAND_COUNT)
                self.assertTrue(mic.downward)

                screen.voice_bands = lambda: ((), ())
                screen.sample_voice()
                self.assertEqual(speaker.values, (1.0 - FALL_PER_TICK,) * BAND_COUNT)
                self.assertEqual(mic.values, (0.5 - FALL_PER_TICK,) * BAND_COUNT)

    async def test_settings_open_from_the_dashboard(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.persistence.load_models.return_value = {area: BACKEND_MODEL for area in MODEL_AREAS}
            await pilot.click("#view-button")
            await pilot.pause()
            await pilot.click("#settings-button")
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            await pilot.pause()
            self.assertIsInstance(app.screen, ModelSettingsScreen)

            await pilot.press("escape")
            await pilot.pause()
            self.assertIsInstance(app.screen, DashboardScreen)


if __name__ == "__main__":
    unittest.main()
