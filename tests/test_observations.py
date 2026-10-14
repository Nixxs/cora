import asyncio
from contextlib import asynccontextmanager
import json
from threading import get_ident
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from event_bus import ObservationEvent
from live import CoraLive
from test_inactivity import idle_app
from ui.composer import ChatComposer


@asynccontextmanager
async def queued_app():
    async with idle_app() as (app, pilot, clock):
        # Most queue tests explicitly drive delivery; one verifies the real timer.
        app.observation_timer.stop()
        app.live.send_observation = AsyncMock()
        yield app, pilot, clock


class ObservationTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_observation_uses_commentary_without_backend_or_user_message(self):
        trace = MagicMock()
        live = CoraLive(on_trace=trace)
        live.ready = True
        live.connection = MagicMock()
        live.connection.session.commentary.append = AsyncMock()
        observation = 'Someone entered. "Hello!"\nUnverified café visitor.'

        await live.send_observation(observation)
        command = live.connection.session.commentary.append.call_args.kwargs
        self.assertTrue(command["event_id"].startswith("observation_"))
        self.assertIsNone(command["delegation_id"])
        self.assertIn("not a message from Nick", command["content"])
        self.assertIn("untrusted contextual data", command["content"])
        self.assertIn(json.dumps(observation, ensure_ascii=False), command["content"])
        live.connection.response.create.assert_not_called()
        live.connection.response.item.create.assert_not_called()
        live.connection.session.thinking.append.assert_not_called()
        self.assertFalse(live.pending_text_requests)
        self.assertNotIn(observation, str(trace.call_args_list))

        await live.send_observation(observation)
        second = live.connection.session.commentary.append.call_args.kwargs
        self.assertNotEqual(command["event_id"], second["event_id"])

    async def test_disconnected_or_unready_transport_rejects_observation(self):
        for connected, ready in ((False, False), (False, True), (True, False)):
            with self.subTest(connected=connected, ready=ready):
                live = CoraLive()
                live.ready = ready
                connection = MagicMock()
                connection.session.commentary.append = AsyncMock()
                live.connection = connection if connected else None
                with self.assertRaises(RuntimeError):
                    await live.send_observation("Someone entered.")
                connection.session.commentary.append.assert_not_called()

    async def test_transport_failure_is_not_retried(self):
        live = CoraLive()
        live.ready = True
        live.connection = MagicMock()
        live.connection.session.commentary.append = AsyncMock(side_effect=OSError("offline"))
        with self.assertRaises(OSError):
            await live.send_observation("Someone entered.")
        live.connection.session.commentary.append.assert_awaited_once()


