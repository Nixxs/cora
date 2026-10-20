"""The title bar's view button switches between the chat and the dashboard. Mocked APIs only."""

import asyncio
from datetime import datetime, timezone
from time import monotonic
import unittest
from unittest.mock import MagicMock, patch

import numpy as np
from textual.widgets import Button, Static

from devices.audio_bands import BAND_COUNT, audio_bands
from live import CoraLive
from session_config import BACKEND_MODEL, MODEL_AREAS
from storage.dashboard_snapshot import HISTORY_LIMIT, DashboardSnapshot, read_dashboard_snapshot
from subconscious.relationship.relationship_rules import RelationshipValues
from ui.braille import BrailleGraph
from ui.chat_widgets import SelectableRichLog
from ui.composer import ChatComposer
from ui.dashboard import BOX_TITLES, FALL_PER_TICK, DashboardScreen
from ui.meters import TONE_STYLES, Meters, ToneRibbon, meter_bar
from ui.model_settings import ModelSettingsScreen
import test_keyboard_persistence as keyboard_tests


class AudioBandTests(unittest.TestCase):
    def test_silence_and_short_chunks_are_empty_bars(self):
        self.assertEqual(audio_bands(bytes(960)), (0.0,) * BAND_COUNT)
        self.assertEqual(audio_bands(b""), (0.0,) * BAND_COUNT)
        self.assertEqual(audio_bands(b"\xff\x7f\xff\x7f"), (0.0,) * BAND_COUNT)

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


