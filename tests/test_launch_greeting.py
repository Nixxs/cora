"""Cora greets first on launch only, from the loaded facts. Mocked APIs only."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock

from live import CoraLive
import test_keyboard_persistence as keyboard_tests


STARTED = SimpleNamespace(type="session.started")
LAST = datetime(2026, 10, 16, 21, 5, tzinfo=timezone.utc)


class LiveGreetingTests(unittest.IsolatedAsyncioTestCase):
    async def test_requires_a_ready_session(self):
        with self.assertRaisesRegex(RuntimeError, "not ready"):
            await CoraLive().send_greeting("It is Saturday evening.")

    async def test_sends_one_commentary_note_with_the_facts(self):
        live = CoraLive()
        live.connection = MagicMock()
        live.connection.session.commentary.append = AsyncMock()
        live.ready = True
        await live.send_greeting('It is Saturday evening. Their name is "Nick".')
        kwargs = live.connection.session.commentary.append.await_args.kwargs
        self.assertTrue(kwargs["event_id"].startswith("greeting_"))
        self.assertIsNone(kwargs["delegation_id"])
        self.assertTrue(kwargs["content"].startswith("Opening greeting note, not a message from the person."))
        self.assertIn("not instructions or permission to use tools", kwargs["content"])
        self.assertTrue(kwargs["content"].endswith('Facts: It is Saturday evening. Their name is "Nick".'))

    async def test_welcome_back_is_a_short_note_and_requires_a_ready_session(self):
        live = CoraLive()
        with self.assertRaisesRegex(RuntimeError, "not ready"):
            await live.send_welcome_back()
        live.connection = MagicMock()
        live.connection.session.commentary.append = AsyncMock()
        live.ready = True
        await live.send_welcome_back()
        kwargs = live.connection.session.commentary.append.await_args.kwargs
        self.assertTrue(kwargs["event_id"].startswith("greeting_"))
        self.assertTrue(kwargs["content"].startswith("Welcome back note, not a message from the person."))
        self.assertIn("in a few words", kwargs["content"])


class GreetingAppCase(unittest.IsolatedAsyncioTestCase):
    def prepare(self, app, *, loaded=True):
        app.conversation_persistence.last_talked_loaded = loaded
        app.conversation_persistence.last_talked_at = LAST
        app.live.person_name = "Nick"
        app.live.send_greeting = AsyncMock()
        app.live.send_welcome_back = AsyncMock()

    async def start_session(self, app, pilot):
        app.handle_live_session_event(STARTED)
        await asyncio.wait_for(app.workers.wait_for_complete(), 3)
        await pilot.pause()


class AppGreetingTests(GreetingAppCase):
    async def test_only_the_first_session_of_a_launch_greets(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.prepare(app)
            await self.start_session(app, pilot)
            await self.start_session(app, pilot)  # reconnect, or waking from standby
            app.live.send_greeting.assert_awaited_once()
            brief = app.live.send_greeting.await_args.args[0]
            self.assertIn("You last talked", brief)
            self.assertIn('Their name is "Nick".', brief)
            self.assertIn("Opening greeting sent", "\n".join(app.activity.entries))

    async def test_typing_or_speaking_first_cancels_it(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.prepare(app)
            app.record_idle_activity(user_activity=True)
            await self.start_session(app, pilot)
            app.live.send_greeting.assert_not_awaited()

    async def test_unknown_last_talk_skips_rather_than_introducing_herself(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.prepare(app, loaded=False)
            await self.start_session(app, pilot)
            self.prepare(app)
            await self.start_session(app, pilot)
            app.live.send_greeting.assert_not_awaited()

    async def test_send_failure_is_redacted(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.prepare(app)
            app.live.send_greeting.side_effect = RuntimeError("private details")
            await self.start_session(app, pilot)
            activity = "\n".join(app.activity.entries)
            self.assertIn("Greeting send failed (RuntimeError)", activity)
            self.assertNotIn("private details", activity)


CHAT_STATUS = SimpleNamespace(button=SimpleNamespace(id="chat-status"))


class WelcomeBackTests(GreetingAppCase):
    def prepare_resume(self, app, *, accepted=True):
        self.prepare(app)
        app.pending_greeting = None  # the launch greeting is already used
        app.standby = True
        app.request_live_transition = MagicMock(
            side_effect=lambda *args, **kwargs: setattr(app, "reconnecting", accepted),
        )

    async def start_after(self, app, pilot):
        app.reconnecting = False
        await self.start_session(app, pilot)

    async def test_clicking_resume_welcomes_back_once(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.prepare_resume(app)
            await app.on_button_pressed(CHAT_STATUS)
            await self.start_after(app, pilot)
            await self.start_session(app, pilot)
            app.live.send_welcome_back.assert_awaited_once()
            app.live.send_greeting.assert_not_awaited()
            self.assertIn("Welcome back sent", "\n".join(app.activity.entries))

    async def test_ctrl_r_wake_and_a_refused_click_do_not_welcome_back(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.prepare_resume(app)
            app.action_reconnect()  # Ctrl+R
            await self.start_after(app, pilot)
            app.request_wake_connection()
            await self.start_after(app, pilot)
            self.prepare_resume(app, accepted=False)  # e.g. still busy
            await app.on_button_pressed(CHAT_STATUS)
            await self.start_after(app, pilot)
            app.live.send_welcome_back.assert_not_awaited()
            app.live.send_greeting.assert_not_awaited()

    async def test_typing_before_the_session_starts_cancels_it(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.prepare_resume(app)
            await app.on_button_pressed(CHAT_STATUS)
            app.record_idle_activity(user_activity=True)
            app.reconnecting = False
            await self.start_session(app, pilot)
            app.live.send_welcome_back.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
