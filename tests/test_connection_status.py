import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from textual.widgets import Button

from app import CoraApp
from test_keyboard_persistence import running_app


class ConnectionStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_events_update_label_class_and_errors_do_not_fake_disconnect(self):
        async with running_app() as (app, pilot):
            status = app.query_one("#chat-status", Button)
            self.assertEqual(str(status.label), "○ Disconnected")
            self.assertTrue(status.has_class("state-disconnected"))
            self.assertTrue(status.disabled)

            app.handle_live_event(SimpleNamespace(type="session.started"))
            self.assertEqual(str(status.label), "● Active")
            self.assertTrue(status.has_class("state-active"))
            self.assertFalse(status.has_class("state-disconnected"))
            self.assertIn("enter standby", status.tooltip)
            self.assertFalse(status.disabled)

            app.handle_live_event(SimpleNamespace(
                type="error", error=SimpleNamespace(message="Rejected command", client_event_id=None),
            ))
            self.assertEqual(str(status.label), "● Active")
            self.assertEqual(app.activity.panel.title, "Activity · API error")
            await pilot.click("#chat-status")
            self.assertEqual(str(status.label), "● Active")
            app.live.close.assert_not_called()

            app.handle_live_event(SimpleNamespace(type="session.closed", reason="remote_hangup"))
            self.assertEqual(str(status.label), "○ Disconnected")
            self.assertTrue(status.has_class("state-disconnected"))
            self.assertFalse(status.has_class("state-active"))

    async def test_connection_worker_displays_connecting_until_session_starts(self):
        connect = CoraApp.connect_to_cora
        async with running_app() as (app, pilot):
            entered = asyncio.Event()
            release = asyncio.Event()

            async def run(on_event, *args):
                entered.set()
                await release.wait()
                on_event(SimpleNamespace(type="session.started"))
                self.assertEqual(str(app.query_one("#chat-status", Button).label), "● Active")
                on_event(SimpleNamespace(type="session.closed", reason="close_requested"))

            app.live.run = AsyncMock(side_effect=run)
            worker = connect(app)
            try:
                await asyncio.wait_for(entered.wait(), 2)
                status = app.query_one("#chat-status", Button)
                self.assertEqual(str(status.label), "◌ Connecting…")
                self.assertTrue(status.has_class("state-connecting"))
            finally:
                release.set()
                await asyncio.wait_for(worker.wait(), 3)
            self.assertEqual(str(status.label), "○ Disconnected")
            self.assertFalse(status.has_class("state-connecting"))

    async def test_failed_connection_remains_disconnected_with_diagnostic_tooltip(self):
        connect = CoraApp.connect_to_cora
        async with running_app() as (app, pilot):
            app.live.run = AsyncMock(side_effect=RuntimeError("Transport failed"))
            worker = connect(app)
            await asyncio.wait_for(worker.wait(), 3)
            status = app.query_one("#chat-status", Button)
            self.assertEqual(str(status.label), "○ Disconnected")
            self.assertEqual(status.tooltip, "Connection failed. See Activity for details.")
            self.assertEqual(app.activity.panel.title, "Activity · Connection failed")

    async def test_shutdown_displays_disconnecting_while_transport_closes(self):
        async with running_app() as (app, pilot):
            entered = asyncio.Event()
            release = asyncio.Event()

            async def close():
                entered.set()
                await release.wait()

            app.live.close = AsyncMock(side_effect=close)
            with patch.object(app, "exit"):
                shutdown = asyncio.create_task(app.action_disconnect_and_quit())
                try:
                    await asyncio.wait_for(entered.wait(), 2)
                    status = app.query_one("#chat-status", Button)
                    self.assertEqual(str(status.label), "◌ Disconnecting…")
                    self.assertTrue(status.has_class("state-disconnecting"))
                    self.assertTrue(status.disabled)
                finally:
                    release.set()
                    await asyncio.wait_for(shutdown, 3)

    async def test_status_and_audio_controls_share_one_line_and_keep_distinct_colours(self):
        async with running_app() as (app, pilot):
            await pilot.resize_terminal(55, 25)
            await pilot.pause()
            status = app.query_one("#chat-status", Button)
            mic = app.query_one("#mic-status", Button)
            speaker = app.query_one("#speaker-status", Button)
            disconnected_colour = status.styles.color
            app.set_connection_state("active")
            await pilot.pause()
            self.assertNotEqual(status.styles.color, disconnected_colour)
            self.assertEqual(status.region.height, 1)
            self.assertEqual(status.region.y, mic.region.y)
            self.assertEqual(status.region.y, speaker.region.y)
            self.assertLessEqual(speaker.region.right, app.size.width)

    async def test_rendered_state_is_left_aligned_and_microphone_labels_are_not_clipped(self):
        async with running_app() as (app, pilot):
            status = app.query_one("#chat-status", Button)
            mic = app.query_one("#mic-status", Button)
            app.set_connection_state("active")
            app.handle_mic_status(True)
            await pilot.pause()
            rendered = status.render_line(0).text
            with self.subTest("state alignment"):
                self.assertLessEqual(len(rendered) - len(rendered.lstrip()), 1)
            self.assertIn("Listening", mic.render_line(0).text)

            with patch.object(app, "record_chat_message"):
                app.handle_live_event(SimpleNamespace(type="session.input_transcript.delta", delta="Hello"))
                await pilot.pause()
                self.assertIn("Speech received", mic.render_line(0).text)
                app.reset_mic_indicator()
                await pilot.pause()
                self.assertIn("Listening", mic.render_line(0).text)
                app.conversation.flush_transcript()

            await pilot.click("#mic-status")
            self.assertIn("Muted", mic.render_line(0).text)
            # Textual suppresses another mouse press during its 0.2 s click effect.
            await pilot.pause(0.25)
            await pilot.click("#mic-status")
            app.handle_mic_status(True)
            await pilot.pause()
            self.assertIn("Listening", mic.render_line(0).text)


if __name__ == "__main__":
    unittest.main()