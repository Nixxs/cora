"""Presence 1: once the PC sits idle she looks through the webcam, and the Decisions API weighs whether
anyone's in her room. Frames are never kept; looks go to Activity, and nothing is done about them yet."""

import asyncio
import base64
from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from home.presence import (
    EMPTY_AT, IDLE_BEFORE_LOOK_SECONDS, LOOK_EVERY_SECONDS, SOMEONE_AT, CameraError, Look, due, evidence,
    judge, parse_grab, verdict,
)
from storage.app_settings_store import AppSettingsStore
import test_keyboard_persistence as keyboard_tests

IDLE = IDLE_BEFORE_LOOK_SECONDS
EVENING = datetime(2026, 11, 21, 20, 15)


class WhenTests(unittest.TestCase):
    def test_only_once_idle_and_not_again_too_soon_in_the_same_quiet_stretch(self):
        self.assertFalse(due(None, None))
        self.assertFalse(due(IDLE - 1, None))
        self.assertTrue(due(IDLE, None))
        # Looked ten minutes into this quiet stretch: not again until half an hour has passed.
        self.assertFalse(due(IDLE + 600, 300))
        self.assertTrue(due(IDLE + LOOK_EVERY_SECONDS, LOOK_EVERY_SECONDS))
        # The last look came before someone used the PC: this is a new quiet stretch.
        self.assertTrue(due(IDLE, IDLE + 60))


class GrabTests(unittest.TestCase):
    def test_the_frame_is_found_among_the_drivers_chatter(self):
        frame = base64.b64encode(b"\xff\xd8jpeg").decode()
        self.assertEqual(parse_grab(f"[VCAMDS] init\nFRAME {frame}\n[VCAMDS] bye\n"), b"\xff\xd8jpeg")
        with self.assertRaisesRegex(CameraError, "no camera"):
            parse_grab("[VCAMDS] init\nERROR no camera\n")
        with self.assertRaises(CameraError):
            parse_grab("")


class JudgeTests(unittest.TestCase):
    def test_the_evidence_reads_plainly(self):
        notes = evidence(EVENING, "Office", 17 * 60, 40 * 60, True, "off; the room reads 24°C",
                         Look(0.12, datetime(2026, 11, 21, 19, 45), 900))
        self.assertIn("Saturday 20:15. The room is Office", notes)
        self.assertIn("keyboard or mouse for 17 minutes", notes)
        self.assertIn("last spoke to it 40 minutes ago", notes)
        self.assertIn("air conditioner: off; the room reads 24°C", notes)
        self.assertIn("At 19:45 the room was judged 12% likely", notes)
        self.assertIn("in standby", evidence(EVENING, "Office", None, None, False, "", None))

    def test_one_request_with_the_frame_and_only_the_probability_back(self):
        answer = SimpleNamespace(type="predicate", name="someone_there", probability=0.08)
        client = MagicMock()
        client.__enter__.return_value = client
        client.decisions.create.return_value = SimpleNamespace(answers=[answer], usage=SimpleNamespace(input_tokens=812))
        with patch("openai.OpenAI", return_value=client):
            self.assertEqual(judge(b"jpeg", "notes here"), (0.08, 812))
        content = client.decisions.create.call_args.kwargs["input"][0]["content"]
        self.assertIn("notes here", content[0]["text"])
        self.assertEqual(content[1]["image_url"], "data:image/jpeg;base64," + base64.b64encode(b"jpeg").decode())
        client.decisions.create.return_value = SimpleNamespace(answers=[], usage=SimpleNamespace(input_tokens=1))
        with patch("openai.OpenAI", return_value=client), self.assertRaises(ValueError):
            judge(b"jpeg", "notes")

    def test_the_verdict(self):
        self.assertEqual([verdict(p) for p in (SOMEONE_AT, 0.5, EMPTY_AT)], ["someone's there", "unsure", "empty"])


class StoreTests(unittest.TestCase):
    def test_off_unless_switched_on(self):
        store = AppSettingsStore(MagicMock())
        with patch.object(store, "_switch", return_value=None):
            self.assertFalse(store.presence_enabled())
        with patch.object(store, "_set") as saved:
            store.set_presence_enabled(True)
            saved.assert_called_once_with("presence", "on")


