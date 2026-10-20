"""The title bar's view button switches between the chat and the dashboard. Mocked APIs only."""

from array import array
import asyncio
from time import monotonic
import unittest
from unittest.mock import patch

from textual.widgets import Button

from devices.microphone import chunk_level
from live import CoraLive
from session_config import BACKEND_MODEL, MODEL_AREAS
from ui.braille import BrailleGraph
from ui.dashboard import BOX_TITLES, DashboardScreen
from ui.model_settings import ModelSettingsScreen
import test_keyboard_persistence as keyboard_tests


class MicrophoneLevelTests(unittest.TestCase):
    def test_chunk_level_is_normalized_rms(self):
        self.assertEqual(chunk_level(bytes(960)), 0.0)
        self.assertEqual(chunk_level(b""), 0.0)
        self.assertAlmostEqual(chunk_level(array("h", [32767, -32767] * 240).tobytes()), 1.0, places=3)

    def test_live_level_is_zero_when_stale_or_muted(self):
        live = CoraLive()
        self.assertEqual(live.get_microphone_level(), 0.0)
        live._microphone_level = (0.5, monotonic())
        self.assertEqual(live.get_microphone_level(), 0.5)
        live.microphone_muted = True
        self.assertEqual(live.get_microphone_level(), 0.0)
        live.microphone_muted = False
        live._microphone_level = (0.5, monotonic() - 1)
        self.assertEqual(live.get_microphone_level(), 0.0)


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

    async def test_voice_band_graphs_both_levels(self):
        async with keyboard_tests.running_app() as (app, pilot):
            with patch.object(app, "voice_levels", return_value=(1.0, 0.25)):
                await pilot.click("#view-button")
                await pilot.pause(0.2)
                self.assertEqual(app.screen.query_one("#speaker-graph", BrailleGraph).values[-1], 1.0)
                mic = app.screen.query_one("#mic-graph", BrailleGraph)
                self.assertEqual(mic.values[-1], 0.5)
                self.assertTrue(mic.downward)

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
