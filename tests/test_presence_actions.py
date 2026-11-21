"""Presence 2: acting on an empty room. Standby while she's in voice for no one; after an hour empty,
her room's air conditioner off (unless a timer or Climate React has it), told on Signal."""

from time import monotonic
import unittest
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from home.presence import EMPTY_FOR_SECONDS, IDLE_BEFORE_LOOK_SECONDS, confirm_due
from home.sensibo import parse_rooms
import test_home
from test_home import pod
import test_keyboard_persistence as keyboard_tests

HOUR = EMPTY_FOR_SECONDS


class ConfirmTests(unittest.TestCase):
    def test_one_look_once_the_quiet_reaches_an_hour(self):
        self.assertFalse(confirm_due(HOUR - 60, 600))
        # Last looked at 45 minutes of quiet: due at the hour.
        self.assertTrue(confirm_due(HOUR, 15 * 60))
        # Already looked since the hour came.
        self.assertFalse(confirm_due(HOUR + 120, 60))
        self.assertFalse(confirm_due(None, None))


class ActTests(unittest.IsolatedAsyncioTestCase):
    async def look(self, app, probability, quiet=IDLE_BEFORE_LOOK_SECONDS + 60, asked=False):
        with patch("app_areas.presence.grab_frame", return_value=b"jpeg"), \
                patch("app_areas.presence.judge", return_value=(probability, 812)), \
                patch("app_areas.presence.idle_seconds", return_value=quiet):
            await app.look_for_someone(asked=asked).wait()

    def in_voice(self, app, blockers=()):
        app.can_reach_by_voice = MagicMock(return_value=True)
        app.live_transition_blockers = MagicMock(return_value=list(blockers))
        app.action_standby = MagicMock()

    async def test_empty_in_voice_goes_to_standby(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.in_voice(app)
            await self.look(app, 0.1)
            app.action_standby.assert_called_once()
            self.assertIn("a home office looks empty; going to standby.", "\n".join(app.activity.entries))
            # It went quiet about 16 minutes before the look.
            self.assertAlmostEqual(monotonic() - app.presence_empty_since, IDLE_BEFORE_LOOK_SECONDS + 60, delta=5)

    async def test_not_while_busy_talking_by_phone_or_asked(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.in_voice(app, blockers=["tool execution"])
            await self.look(app, 0.1)
            app.action_standby.assert_not_called()
            self.in_voice(app)
            with patch.object(type(app.web_voice), "active", new_callable=PropertyMock, return_value=True):
                await self.look(app, 0.1)
            app.action_standby.assert_not_called()
            await self.look(app, 0.1, asked=True)
            app.action_standby.assert_not_called()
            # Not empty: no standby, and the empty stretch ends.
            await self.look(app, 0.5)
            app.action_standby.assert_not_called()
            self.assertIsNone(app.presence_empty_since)

    async def test_an_hour_empty_turns_her_rooms_aircon_off_once(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.can_reach_by_voice = MagicMock(return_value=False)
            app.turn_off_empty_room = AsyncMock()
            await self.look(app, 0.1)
            app.turn_off_empty_room.assert_not_awaited()
            # Most of an hour later, still empty.
            app.presence_empty_since -= HOUR - IDLE_BEFORE_LOOK_SECONDS
            await self.look(app, 0.1, quiet=HOUR + 30)
            app.turn_off_empty_room.assert_awaited_once()
            await self.look(app, 0.1, quiet=HOUR + 30 * 60)
            app.turn_off_empty_room.assert_awaited_once()


class TurnOffTests(unittest.IsolatedAsyncioTestCase):
    def ready(self, app, *pods):
        test_home.AppTests.ready(self, app)
        app.sensibo.rooms = AsyncMock(return_value=(parse_rooms({"result": list(pods)}), 0.0))
        app.change_aircon = AsyncMock(return_value={"status": "ok"})
        app.tell_person_on_signal = AsyncMock()
        app.signal_reaches_person = MagicMock(return_value=True)

    async def test_off_and_told_on_signal(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.ready(app, pod(on=True), pod("BpP9b5Ka", "Declan’s Room", on=True))
            await app.turn_off_empty_room()
            action, rooms, changes = app.change_aircon.call_args.args
            self.assertEqual((action, [room.name for room in rooms], changes["on"]), ("set", ["Office"], False))
            app.tell_person_on_signal.assert_awaited_once_with(
                "Office has looked empty for about an hour, so I turned its air conditioner off.")

    async def test_left_alone_when_off_or_kept_by_climate_react(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.ready(app, pod(on=False))
            await app.turn_off_empty_room()
            self.ready(app, pod(on=True) | {"smartMode": {"enabled": True, "lowTemperatureThreshold": 22,
                                                          "highTemperatureThreshold": 25}})
            await app.turn_off_empty_room()
            app.change_aircon.assert_not_awaited()
            app.tell_person_on_signal.assert_not_awaited()


class CheckTests(unittest.IsolatedAsyncioTestCase):
    async def test_coming_back_ends_the_stretch_and_the_hour_brings_a_look(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.presence_enabled = True
            app.presence_empty_since = monotonic() - HOUR
            app.presence_looked_at = monotonic() - 15 * 60
            app.her_aircon_on = MagicMock(return_value=True)
            with patch.object(app, "look_for_someone") as look:
                with patch("app_areas.presence.idle_seconds", return_value=HOUR):
                    app.presence_check()
                look.assert_called_once()
                # Someone touched the PC a minute ago.
                with patch("app_areas.presence.idle_seconds", return_value=60.0):
                    app.presence_check()
                self.assertIsNone(app.presence_empty_since)
                look.assert_called_once()



class AwayTests(unittest.TestCase):
    def test_roughly_how_long(self):
        from home.presence import away_words
        self.assertEqual(away_words(17 * 60), "about 15 minutes")
        self.assertEqual(away_words(4 * 60), "about 4 minutes")
        self.assertEqual(away_words(75 * 60), "about 1 hour 15 minutes")
        self.assertEqual(away_words(2 * 3600 + 60), "about 2 hours")


class BackTests(unittest.IsolatedAsyncioTestCase):
    def stretch(self, app, aircon_off=""):
        app.presence_enabled = True
        app.standby = True
        app.presence_empty_since = monotonic() - HOUR
        app.presence_looked_at = monotonic() - 10 * 60
        app.presence_aircon_off = aircon_off
        app.persistence.load_open_tasks.return_value = []

    async def come_back(self, app):
        with patch("app_areas.presence.idle_seconds", return_value=20.0):
            app.presence_check()
        await app.workers.wait_for_complete()

    async def test_nothing_waiting_she_leaves_a_note_and_stays_in_standby(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.stretch(app, aircon_off="Office")
            app.reply_in_writing = AsyncMock(return_value="Welcome back! I turned the office aircon off; want it on?")
            app.action_reconnect = MagicMock()
            await self.come_back(app)
            app.action_reconnect.assert_not_called()
            prompt = app.reply_in_writing.call_args.args[0][0]["text"]
            self.assertIn('"out_for": "about 1 hour"', prompt)
            self.assertIn('"air_conditioner_turned_off": "Office"', prompt)
            shown = [str(getattr(entry, "markup", entry)) for entry in app.conversation.log_entries]
            self.assertTrue(any("Welcome back! I turned the office aircon off" in line for line in shown), shown)
            self.assertIsNone(app.presence_empty_since)

    async def test_something_waiting_she_wakes_and_says_so(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.stretch(app)
            app.attention.pending.append("Claude Code finished in ComboDots.")
            app.reply_in_writing = AsyncMock()

            def reconnect():
                app.reconnecting = True
            app.action_reconnect = MagicMock(side_effect=reconnect)
            await self.come_back(app)
            app.action_reconnect.assert_called_once()
            app.reply_in_writing.assert_not_awaited()
            self.assertEqual(app.pending_greeting, "welcome_back")
            self.assertIn("1 thing you noticed while they were out", app.return_note)
            self.assertIn("out about 1 hour", app.return_note)

    async def test_awake_or_never_judged_empty_nothing_happens(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.stretch(app)
            app.standby = False
            app.person_back = MagicMock()
            await self.come_back(app)
            app.person_back.assert_not_called()
            self.stretch(app)
            app.presence_empty_since = None
            await self.come_back(app)
            app.person_back.assert_not_called()


if __name__ == "__main__":
    unittest.main()
