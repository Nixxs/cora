"""Her home, live: Sensibo's event stream, her room following it, and changes made elsewhere. Mocked HTTP only."""

import asyncio
from time import monotonic
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx2

from home.sensibo import BadKey, noticeable, parse_rooms, with_measurements
from home.stream import SensiboStream, SseParser
import test_home
from test_home import ROOMS, pod
import test_keyboard_persistence as keyboard_tests


class SseTests(unittest.TestCase):
    def test_events_comments_and_multi_line_data(self):
        parser = SseParser()
        lines = [": heartbeat", "event: hello", 'data: {"heartbeatSeconds": 120}', "", "", "event: changed",
                 'data: {"id": "a1",', 'data:  "fields": []}', ""]
        events = [event for line in lines if (event := parser.feed(line)) is not None]
        self.assertEqual(events, [("hello", '{"heartbeatSeconds": 120}'), ("changed", '{"id": "a1",\n "fields": []}')])


class StreamTests(unittest.IsolatedAsyncioTestCase):
    def stream(self, *answers):
        self.events, self.problems, self.slept, self.requests = [], [], [], []
        answers = list(answers)

        def handle(request):
            self.requests.append(request)
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        async def on_event(name, data):
            self.events.append((name, data))

        async def sleep(seconds):
            self.slept.append(seconds)
            if len(self.slept) >= 3:
                raise asyncio.CancelledError

        return SensiboStream("k" * 30, on_event, on_problem=self.problems.append,
                             transport=httpx2.MockTransport(handle), sleep=sleep, jitter=lambda: 0.0)

    async def test_events_reach_the_app_and_the_key_stays_out_of_the_address(self):
        body = (b'event: hello\ndata: {"heartbeatSeconds": 60, "pods": 2}\n\n: beat\n\n'
                b'event: measurements\ndata: {"id": "Jyt7jM8c", "measurements": {"temperature": 24.5}}\n\n'
                b'event: resync\ndata: {}\n\n')
        stream = self.stream(httpx2.Response(200, content=body))
        self.assertTrue(await stream.listen())
        self.assertEqual([name for name, _ in self.events], ["hello", "measurements", "resync"])
        self.assertEqual(self.events[1][1]["measurements"]["temperature"], 24.5)
        self.assertEqual(stream.heartbeat, 60.0)
        request = self.requests[0]
        self.assertEqual(request.url.path, "/api/v2/stream")
        self.assertEqual(request.headers["X-Api-Key"], "k" * 30)
        self.assertNotIn("apiKey", str(request.url))

    async def test_it_reconnects_after_a_growing_delay_honouring_retry_after(self):
        stream = self.stream(httpx2.Response(429, headers={"Retry-After": "40"}), httpx2.ConnectError("down"),
                             httpx2.Response(503))
        with self.assertRaises(asyncio.CancelledError):
            await stream.run()
        self.assertEqual(self.slept, [40.0, 6.0, 12.0])
        self.assertEqual(len(self.problems), 3)

    async def test_a_refused_key_ends_it(self):
        stream = self.stream(httpx2.Response(401))
        with self.assertRaises(BadKey):
            await stream.run()

    async def test_a_silent_connection_is_given_up_after_two_heartbeats(self):
        async def never():
            await asyncio.sleep(3600)
            yield b""

        stream = self.stream(httpx2.Response(200, content=never()))
        stream.heartbeat = -4.95  # two of them plus the 10 seconds' grace: a tenth of a second
        with self.assertRaises(asyncio.TimeoutError):
            await stream.listen()


class LiveRoomTests(unittest.TestCase):
    def test_a_note_needs_two_degrees_or_fifteen_points_and_fifteen_minutes(self):
        room = parse_rooms(ROOMS)[0]  # 20.8°, 75%
        self.assertFalse(noticeable((20.0, 75.0, 0.0), room, 3600.0))
        self.assertTrue(noticeable((18.8, 75.0, 0.0), room, 3600.0))
        self.assertTrue(noticeable((20.8, 55.0, 0.0), room, 3600.0))
        self.assertFalse(noticeable((18.0, 75.0, 0.0), room, 600.0))

    def test_new_readings_keep_the_rest_of_the_room(self):
        room = parse_rooms(ROOMS)[0]
        newer = with_measurements(room, {"temperature": 23.1, "time": {"time": "2026-11-20T13:00:00Z"}})
        self.assertEqual((newer.temperature, newer.humidity, newer.mode, newer.name), (23.1, 75.0, "cool", "Office"))
        self.assertEqual(newer.measured_at.hour, 13)
        self.assertIsNone(with_measurements(room, {"rssi": -40}))
        self.assertIsNone(with_measurements(room, None))


