"""The title bar's view button switches between the chat and the dashboard. Mocked APIs only."""

import asyncio
from datetime import date, datetime, timedelta, timezone
from time import monotonic
import unittest
from unittest.mock import MagicMock, PropertyMock, patch
from uuid import uuid4

import numpy as np
from textual.containers import HorizontalScroll
from textual.geometry import Size
from textual.widgets import Button, Static

from devices.audio_bands import BAND_COUNT, audio_bands
from live import CoraLive
from session_config import BACKEND_MODEL, MODEL_AREAS
from storage.dashboard_snapshot import (
    HISTORY_LIMIT, CALENDAR_DAYS, DashboardSnapshot, live_interests, next_due, read_dashboard_snapshot,
)
from subconscious.identity.identity_rules import Trait
from subconscious.interests.evolve_interests import EVOLVE_EVERY
from subconscious.interests.exploration_store import DAILY_LIMIT, MIN_GAP
from subconscious.relationship.relationship_rules import RelationshipValues
from ui.braille import BrailleGraph
from ui.chat_widgets import SelectableRichLog
from ui.composer import ChatComposer
from ui.dashboard import BOX_TITLES, FALL_PER_TICK, DashboardScreen, ago, until
from ui.meters import (
    HEAT_STYLES, LABEL_STYLE, TONE_STYLES, TRACK_STYLE, CalendarHeatmap, Meters, ToneRibbon, meter_bar,
)
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

    def test_heatmap_puts_today_in_the_last_column_and_brightens_busier_days(self):
        heatmap = CalendarHeatmap()
        heatmap.days = (4, 0, 1, 2)  # 3 days ago .. today
        heatmap.today_row = 2  # Wednesday
        # Day initials, then four weeks of cell-and-gap.
        with patch.object(CalendarHeatmap, "size", new_callable=PropertyMock, return_value=Size(10, 7)):
            text = heatmap.render()
        self.assertEqual(text.plain.splitlines(), [
            "M       ■ ",
            "T       ■ ",
            "W       ■ ",
            "T         ",
            "F         ",
            "S         ",
            "S     ■   ",
        ])
        cells = [span.style for span in text.spans if span.style != LABEL_STYLE]
        # Monday had none; Tuesday 1 and Wednesday 2 of the busiest 4; last Sunday was the busiest.
        self.assertEqual(cells, [TRACK_STYLE, HEAT_STYLES[0], HEAT_STYLES[1], HEAT_STYLES[3]])

    def test_hovering_a_heatmap_cell_tells_its_date_and_message_count(self):
        heatmap = CalendarHeatmap()
        heatmap.days = (4, 0, 1, 2)  # 3 days ago .. today
        heatmap.today_row = 2
        with patch.object(CalendarHeatmap, "size", new_callable=PropertyMock, return_value=Size(10, 7)):
            # Today, last Sunday, and nothing on a gap, an initial, the future or before the data.
            self.assertEqual(heatmap.days_ago_at(8, 2), 0)
            self.assertEqual(heatmap.days_ago_at(6, 6), 3)
            for x, y in ((7, 2), (0, 2), (8, 3), (2, 6)):
                self.assertIsNone(heatmap.days_ago_at(x, y))
            heatmap.on_mouse_move(MagicMock(x=6, y=6))
            last_sunday = date.today() - timedelta(days=3)
            self.assertEqual(heatmap.tooltip, f"{last_sunday:%a %d %b}: 4 messages")
            heatmap.on_mouse_move(MagicMock(x=8, y=1))
            self.assertTrue(heatmap.tooltip.endswith(": 1 message"))
            heatmap.on_mouse_move(MagicMock(x=7, y=1))
            self.assertIsNone(heatmap.tooltip)

    def test_ago_uses_the_largest_whole_unit(self):
        now = datetime(2026, 10, 20, tzinfo=timezone.utc)
        self.assertEqual(ago(None, now), "never")
        self.assertEqual(ago(now - timedelta(seconds=59), now), "just now")
        self.assertEqual(ago(now - timedelta(minutes=5, seconds=59), now), "5m ago")
        self.assertEqual(ago(now - timedelta(hours=3, minutes=59), now), "3h ago")
        self.assertEqual(ago(now - timedelta(days=2, hours=23), now), "2d ago")

    def test_until_says_due_once_the_time_has_come(self):
        now = datetime(2026, 10, 20, tzinfo=timezone.utc)
        self.assertEqual(until(None, now), "due")
        self.assertEqual(until(now, now), "due")
        self.assertEqual(until(now - timedelta(hours=1), now), "due")
        self.assertEqual(until(now + timedelta(seconds=30), now), "soon")
        self.assertEqual(until(now + timedelta(hours=5, minutes=30), now), "in 5h")

    def test_next_due_adds_each_cooldown_to_the_last_run(self):
        now = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
        runs = {"interests": now, "identity": None}
        with patch("storage.dashboard_snapshot.Session") as session:
            scalars = session.return_value.__enter__.return_value.scalars.return_value
            scalars.all.return_value = []
            self.assertEqual(next_due("engine", runs), {
                "interests": now + EVOLVE_EVERY, "exploration": None, "identity": None,
            })
            # Two recent starts: only the gap after the newest applies.
            scalars.all.return_value = [now, now - timedelta(hours=7)]
            self.assertEqual(next_due("engine", runs)["exploration"], now + MIN_GAP)
            # A full day's starts (the limit is 3): wait until the oldest is 24 hours old.
            self.assertEqual(DAILY_LIMIT, 3)
            oldest = now - timedelta(hours=12)
            scalars.all.return_value = [now, now - timedelta(hours=6), oldest]
            self.assertEqual(next_due("engine", runs)["exploration"], oldest + timedelta(hours=24))

    def test_snapshot_reads_relationship_values_drifted_to_now_history_tones_and_memory_kinds(self):
        now = datetime(2026, 10, 20, tzinfo=timezone.utc)
        values = RelationshipValues(0.5, 0.6, 0.7)
        events = [MagicMock(trust=index / 1000, warmth=0.5, familiarity=0.1) for index in range(250)]
        with patch("storage.dashboard_snapshot.RelationshipStore") as store, \
                patch("storage.dashboard_snapshot.MemoryStore") as memories, \
                patch("storage.dashboard_snapshot.last_runs", return_value={"summaries": None}), \
                patch("storage.dashboard_snapshot.next_due", return_value={"identity": None}) as due, \
                patch("storage.dashboard_snapshot.ConversationStore") as conversations, \
                patch("storage.dashboard_snapshot.IdentityStore") as identity, \
                patch("storage.dashboard_snapshot.live_interests",
                      return_value=((("Tides", 0.3, True),), ("Tides", "Moon"))):
            store.return_value.current_values.return_value = values
            store.return_value.history.return_value = events
            store.return_value.recent_tones.return_value = ["warm", "curt"]
            memories.return_value.kind_counts.return_value = {"fact": 3}
            memories.return_value.daily_counts.return_value = [0, 2]
            memories.return_value.pending_counts.return_value = {"embedding": 1}
            conversations.return_value.daily_message_counts.return_value = [3, 0]
            identity.return_value.list_traits.return_value = ["trait"]
            snapshot = read_dashboard_snapshot("engine", now)
        self.assertEqual(snapshot.traits, ("trait",))
        conversations.return_value.daily_message_counts.assert_called_once_with(now, CALENDAR_DAYS)
        self.assertEqual(snapshot.message_days, (3, 0))
        store.assert_called_once_with("engine")
        memories.assert_called_once_with("engine")
        memories.return_value.daily_counts.assert_called_once_with(now, HISTORY_LIMIT)
        self.assertEqual(snapshot.memory_days, (0, 2))
        self.assertEqual(snapshot.memory_pending, {"embedding": 1})
        self.assertEqual(snapshot.interests, (("Tides", 0.3, True),))
        self.assertEqual(snapshot.latest_finding, ("Tides", "Moon"))
        self.assertEqual(snapshot.last_runs, {"summaries": None})
        due.assert_called_once_with("engine", {"summaries": None})
        self.assertEqual(snapshot.next_due, {"identity": None})
        store.return_value.current_values.assert_called_once_with(now=now)
        store.return_value.recent_tones.assert_called_once_with(HISTORY_LIMIT)
        self.assertEqual(snapshot.relationship, values)
        self.assertEqual(snapshot.tones, ("warm", "curt"))
        self.assertEqual(snapshot.memory_kinds, {"fact": 3})
        # Only the latest events, oldest first.
        self.assertEqual(len(snapshot.history), HISTORY_LIMIT)
        self.assertEqual(snapshot.history[0], RelationshipValues(0.05, 0.5, 0.1))
        self.assertEqual(snapshot.history[-1], RelationshipValues(0.249, 0.5, 0.1))

    def test_snapshot_reads_live_interests_decayed_to_now_and_their_newest_finding(self):
        now = datetime(2026, 10, 20, tzinfo=timezone.utc)
        week_ago = datetime(2026, 10, 13, tzinfo=timezone.utc)
        interests = [
            # Reinforced a week ago, so only decaying since.
            MagicMock(id=1, title="Old", strength=0.8, strength_at=week_ago, status="active",
                      last_reinforced_at=week_ago, created_at=week_ago),
            # Never reinforced but spawned today.
            MagicMock(id=2, title="New", strength=0.5, strength_at=now, status="emerging",
                      last_reinforced_at=None, created_at=now),
            # Halves to 0.08, below the dormant threshold.
            MagicMock(id=3, title="Fading", strength=0.16, strength_at=week_ago, status="active",
                      last_reinforced_at=None, created_at=week_ago),
        ]
        with patch("storage.dashboard_snapshot.InterestStore") as store:
            store.return_value.list_interests.return_value = interests
            store.return_value.latest_findings.return_value = {
                1: ("Newer finding", now), 2: ("Older finding", week_ago),
            }
            self.assertEqual(live_interests("engine", now), (
                (("New", 0.5, True), ("Old", 0.4, False)), ("Old", "Newer finding"),
            ))
        store.assert_called_once_with("engine")
        # Only live interests' findings are read.
        store.return_value.latest_findings.assert_called_once_with([1, 2])


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

    async def test_middle_row_pans_sideways_when_its_boxes_do_not_fit(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await pilot.click("#view-button")
            await pilot.pause()
            row = app.screen.query_one("#dashboard-middle", HorizontalScroll)
            boxes = [app.screen.query_one(f"#{box_id}") for box_id in (
                "relationship-box", "memories-box", "interests-box", "identity-box",
            )]
            # 80 columns can't hold four 34-wide boxes, so the row scrolls instead of squashing them.
            self.assertEqual({box.outer_size.width for box in boxes}, {34})
            self.assertGreater(row.max_scroll_x, 0)
            # The scrollbar gets its own line rather than cutting into the boxes.
            self.assertEqual({box.outer_size.height for box in boxes}, {16})
            self.assertEqual(row.outer_size.height, 17)
            self.assertFalse(row.can_focus)

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

    async def test_voice_box_shows_the_latest_activity_summary(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await pilot.click("#view-button")
            await pilot.pause()
            app.activity.write("details", "Dashboard load failed [x]")
            await pilot.pause(0.3)
            summary = app.screen.query_one("#dashboard-activity", Static)
            self.assertEqual(str(summary.render()), "Activity · Dashboard load failed [x]")
            self.assertTrue(summary.has_class("activity-error"))

    async def test_snapshot_loads_on_open_and_a_failed_refresh_keeps_it(self):
        async with keyboard_tests.running_app() as (app, pilot):
            first = DashboardSnapshot(
                relationship=RelationshipValues(0.5, 0.6, 0.7), history=(), tones=(), memory_kinds={},
                memory_days=(), memory_pending={}, interests=(), latest_finding=None, last_runs={}, next_due={}, message_days=(), traits=(),
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
                interests=(),
                latest_finding=None,
                last_runs={},
                next_due={},
                message_days=(),
                traits=(),
            )
            await screen.refresh_snapshot().wait()
            await pilot.pause()
            self.assertEqual(
                meters.rows, (("trust", 0.5), ("warmth", 0.75), ("familiarity", 0.25)),
            )
            lines = str(meters.render()).splitlines()
            self.assertEqual(len(lines), 3)
            # The widget grows from the one-line placeholder to show every row.
            self.assertEqual(meters.size.height, 3)
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
                memory_pending={"embedding": 3, "duplicate check": 0, "correction check": 0, "fade check": 2},
                interests=(),
                latest_finding=None,
                last_runs={},
                next_due={},
                message_days=(),
                traits=(),
            )
            await screen.refresh_snapshot().wait()
            await pilot.pause()
            self.assertEqual(kinds.rows[0], ("fact", 0.6, "12000"))
            lines = str(kinds.render()).splitlines()
            self.assertEqual(len(lines), 4)
            self.assertEqual(kinds.size.height, 4)
            self.assertTrue(lines[0].startswith("fact       ■"))
            self.assertTrue(lines[0].endswith(" 12000"))
            self.assertTrue(lines[2].endswith("     0"))
            self.assertTrue(all(len(line) == kinds.size.width for line in lines))
            days = screen.query_one("#memory-days", BrailleGraph)
            self.assertEqual(days.values[-3:], (0, 3, 1))
            # The busy day 300 days ago is off screen, so the busiest shown day fills the height.
            self.assertEqual(days.maximum, 3)
            # Shaded with recall's fade weight: today full, older days dimmer.
            self.assertEqual(days.shade(0), 1.0)
            self.assertLess(days.shade(45), 1.0)
            pending = screen.query_one("#memory-pending", Static)
            self.assertEqual(str(pending.render()), "3 embedding · 2 fade check")
            self.assertTrue(pending.has_class("memory-waiting"))

    async def test_interests_box_shows_strengths_or_a_placeholder(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await pilot.click("#view-button")
            await pilot.pause()
            screen = app.screen
            await asyncio.wait_for(screen.workers.wait_for_complete(), 3)
            strengths = screen.query_one("#interest-strengths", Meters)
            finding = screen.query_one("#interest-finding", Static)
            self.assertEqual(str(strengths.render()), "no interests yet")
            self.assertEqual(str(finding.render()), "nothing found yet")

            interests = (("Tidal energy", 0.5, True), ("A very long interest title indeed", 0.25, False))
            app.persistence.load_dashboard_snapshot.return_value = DashboardSnapshot(
                relationship=None, history=(), tones=(), memory_kinds={}, memory_days=(),
                memory_pending={}, interests=interests,
                latest_finding=("Tidal energy", "Lagoons [bold]store[/bold] tides"),
                last_runs={},
                next_due={},
                message_days=(),
                traits=(),
            )
            await screen.refresh_snapshot().wait()
            await pilot.pause()
            # Brackets in the finding are shown as written, not read as markup.
            self.assertEqual(
                str(finding.render()), "Tidal energy: Lagoons [bold]store[/bold] tides",
            )
            self.assertEqual(strengths.rows[0], ("Tidal energy", 0.5, "↑ 50%"))
            lines = str(strengths.render()).splitlines()
            half = strengths.size.width // 2
            # The long title is cut to half the width, leaving room for its bar.
            self.assertEqual(lines[1][:half], "A very long interest title indeed"[:half - 1] + "…")
            self.assertTrue(lines[0].startswith("Tidal energy"))
            self.assertIn("■", lines[1])
            self.assertTrue(lines[1].endswith(" ↓ 25%"))
            self.assertTrue(all(len(line) == strengths.size.width for line in lines))

    async def test_identity_box_lists_traits_and_dims_the_ones_still_forming(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await pilot.click("#view-button")
            await pilot.pause()
            screen = app.screen
            await asyncio.wait_for(screen.workers.wait_for_complete(), 3)
            traits = screen.query_one("#identity-traits", Static)
            self.assertEqual(str(traits.render()), "no traits yet")

            app.persistence.load_dashboard_snapshot.return_value = DashboardSnapshot(
                relationship=None, history=(), tones=(), memory_kinds={}, memory_days=(),
                memory_pending={}, interests=(), latest_finding=None, last_runs={}, next_due={},
                message_days=(),
                traits=(
                    Trait(uuid4(), "value", "I say what I [b]think[/b]", None, None, 0.7, "active"),
                    Trait(uuid4(), "tendency", "I ask follow-up questions", None, None, 0.3, "active"),
                ),
            )
            await screen.refresh_snapshot().wait()
            await pilot.pause()
            text = traits.render()
            # Brackets in a statement are shown as written, not read as markup.
            self.assertEqual(str(text).splitlines(), [
                "70% value",
                "I say what I [b]think[/b]",
                "",
                "30% tendency · forming",
                "I ask follow-up questions",
            ])
            # The held trait's confidence is bright and its statement unstyled; the forming one is dim.
            colours = [span.style.foreground.hex.lower() for span in text.spans]
            self.assertNotEqual(colours[0], LABEL_STYLE)
            self.assertEqual(colours[2:], [LABEL_STYLE] * 3)
            self.assertEqual(text.spans[1].end, len("70% value"))

    async def test_background_box_lists_when_each_job_last_ran_and_is_next_due(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await pilot.click("#view-button")
            await pilot.pause()
            screen = app.screen
            await asyncio.wait_for(screen.workers.wait_for_complete(), 3)
            now = datetime.now(timezone.utc)
            app.persistence.load_dashboard_snapshot.return_value = DashboardSnapshot(
                relationship=None, history=(), tones=(), memory_kinds={}, memory_days=(),
                memory_pending={}, interests=(), latest_finding=None,
                last_runs={
                    "summaries": now - timedelta(hours=3, minutes=5),
                    "duplicate check": None,
                    "interests": now - timedelta(hours=2, minutes=5),
                    "identity": None,
                },
                next_due={"interests": now + timedelta(hours=21, minutes=55), "identity": None},
                message_days=(),
                traits=(),
            )
            await screen.refresh_snapshot().wait()
            await pilot.pause()
            self.assertEqual(
                str(screen.query_one("#background-runs", Static).render()).splitlines(),
                [
                    "summaries        3h ago",
                    "duplicate check  never",
                    "interests        2h ago    in 21h",
                    "identity         never     due",
                ],
            )
            # The box hugs its widest line (plus border and padding); conversations gets the rest.
            self.assertEqual(screen.query_one("#background-box").outer_size.width, 33 + 4)
            self.assertGreater(
                screen.query_one("#conversations-box").outer_size.width, 80 - 37 - 2,
            )

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