class AppTests(unittest.IsolatedAsyncioTestCase):
    async def look(self, app, probability=0.1, asked=False):
        with patch("app_areas.presence.grab_frame", return_value=b"jpeg"), \
                patch("app_areas.presence.judge", return_value=(probability, 812)) as judged, \
                patch("app_areas.presence.idle_seconds", return_value=IDLE + 120):
            await app.look_for_someone(asked=asked).wait()
        return judged

    async def test_a_look_goes_to_activity(self):
        async with keyboard_tests.running_app() as (app, pilot):
            judged = await self.look(app, 0.1)
            self.assertIn("Nobody has touched the PC's keyboard or mouse for 17 minutes", judged.call_args.args[1])
            entries = "\n".join(app.activity.entries)
            self.assertIn("a home office: empty (10% someone's there) · PC idle 17 minutes", entries)
            self.assertEqual(app.presence_status(), "off · looked "
                             f"{app.presence_last.at:%H:%M}: empty (10% someone's there)")
            # The next look knows the last.
            judged = await self.look(app, 0.9)
            self.assertIn("judged 10% likely", judged.call_args.args[1])

    async def test_a_camera_problem_is_written_once(self):
        async with keyboard_tests.running_app() as (app, pilot):
            for _ in range(2):
                with patch("app_areas.presence.grab_frame", side_effect=CameraError("The camera gave no picture.")):
                    await app.look_for_someone().wait()
            entries = [entry for entry in app.activity.entries if "camera gave no picture" in entry]
            self.assertEqual(len(entries), 1)
            self.assertEqual(app.presence_status(), "The camera gave no picture.")

    async def test_the_check_looks_only_when_due(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.presence_enabled = True
            with patch.object(app, "look_for_someone") as look:
                with patch("app_areas.presence.idle_seconds", return_value=60.0):
                    app.presence_check()
                look.assert_not_called()
                with patch("app_areas.presence.idle_seconds", return_value=IDLE + 1):
                    app.presence_check()
                look.assert_called_once()
                # Spoken to a minute ago: still about, whatever the PC says.
                app.person_heard_at = __import__("time").monotonic() - 60
                with patch("app_areas.presence.idle_seconds", return_value=IDLE + 1):
                    app.presence_check()
                look.assert_called_once()

    async def test_the_switch_is_saved_and_runs_the_check(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.save_presence_enabled(True)
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            app.persistence.save_presence_enabled.assert_called_once_with(True)
            self.assertIsNotNone(app.presence_timer)
            app.save_presence_enabled(False)
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            self.assertIsNone(app.presence_timer)


class SettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_switch_and_look_now(self):
        from textual.widgets import Button, Static, Switch
        from test_model_settings_screen import MODELS
        from ui.model_settings import ModelSettingsScreen

        async with keyboard_tests.running_app() as (app, pilot):
            app.persistence.load_models.return_value = MODELS
            await pilot.click("#settings-button")
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            await pilot.pause()
            self.assertIsInstance(app.screen, ModelSettingsScreen)
            await asyncio.wait_for(app.screen.all_built.wait(), 5)
            screen = app.screen
            with patch.object(app, "look_for_someone") as look:
                screen.query_one("#home-look-now", Button).press()
                await pilot.pause()
                look.assert_called_once_with(asked=True)
            screen.query_one("#home-presence", Switch).value = True
            await pilot.pause()
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            app.persistence.save_presence_enabled.assert_called_once_with(True)
            await pilot.pause(0.6)
            self.assertIn("waits for the PC", str(screen.query_one("#home-presence-status", Static).render()))


class ViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_dashboard_line_for_each_state(self):
        from time import monotonic

        async with keyboard_tests.running_app() as (app, pilot):
            self.assertIsNone(app.presence_view())
            app.presence_enabled = True
            with patch("app_areas.presence.idle_seconds", return_value=IDLE + 60):
                self.assertEqual(app.presence_view(), ("watching · no look yet", "waiting"))
                app.presence_last = Look(0.08, datetime(2026, 11, 21, 20, 15), 812)
                app.presence_looked_at = monotonic() - 30
                self.assertEqual(app.presence_view(), ("empty · looked 20:15 (8%)", "empty"))
            # Someone at the PC since that look: plainly there, whatever it said.
            with patch("app_areas.presence.idle_seconds", return_value=5.0):
                self.assertEqual(app.presence_view(), ("someone's here · active just now", "here"))
            app.presence_problem = "The camera gave no picture."
            self.assertEqual(app.presence_view(), ("The camera gave no picture.", "problem"))


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_under_her_room_or_on_its_own_without_sensibo(self):
        from textual.widgets import Static
        from ui.dashboard import DashboardScreen
        import test_home

        async with keyboard_tests.running_app() as (app, pilot):
            app.presence_enabled = True
            app.presence_last = Look(0.08, datetime(2026, 11, 21, 20, 15), 812)
            with patch("app_areas.presence.idle_seconds", return_value=IDLE + 60):
                await pilot.click("#view-button")
                await pilot.pause()
                screen = app.screen
                self.assertIsInstance(screen, DashboardScreen)
                await pilot.pause()
                self.assertTrue(screen.query_one("#home-box").display)
                shown = str(screen.query_one("#home-rooms", Static).render())
                self.assertEqual(shown.splitlines(), ["◆ her room", "  ◉ empty · looked 20:15 (8%)"])
                test_home.AppTests.ready(self, app)
                await app.update_her_room()
                screen.show_home()
                lines = str(screen.query_one("#home-rooms", Static).render()).splitlines()
                self.assertEqual(lines[:2], ["◆ Office · her room", "  ◉ empty · looked 20:15 (8%)"])
                app.presence_enabled = app.sensibo_enabled = False
                screen.show_home()
                await pilot.pause()
                self.assertIn("home-box", screen.absent)


if __name__ == "__main__":
    unittest.main()
