"""The title bar's view button switches between the chat and the dashboard. Mocked APIs only."""

import asyncio
from datetime import date, datetime, timedelta, timezone
from time import monotonic
import unittest
from unittest.mock import MagicMock, PropertyMock, patch
from uuid import uuid4

import numpy as np
from PIL import Image
from textual.containers import HorizontalScroll
from textual.geometry import Size
from textual.widgets import Button, Label, Static, TextArea

from devices.audio_bands import BAND_COUNT, audio_bands
from live import CoraLive
from session_config import BACKEND_MODEL, MODEL_AREAS, config_from_sections, instruction_sections
from storage.dashboard_snapshot import (
    HISTORY_LIMIT, CALENDAR_DAYS, DashboardSnapshot, live_interests, next_due, read_dashboard_snapshot,
)
from storage.task_store import PartyView, TaskView
from subconscious.identity.identity_rules import Trait
from subconscious.interests.evolve_interests import EVOLVE_EVERY
from subconscious.interests.exploration_store import DAILY_LIMIT, MIN_GAP
from subconscious.relationship.relationship_rules import RelationshipValues
from ui.braille import BrailleGraph
from ui.chat_widgets import SelectableRichLog
from ui.composer import IMAGE_MARKER, ChatComposer
from ui.dashboard import BOX_TITLES, FALL_PER_TICK, DashboardScreen, ago, until
from ui.meters import (
    HEAT_STYLES, HOVER_STYLE, LABEL_STYLE, MAX_WEEKS, MIN_WEEKS, TONE_STYLES, TRACK_STYLE, CalendarHeatmap,
    ContextMeters, Meters, ToneRibbon, meter_bar,
)
from ui.context_view import ContextScreen
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
                patch("storage.dashboard_snapshot.TaskStore") as tasks, \
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
            tasks.return_value.open_tasks.return_value = ["task"]
            snapshot = read_dashboard_snapshot("engine", now)
        self.assertEqual(snapshot.traits, ("trait",))
        self.assertEqual(snapshot.tasks, ("task",))
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

    async def test_dashboard_attaches_removes_and_sends_a_pasted_image_like_the_chat(self):
        with patch("ui.composer.ImageGrab.grabclipboard", return_value=Image.new("RGB", (4, 4))), \
                patch("ui.chat_submission.look", return_value="A black square."):
            async with keyboard_tests.running_app() as (app, pilot):
                await pilot.click("#view-button")
                await pilot.pause()
                composer = app.screen.query_one("#dashboard-input", ChatComposer)
                box = app.screen.query_one("#chat-box")
                await pilot.press("ctrl+v")
                self.assertEqual(box.border_subtitle, IMAGE_MARKER)
                self.assertEqual(box.styles.border_subtitle_align, "left")
                await pilot.press("escape")
                self.assertIsNone(composer.image_png)
                self.assertEqual(box.border_subtitle, "")
                self.assertIsInstance(app.screen, DashboardScreen)
                await pilot.press("ctrl+v")
                composer.load_text("What's this?")
                await pilot.press("enter")
                await asyncio.wait_for(app.workers.wait_for_complete(), 3)
                await pilot.pause()
                self.assertEqual(app.live.send_text.await_args.args, ("What's this?",))
                self.assertEqual(app.live.send_text.await_args.kwargs, {"image_seen": "A black square."})
                self.assertIsNone(composer.image_png)
                self.assertEqual(box.border_subtitle, "")

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

    async def test_voice_box_shows_standby_at_its_top_right(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await pilot.click("#view-button")
            await pilot.pause()
            indicator = app.screen.query_one("#dashboard-standby", Static)
            self.assertFalse(indicator.display)
            app.standby = True
            await pilot.pause(0.3)
            self.assertTrue(indicator.display)
            header = app.screen.query_one("#voice-header")
            # Just before the top right corner, on the same line as the box's title.
            self.assertEqual(str(indicator.render()), "○ standby")
            self.assertEqual(indicator.region.y, header.region.y)
            self.assertEqual(indicator.region.right, header.region.right - 2)
            self.assertEqual(app.screen.query_one("#voice-title", Static).region.x, header.region.x + 2)
            app.standby = False
            await pilot.pause(0.3)
            self.assertFalse(indicator.display)

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
            # The box hugs its widest line (plus border and padding).
            self.assertEqual(screen.query_one("#background-box").outer_size.width, 33 + 4)

    async def test_tasks_box_shows_each_open_task_with_its_peoples_next_checks(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await pilot.click("#view-button")
            await pilot.pause()
            screen = app.screen
            await asyncio.wait_for(screen.workers.wait_for_complete(), 3)
            tasks = screen.query_one("#task-list", Static)
            self.assertEqual(str(tasks.render()), "no open tasks")
            self.assertEqual(screen.query_one("#tasks-box").border_title, "tasks")
            now = datetime.now(timezone.utc)
            app.persistence.load_dashboard_snapshot.return_value = DashboardSnapshot(
                relationship=None, history=(), tones=(), memory_kinds={}, memory_days=(),
                memory_pending={}, interests=(), latest_finding=None, last_runs={}, next_due={},
                message_days=(), traits=(),
                tasks=(
                    TaskView("Lesson for [b]Sarah[/b]", "waiting", now - timedelta(days=2, minutes=5),
                             now + timedelta(days=5, minutes=5), (
                                 PartyView("Karene", "waiting", 0, now + timedelta(hours=6, minutes=5)),
                                 PartyView("Royce", "waiting", 1, now - timedelta(minutes=5)),
                                 PartyView("Al", "waiting", 2, now + timedelta(hours=1, minutes=5)),
                                 PartyView("Sam", "done", 0, None),
                             ), "Sam reported:\nTuesdays " + "x" * 200),
                    TaskView("Car service", "needs_person", now - timedelta(hours=3, minutes=5), None,
                             (PartyView("Royce", "waiting", 0, None),), None),
                ),
            )
            await screen.refresh_snapshot().wait()
            await pilot.pause()
            lines = str(tasks.render()).splitlines()
            # Brackets in a goal are shown as written, not read as markup.
            self.assertEqual(lines[:6], [
                "● Lesson for [b]Sarah[/b]",
                "  waiting · started 2d ago · back to you in 5d",
                "  Karene  nudge 1/2 in 6h",
                "  Royce   nudge 2/2 next check",
                "  Al      no answer · back to you in 1h",
                "  Sam     answered",
            ])
            self.assertTrue(lines[6].startswith("  ↳ Sam reported: Tuesdays xxx"))
            self.assertTrue(lines[6].endswith("…"))
            self.assertEqual(lines[7:], ["", "● Car service", "  needs you · started 3h ago", "  Royce  asked"])
            self.assertEqual(screen.query_one("#tasks-box").border_subtitle, "2 open")

    async def test_context_box_lists_each_models_startup_items_once_connected(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await pilot.click("#view-button")
            await pilot.pause()
            screen = app.screen
            placeholder = screen.query_one("#context-placeholder", Static)
            self.assertTrue(placeholder.display)
            self.assertFalse(screen.query_one("#context-scroll").display)

            sections = instruction_sections("[1]")
            app.live.context.start(config_from_sections([], sections), sections)
            screen.show_context()
            await pilot.pause()
            self.assertFalse(placeholder.display)
            voice = screen.query_one("#context-voice", Meters)
            backend = screen.query_one("#context-backend", Meters)
            self.assertEqual([row[0] for row in voice.rows], ["instructions", "memories"])
            self.assertEqual([row[0] for row in backend.rows], ["instructions", "tools"])
            # Each bar is the item's share of its model's total; the text is its estimated size.
            self.assertAlmostEqual(sum(row[1] for row in voice.rows), 1.0)
            tokens = app.live.context.items("voice")[0].tokens
            self.assertEqual(voice.rows[0][2], f"~{tokens / 1000:.1f}k")

    async def test_clicking_a_context_item_shows_its_text_until_escape(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await pilot.resize_terminal(190, 53)
            await pilot.click("#view-button")
            await pilot.pause()
            screen = app.screen
            sections = instruction_sections("[1]")
            app.live.context.start(config_from_sections([{"type": "function", "name": "close_app"}], sections), sections)
            screen.show_context()
            await pilot.pause()
            backend = screen.query_one("#context-backend", ContextMeters)
            # The second backend row is the tools.
            await pilot.click(backend, offset=(1, 1))
            await pilot.pause()
            self.assertIsInstance(app.screen, ContextScreen)
            title = str(app.screen.query_one("#context-view-title", Label).render())
            self.assertTrue(title.startswith("tools · backend · ~"))
            text = app.screen.query_one("#context-view-text", TextArea)
            self.assertTrue(text.read_only)
            self.assertIn('"name": "close_app"', text.text)
            # Clicking inside the box keeps it open for reading and selecting.
            await pilot.click(text)
            self.assertIsInstance(app.screen, ContextScreen)

            await pilot.press("escape")
            await pilot.pause()
            self.assertIs(app.screen, screen)

    def test_context_rows_highlight_on_hover_and_post_their_index_on_click(self):
        meters = ContextMeters()
        meters.rows = (("instructions", 0.5, "~1.3k"), ("memories", 0.2, "~532"))
        with patch.object(ContextMeters, "size", new_callable=PropertyMock, return_value=Size(30, 2)), \
                patch.object(ContextMeters, "post_message") as post:
            meters.on_mouse_move(MagicMock(y=1))
            self.assertEqual(meters.hover_row, 1)
            self.assertEqual(meters.tooltip, "click to see what's in it")
            hovered = [span for span in meters.render().spans if span.style == HOVER_STYLE]
            self.assertEqual([(span.start, span.end) for span in hovered], [(31, 61)])
            meters.on_click(MagicMock(y=1))
            self.assertEqual(post.call_args.args[0].index, 1)
            # Below the last row is neither hovered nor clickable.
            meters.on_mouse_move(MagicMock(y=2))
            self.assertIsNone(meters.hover_row)
            meters.on_click(MagicMock(y=2))
            self.assertEqual(post.call_count, 1)
            meters.on_mouse_move(MagicMock(y=0))
            meters.on_leave()
            self.assertIsNone(meters.hover_row)

    async def test_context_totals_show_what_the_api_measured(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await pilot.click("#view-button")
            await pilot.pause()
            screen = app.screen
            context = app.live.context
            sections = instruction_sections()
            context.start(config_from_sections([], sections), sections)
            screen.show_context()
            totals = screen.query_one("#context-totals", Meters)
            box = screen.query_one("#context-box")
            self.assertEqual(totals.rows, (("voice", 0.0, "measuring…"), ("backend", 0.0, "after a request")))
            self.assertEqual(box.border_subtitle, "")

            context.voice_ratio = 0.62
            context.backend_tokens = 18200
            # Without a known limit the backend shows only its size.
            with patch.dict("ui.dashboard.BACKEND_CONTEXT_TOKENS", clear=True):
                screen.show_context()
            self.assertEqual(totals.rows, (("voice", 0.62, "62%"), ("backend", 0.0, "18.2k")))
            with patch.dict("ui.dashboard.BACKEND_CONTEXT_TOKENS", {BACKEND_MODEL: 91000}):
                screen.show_context()
            self.assertEqual(totals.rows[1], ("backend", 0.2, "20% · 18.2k"))

            context.end()
            screen.show_context()
            self.assertEqual(box.border_subtitle, "last session")
            self.assertEqual(totals.rows[0], ("voice", 0.62, "62%"))

    async def test_bottom_row_pans_sideways_when_its_boxes_do_not_fit(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await pilot.resize_terminal(60, 35)
            await pilot.click("#view-button")
            await pilot.pause()
            screen = app.screen
            screen.query_one("#background-runs", Static).update("x" * 33)
            await pilot.pause()
            await pilot.pause()
            row = screen.query_one("#dashboard-bottom", HorizontalScroll)
            # Each box stops at its minimum and the row scrolls, rather than squashing them.
            self.assertEqual(screen.query_one("#conversations-box").outer_size.width, 16)
            self.assertEqual(screen.query_one("#context-box").outer_size.width, 40)
            self.assertGreater(row.max_scroll_x, 0)
            self.assertFalse(row.can_focus)
            self.assertEqual(screen.query_one("#conversation-days", CalendarHeatmap).visible_weeks(), MIN_WEEKS)

    async def test_wide_bottom_row_caps_the_heatmap_and_gives_context_the_rest(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await pilot.resize_terminal(240, 53)
            await pilot.click("#view-button")
            await pilot.pause()
            screen = app.screen
            heatmap = screen.query_one("#conversation-days", CalendarHeatmap)
            # No empty columns between the day initials and the grid.
            self.assertEqual(heatmap.visible_weeks(), MAX_WEEKS)
            self.assertEqual(heatmap.size.width, 2 + 2 * MAX_WEEKS)
            self.assertGreater(
                screen.query_one("#context-box").outer_size.width,
                screen.query_one("#conversations-box").outer_size.width,
            )
            self.assertEqual(screen.query_one("#dashboard-bottom", HorizontalScroll).max_scroll_x, 0)

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