class DashboardSnapshotTests(unittest.TestCase):
    def test_meter_bar_fills_whole_cells_and_clamps(self):
        self.assertEqual(meter_bar(0.5, 4), "■■")
        self.assertEqual(meter_bar(0.7, 4), "■■■")
        self.assertEqual(meter_bar(2.0, 3), "■■■")
        self.assertEqual(meter_bar(-1.0, 3), "")
        self.assertEqual(meter_bar(0.5, 0), "")

    def test_snapshot_reads_relationship_values_drifted_to_now_history_tones_and_memory_kinds(self):
        now = datetime(2026, 10, 20, tzinfo=timezone.utc)
        values = RelationshipValues(0.5, 0.6, 0.7)
        events = [MagicMock(trust=index / 1000, warmth=0.5, familiarity=0.1) for index in range(250)]
        with patch("storage.dashboard_snapshot.RelationshipStore") as store, \
                patch("storage.dashboard_snapshot.MemoryStore") as memories:
            store.return_value.current_values.return_value = values
            store.return_value.history.return_value = events
            store.return_value.recent_tones.return_value = ["warm", "curt"]
            memories.return_value.kind_counts.return_value = {"fact": 3}
            memories.return_value.daily_counts.return_value = [0, 2]
            memories.return_value.pending_counts.return_value = {"embedding": 1}
            snapshot = read_dashboard_snapshot("engine", now)
        store.assert_called_once_with("engine")
        memories.assert_called_once_with("engine")
        memories.return_value.daily_counts.assert_called_once_with(now, HISTORY_LIMIT)
        self.assertEqual(snapshot.memory_days, (0, 2))
        self.assertEqual(snapshot.memory_pending, {"embedding": 1})
        store.return_value.current_values.assert_called_once_with(now=now)
        store.return_value.recent_tones.assert_called_once_with(HISTORY_LIMIT)
        self.assertEqual(snapshot.relationship, values)
        self.assertEqual(snapshot.tones, ("warm", "curt"))
        self.assertEqual(snapshot.memory_kinds, {"fact": 3})
        # Only the latest events, oldest first.
        self.assertEqual(len(snapshot.history), HISTORY_LIMIT)
        self.assertEqual(snapshot.history[0], RelationshipValues(0.05, 0.5, 0.1))
        self.assertEqual(snapshot.history[-1], RelationshipValues(0.249, 0.5, 0.1))


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

    async def test_chat_box_mirrors_the_conversation_while_open(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.conversation.write_log("You: before")
            await pilot.click("#view-button")
            await pilot.pause()
            chat = app.screen.query_one("#dashboard-chat", SelectableRichLog)
            app.conversation.write_log("Cora: during")
            app.conversation.append_transcript("You", "still talking")
            await pilot.pause()
            await pilot.pause()
            text = "\n".join(line.text for line in chat.lines)
            for expected in ("You: before", "Cora: during", "You: still talking"):
                self.assertIn(expected, text)

            await pilot.click("#view-button")
            await pilot.pause()
            self.assertEqual(app.conversation.mirrors, [])

    async def test_typing_in_the_dashboard_sends_to_cora(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await pilot.click("#view-button")
            await pilot.pause()
            composer = app.screen.query_one("#dashboard-input", ChatComposer)
            self.assertIs(app.focused, composer)
            composer.load_text("Hi Cora")
            await pilot.press("enter")
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            await pilot.pause()
            await pilot.pause()
            app.live.send_text.assert_awaited_once_with("Hi Cora")
            self.assertEqual(composer.text, "")
            chat = app.screen.query_one("#dashboard-chat", SelectableRichLog)
            self.assertIn("You: Hi Cora", "\n".join(line.text for line in chat.lines))

    async def test_voice_box_toggles_mute_like_the_chat_buttons(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await pilot.click("#view-button")
            await pilot.pause()
            speaker = app.screen.query_one("#dashboard-speaker", Button)
            mic = app.screen.query_one("#dashboard-mic", Button)
            self.assertEqual(speaker.region.y, mic.region.y)
            await pilot.click("#dashboard-speaker")
            await pilot.click("#dashboard-mic")
            await pilot.pause(0.3)
            self.assertTrue(app.live.speaker_muted)
            self.assertTrue(app.live.microphone_muted)
            self.assertEqual(str(speaker.label), "🔇 muted")
            self.assertEqual(str(mic.label), "🎙 muted")
            self.assertTrue(speaker.has_class("audio-muted"))
            self.assertTrue(mic.has_class("audio-muted"))
            self.assertIs(app.focused, app.screen.query_one("#dashboard-input", ChatComposer))

            await pilot.click("#view-button")
            await pilot.pause()
            self.assertEqual(str(app.query_one("#speaker-status", Button).label), "🔇 Muted")
            self.assertEqual(str(app.query_one("#mic-status", Button).label), "🎙 Muted")

    async def test_snapshot_loads_on_open_and_a_failed_refresh_keeps_it(self):
        async with keyboard_tests.running_app() as (app, pilot):
            first = DashboardSnapshot(
                relationship=RelationshipValues(0.5, 0.6, 0.7), history=(), tones=(), memory_kinds={},
                memory_days=(), memory_pending={},
            )
            app.persistence.load_dashboard_snapshot.return_value = first
            await pilot.click("#view-button")
            await pilot.pause()
            screen = app.screen
            await asyncio.wait_for(screen.workers.wait_for_complete(), 3)
            self.assertIs(screen.snapshot, first)

            app.persistence.load_dashboard_snapshot.side_effect = RuntimeError("private details")
            await screen.refresh_snapshot().wait()
            self.assertIs(screen.snapshot, first)
            entries = "\n".join(app.activity.entries)
            self.assertIn("Dashboard load failed (RuntimeError)", entries)
            self.assertNotIn("private details", entries)

    async def test_relationship_box_shows_meters_tones_or_placeholders(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await pilot.click("#view-button")
            await pilot.pause()
            screen = app.screen
            await asyncio.wait_for(screen.workers.wait_for_complete(), 3)
            meters = screen.query_one("#relationship-meters", Meters)
            ribbon = screen.query_one("#relationship-tones", ToneRibbon)
            self.assertEqual(meters.rows, ())
            self.assertEqual(str(meters.render()), "no relationship yet")
            self.assertEqual(str(ribbon.render()), "no everyday conversations yet")

            tones = ("warm", "neutral", "curt", "hostile") * 50
            app.persistence.load_dashboard_snapshot.return_value = DashboardSnapshot(
                relationship=RelationshipValues(0.5, 0.75, 0.25),
                history=(RelationshipValues(0.5, 0.5, 0.0), RelationshipValues(0.6, 0.7, 0.2)),
                tones=tones,
                memory_kinds={},
                memory_days=(),
                memory_pending={},
            )
            await screen.refresh_snapshot().wait()
            await pilot.pause()
            self.assertEqual(
                meters.rows, (("trust", 0.5), ("warmth", 0.75), ("familiarity", 0.25)),
            )
            lines = str(meters.render()).splitlines()
            self.assertEqual(len(lines), 3)
            self.assertTrue(lines[1].startswith("warmth      ■"))
            self.assertTrue(lines[1].endswith(" 75%"))
            self.assertTrue(all(len(line) == meters.size.width for line in lines))
            # Each graph ends at the current value its meter shows.
            self.assertEqual(screen.query_one("#trust-history", BrailleGraph).values, (0.5, 0.6, 0.5))
            self.assertEqual(screen.query_one("#warmth-history", BrailleGraph).values, (0.5, 0.7, 0.75))
            self.assertEqual(
                screen.query_one("#familiarity-history", BrailleGraph).values, (0.0, 0.2, 0.25),
            )
            # The newest tones that fit, newest (hostile) on the right.
            cells = ribbon.render()
            self.assertEqual(str(cells), "■" * ribbon.size.width)
            self.assertEqual(cells.spans[-1].style, TONE_STYLES["hostile"])
            self.assertEqual(cells.spans[-4].style, TONE_STYLES["warm"])

    async def test_memories_box_shows_counts_by_kind_or_a_placeholder(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await pilot.click("#view-button")
            await pilot.pause()
            screen = app.screen
            await asyncio.wait_for(screen.workers.wait_for_complete(), 3)
            kinds = screen.query_one("#memory-kinds", Meters)
            self.assertEqual(str(kinds.render()), "no memories yet")
            self.assertEqual(str(screen.query_one("#memory-pending", Static).render()), "nothing")

            app.persistence.load_dashboard_snapshot.return_value = DashboardSnapshot(
                relationship=None, history=(), tones=(),
                memory_kinds={"fact": 12000, "preference": 4000, "decision": 0, "task": 4000},
                memory_days=(50,) + (0, 3, 1) * 100,
                memory_pending={"embedding": 3, "duplicate check": 0},
            )
            await screen.refresh_snapshot().wait()
            await pilot.pause()
            self.assertEqual(kinds.rows[0], ("fact", 0.6, "12000"))
            lines = str(kinds.render()).splitlines()
            self.assertEqual(len(lines), 4)
            self.assertTrue(lines[0].startswith("fact       ■"))
            self.assertTrue(lines[0].endswith(" 12000"))
            self.assertTrue(lines[2].endswith("     0"))
            self.assertTrue(all(len(line) == kinds.size.width for line in lines))
            days = screen.query_one("#memory-days", BrailleGraph)
            self.assertEqual(days.values[-3:], (0, 3, 1))
            # The busy day 300 days ago is off screen, so the busiest shown day fills the height.
            self.assertEqual(days.maximum, 3)
            pending = screen.query_one("#memory-pending", Static)
            self.assertEqual(str(pending.render()), "3 embedding")
            self.assertTrue(pending.has_class("memory-waiting"))

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