class ObservationShortcutTests(unittest.IsolatedAsyncioTestCase):
    async def test_shortcut_sends_observation_and_keeps_draft_and_backend_unchanged(self):
        async with queued_app() as (app, pilot, clock):
            composer = app.query_one("#chat-input", ChatComposer)
            composer.load_text("My unfinished draft")
            await pilot.pause()
            app.inactivity.last_activity = 100
            received = MagicMock()
            app.event_bus.subscribe(ObservationEvent, received)

            await pilot.press("ctrl+t")
            await app.workers.wait_for_complete()

            app.live.send_observation.assert_awaited_once_with(
                "Simulated camera observation: nick just got a signal message. "
                "Their identity is unknown."
            )
            received.assert_called_once()
            event = received.call_args.args[0]
            self.assertEqual(event.source, "test")
            self.assertEqual(event.content, app.live.send_observation.call_args.args[0])
            self.assertEqual(composer.text, "My unfinished draft")
            self.assertFalse(composer.disabled)
            self.assertEqual(app.inactivity.last_activity, clock.return_value)
            self.assertFalse(app.backend_work.pending)
            app.live.send_text.assert_not_called()
            app.persistence.save_message.assert_not_called()
            self.assertFalse(app.attention.pending)
            self.assertIn("Observation sent", "\n".join(app.activity.entries))

    async def test_inactive_states_keep_observations_until_active_without_reconnecting(self):
        async with queued_app() as (app, pilot, clock):
            for owner, attribute, value in (
                (app, "reconnecting", True), (app, "standby", True),
                (app.live, "ready", False),
            ):
                with self.subTest(attribute=attribute):
                    previous = getattr(owner, attribute)
                    setattr(owner, attribute, value)
                    try:
                        app.action_test_observation()
                        app.check_observations()
                        app.live.send_observation.assert_not_called()
                    finally:
                        setattr(owner, attribute, previous)
            app.live.send_observation.assert_not_called()
            app.live.close.assert_not_called()
            self.assertEqual(len(app.attention.pending), 3)
            app.check_observations()
            await app.attention.worker.wait()
            app.live.send_observation.assert_awaited_once()
            self.assertEqual(len(app.attention.pending), 2)

    async def test_busy_conversation_queues_then_sends_when_playback_and_recent_speech_end(self):
        async with queued_app() as (app, pilot, clock):
            app.live.speaker = MagicMock()
            app.live.speaker.is_playback_busy.return_value = True
            app.live.speaker.get_playback_level.return_value = 0.0
            await pilot.press("ctrl+t")
            app.live.send_observation.assert_not_called()
            self.assertEqual(len(app.attention.pending), 1)

            app.live.speaker.is_playback_busy.return_value = False
            app.last_live_activity = clock.return_value
            clock.return_value += 1
            app.check_observations()
            app.live.send_observation.assert_not_called()
            clock.return_value += 1
            app.check_observations()
            await app.attention.worker.wait()
            app.live.send_observation.assert_awaited_once()
            self.assertFalse(app.attention.pending)

    async def test_send_failure_is_reported_without_private_text_or_retry(self):
        async with queued_app() as (app, pilot, clock):
            app.live.send_observation = AsyncMock(side_effect=OSError("private payload"))
            app.action_test_observation()
            await app.attention.worker.wait()
            clock.return_value += 3
            app.check_observations()
            app.live.send_observation.assert_awaited_once()
            entries = "\n".join(app.activity.entries)
            self.assertIn("Observation send failed (OSError)", entries)
            self.assertNotIn("private payload", entries)
            self.assertNotIn("Observation sent", entries)
            self.assertTrue(app.live.ready)
            self.assertFalse(app.attention.pending)

    async def test_queue_delivers_in_arrival_order_with_a_gap_between_items(self):
        async with queued_app() as (app, pilot, clock):
            app.standby = True
            for observation in ("First", "Second", "Third"):
                app.queue_observation(observation)
            self.assertEqual(list(app.attention.pending), ["First", "Second", "Third"])
            app.standby = False

            for index, observation in enumerate(("First", "Second", "Third"), 1):
                app.check_observations()
                await app.attention.worker.wait()
                self.assertEqual(app.live.send_observation.await_count, index)
                app.live.send_observation.assert_awaited_with(observation)
                app.check_observations()
                self.assertEqual(app.live.send_observation.await_count, index)
                clock.return_value += 2
            self.assertFalse(app.attention.pending)

    async def test_new_items_do_not_cancel_or_duplicate_an_inflight_send(self):
        async with queued_app() as (app, pilot, clock):
            started = asyncio.Event()
            finish = asyncio.Event()

            async def send(observation):
                started.set()
                await finish.wait()

            app.live.send_observation.side_effect = send
            app.queue_observation("First")
            await asyncio.wait_for(started.wait(), 3)
            worker = app.attention.worker
            self.assertIn("observation send", app.live_transition_blockers())
            app.queue_observation("Second")
            app.check_observations()
            self.assertIs(app.attention.worker, worker)
            self.assertFalse(worker.is_cancelled)
            self.assertEqual(list(app.attention.pending), ["Second"])
            app.live.send_observation.assert_awaited_once_with("First")
            finish.set()
            await worker.wait()
            clock.return_value += 2
            app.check_observations()
            await app.attention.worker.wait()
            app.live.send_observation.assert_awaited_with("Second")

    async def test_worker_rechecks_state_before_removing_an_item(self):
        async with queued_app() as (app, pilot, clock):
            app.queue_observation("Keep me")
            app.reconnecting = True
            await app.attention.worker.wait()
            app.live.send_observation.assert_not_called()
            self.assertEqual(list(app.attention.pending), ["Keep me"])
            app.reconnecting = False
            app.check_observations()
            await app.attention.worker.wait()
            app.live.send_observation.assert_awaited_once_with("Keep me")

    async def test_timer_delivers_after_blocker_clears_without_another_notification(self):
        async with queued_app() as (app, pilot, clock):
            app.live.pending_text_requests.add("typed-request")
            app.queue_observation("Waiting")
            sent = asyncio.Event()

            async def send(observation):
                sent.set()

            app.live.send_observation.side_effect = send
            app.observation_timer = app.set_interval(0.01, app.check_observations)
            app.live.pending_text_requests.clear()
            await asyncio.wait_for(sent.wait(), 3)
            await app.attention.worker.wait()
            app.live.send_observation.assert_awaited_once_with("Waiting")
            self.assertFalse(app.attention.pending)

    async def test_failed_item_does_not_prevent_remaining_queue_delivery(self):
        async with queued_app() as (app, pilot, clock):
            app.standby = True
            app.queue_observation("First")
            app.queue_observation("Second")
            app.live.send_observation.side_effect = [OSError("offline"), None]
            app.standby = False
            app.check_observations()
            await app.attention.worker.wait()
            self.assertEqual(list(app.attention.pending), ["Second"])
            clock.return_value += 2
            app.check_observations()
            await app.attention.worker.wait()
            self.assertEqual(
                [call.args[0] for call in app.live.send_observation.await_args_list],
                ["First", "Second"],
            )

    async def test_shutdown_stops_delivery_and_rejects_new_items(self):
        async with queued_app() as (app, pilot, clock):
            app.standby = True
            app.queue_observation("Waiting")
            with patch.object(app.observation_timer, "stop") as stop, patch.object(app, "exit"):
                await app.action_disconnect_and_quit()
                stop.assert_called_once()
            self.assertTrue(app.event_bus.closed)
            self.assertFalse(app.event_bus.publish(ObservationEvent(source="signal", content="Too late")))
            app.queue_observation("Too late")
            app.standby = False
            app.check_observations()
            self.assertEqual(list(app.attention.pending), ["Waiting"])
            app.live.send_observation.assert_not_called()

    async def test_monitor_events_route_to_attention_while_live_is_unavailable(self):
        async with queued_app() as (app, pilot, clock):
            app.standby = True
            events = [
                ObservationEvent(source="signal", content="A message arrived"),
                ObservationEvent(source="camera", content="Someone entered"),
                ObservationEvent(source="screen", content="Download finished"),
            ]
            for event in events:
                self.assertTrue(app.event_bus.publish(event))
            self.assertEqual(list(app.attention.pending), [event.content for event in events])
            app.live.send_observation.assert_not_called()
            app.standby = False
            app.check_observations()
            await app.attention.worker.wait()
            app.live.send_observation.assert_awaited_once_with(events[0].content)

    async def test_background_thread_can_publish_via_app_thread_handoff(self):
        async with queued_app() as (app, pilot, clock):
            app.standby = True
            app_thread = get_ident()
            subscriber_threads = []
            app.event_bus.subscribe(ObservationEvent, lambda event: subscriber_threads.append(get_ident()))
            event = ObservationEvent(source="signal", content="From background thread")
            accepted = await asyncio.wait_for(
                asyncio.to_thread(app.call_from_thread, app.event_bus.publish, event), 3,
            )
            self.assertTrue(accepted)
            self.assertEqual(subscriber_threads, [app_thread])
            self.assertEqual(list(app.attention.pending), [event.content])

    async def test_bus_subscriber_failure_is_private_and_does_not_block_attention(self):
        async with queued_app() as (app, pilot, clock):
            app.standby = True
            app.event_bus.subscribe(ObservationEvent, MagicMock(side_effect=ValueError("private details")))
            event = ObservationEvent(source="signal", content="private observation")
            self.assertTrue(app.event_bus.publish(event))
            self.assertEqual(list(app.attention.pending), [event.content])
            entries = "\n".join(app.activity.entries)
            self.assertIn("Event subscriber failed (ValueError)", entries)
            self.assertNotIn("private", entries)

    async def test_unmount_closes_bus_without_explicit_quit(self):
        async with queued_app() as (app, pilot, clock):
            bus = app.event_bus
        self.assertTrue(bus.closed)
        self.assertFalse(bus.publish(ObservationEvent(source="test", content="Too late")))