class StreamAppTests(unittest.IsolatedAsyncioTestCase):
    ready = test_home.AppTests.ready

    async def test_her_room_follows_the_stream_and_a_noticeable_change_is_noted_quietly(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.ready(app)
            app.live.note_her_room = AsyncMock(return_value=True)
            await app.update_her_room()
            self.assertIsNotNone(app.her_room_told)
            await app.on_home_event("measurements", {"id": "Jyt7jM8c", "measurements": {"temperature": 21.3}})
            self.assertIn("21.3°C", app.live.room_context)
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            app.live.note_her_room.assert_not_awaited()
            app.her_room_told = (20.8, 75.0, monotonic() - 3600)
            await app.on_home_event("measurements", {"id": "Jyt7jM8c", "measurements": {"temperature": 23.4}})
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            app.live.note_her_room.assert_awaited_once()
            self.assertIn("23.4°C", app.live.note_her_room.await_args.args[0])
            self.assertEqual(app.her_room_told[0], 23.4)
            # Another unit's readings aren't hers.
            await app.on_home_event("measurements", {"id": "BpP9b5Ka", "measurements": {"temperature": 30}})
            self.assertIn("23.4°C", app.live.room_context)

    async def test_switched_by_someone_else_is_news_but_her_own_change_isnt(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.ready(app)
            app.queue_observation = MagicMock()
            app.live.ready = True
            app.notice_aircon_changes(parse_rooms(ROOMS))
            app.queue_observation.assert_not_called()
            on = parse_rooms({"result": [pod(on=True, targetTemperature=24), pod("BpP9b5Ka", "Declan’s Room")]})
            app.notice_aircon_changes(on)
            observation = app.queue_observation.call_args.args[0]
            self.assertIn("Office's air conditioner was just switched on (cool, 24°C, fan auto), not by you", observation)
            self.assertEqual(app.queue_observation.call_args.kwargs["source"], "sensibo")
            app.queue_observation.reset_mock()
            app.aircon_own["Jyt7jM8c"] = monotonic()
            app.notice_aircon_changes(parse_rooms(ROOMS))
            app.queue_observation.assert_not_called()
            offline = parse_rooms({"result": [pod(), pod("BpP9b5Ka", "Declan’s Room", alive=False)]})
            app.notice_aircon_changes(offline)
            self.assertIn("gone offline", app.queue_observation.call_args.args[0])

    async def test_in_standby_a_change_goes_to_activity_only_never_queued_for_later(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.ready(app)
            app.queue_observation = MagicMock()
            app.live.ready = True
            app.notice_aircon_changes(parse_rooms(ROOMS))
            app.standby = True
            app.notice_aircon_changes(parse_rooms({"result": [pod(on=True), pod("BpP9b5Ka", "Declan’s Room")]}))
            app.queue_observation.assert_not_called()
            self.assertIn("Office's air conditioner was just switched on", "\n".join(app.activity.entries))

    async def test_news_of_a_change_rereads_once_it_settles(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.ready(app)
            with patch.object(app, "reread_home") as reread:
                await app.on_home_event("changed", {"id": "Jyt7jM8c", "fields": ["measurements"]})
                reread.assert_not_called()
                await app.on_home_event("changed", {"id": "Jyt7jM8c", "fields": ["acState"]})
                await app.on_home_event("changed", {"id": "Jyt7jM8c", "fields": []})
                await app.on_home_event("hello", {"heartbeatSeconds": 120})
                self.assertEqual([call.args[0] for call in reread.call_args_list], [3.0, 3.0, 0.0])

    async def test_the_stream_runs_only_while_sensibo_is_on_with_a_key(self):
        async with keyboard_tests.running_app() as (app, pilot):
            started = asyncio.Event()

            async def listen(stream):
                started.set()
                await asyncio.sleep(3600)

            with patch.object(SensiboStream, "run", listen):
                app.sync_home_stream()
                self.assertIsNone(app.home_stream_worker)
                self.ready(app)
                app.sync_home_stream()
                await asyncio.wait_for(started.wait(), 3)
                worker = app.home_stream_worker
                app.sync_home_stream()
                self.assertIs(app.home_stream_worker, worker)
                app.sensibo_enabled = False
                app.sync_home_stream()
                await pilot.pause()
                self.assertTrue(worker.is_cancelled)


if __name__ == "__main__":
    unittest.main()
