"""Where you are (WB6): asked of the open page on demand, named by OpenStreetMap, held in memory only."""

import asyncio
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx2 as httpx

from backend_chat import SIGNAL_EXCLUDED, STANDBY_EXCLUDED, WEB_EXCLUDED
from server.listener import Listener, build_app
from storage.web_device_store import WebDevice
from tools.where_is_person import WHY_NOT, WhereIsPersonTool, validate_arguments
from web.feed import NOTICE, MessageFeed
from web.location import (
    DENIED, NO_PAGE, OFF, UNAVAILABLE, UNNAMED, Whereabouts, name_place, place_from, valid_fix, zoom_for,
)
from web.pairing import Pairing, token_hash
from web.routes import COOKIE, WebAccess, web_routes
import test_keyboard_persistence as keyboard_tests

NOW = datetime(2026, 11, 6, 9, tzinfo=timezone.utc)
IPHONE = WebDevice(uuid4(), "iPhone", NOW, NOW)
TOKEN = "a-paired-devices-token"
FIX = {"latitude": -31.936, "longitude": 115.84, "accuracy": 25}
FOUND = {"address": {"road": "Oxford Street", "house_number": "12", "suburb": "Leederville", "city": "Perth",
                     "state": "Western Australia", "country": "Australia"}}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class PlaceNameTests(unittest.TestCase):
    def test_a_street_is_named_only_when_the_fix_is_close_and_never_a_house_number(self):
        self.assertEqual(place_from(FOUND, zoom_for(25)),
                         "Leederville, near Oxford Street, Perth, Western Australia, Australia")
        self.assertEqual(place_from(FOUND, zoom_for(800)), "Leederville, Perth, Western Australia, Australia")
        self.assertEqual(zoom_for(5000), 10)
        self.assertEqual(place_from({"display_name": "Somewhere"}, 10), "Somewhere")

    def test_only_real_positions_are_taken(self):
        self.assertEqual(valid_fix(FIX), (-31.936, 115.84, 25.0))
        for bad in ({"latitude": 91, "longitude": 0, "accuracy": 5}, {"latitude": "x", "longitude": 0, "accuracy": 5},
                    {"latitude": float("nan"), "longitude": 0, "accuracy": 5}, {"latitude": 0, "longitude": 0}):
            self.assertIsNone(valid_fix(bad))


class GeocoderTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_geocoder_is_asked_politely_and_its_answer_named(self):
        asked = []

        def handler(request):
            asked.append(request)
            return httpx.Response(200, json=FOUND)

        def client(**options):
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), **options)

        place = await name_place(-31.936, 115.84, 25, client_factory=client)
        self.assertEqual(place, "Leederville, near Oxford Street, Perth, Western Australia, Australia")
        self.assertEqual(asked[0].url.host, "nominatim.openstreetmap.org")
        self.assertEqual(asked[0].url.params["zoom"], "17")
        self.assertTrue(asked[0].headers["user-agent"].startswith("Cora"))

    async def test_no_place_there_raises(self):
        def client(**options):
            return httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"error": "Unable to geocode"})), **options)

        with self.assertRaises(ValueError):
            await name_place(0, 0, 25, client_factory=client)


class WhereaboutsTests(unittest.IsolatedAsyncioTestCase):
    def make(self, pages=1, **options):
        self.asked = []

        def ask_pages(request_id):
            self.asked.append(request_id)
            return pages

        self.clock = Clock()
        self.name = AsyncMock(return_value="Leederville, Perth")
        return Whereabouts(ask_pages, name=self.name, clock=self.clock, **options)

    async def answered(self, whereabouts, *answers, refresh=False):
        """Check, with the pages giving these answers (device, body without the id)."""
        checking = asyncio.ensure_future(whereabouts.check(refresh))
        await asyncio.sleep(0)
        for device, body in answers:
            whereabouts.answer(device, {"id": self.asked[-1], **body})
        return await checking

    async def test_no_open_page_is_said_at_once(self):
        whereabouts = self.make(pages=0)
        self.assertEqual(await whereabouts.check(), (None, NO_PAGE, False))

    async def test_the_first_position_is_named_held_and_reused_until_it_is_old(self):
        whereabouts = self.make(pages=2)
        where, why_not, fresh = await self.answered(whereabouts, ("iPhone", FIX))
        self.assertIsNone(why_not)
        self.assertTrue(fresh)
        self.assertEqual((where.place, where.accuracy, where.device), ("Leederville, Perth", 25, "iPhone"))
        self.name.assert_awaited_once_with(-31.936, 115.84, 25.0)
        # Reused within ten minutes; the page isn't asked again.
        self.clock.now += 9 * 60
        self.assertEqual(await whereabouts.check(), (where, None, False))
        self.assertEqual(len(self.asked), 1)
        # Asked again once it's old, or when she asks for a fresh one.
        self.clock.now += 2 * 60
        await self.answered(whereabouts, ("iPad", FIX))
        self.assertEqual(len(self.asked), 2)
        await self.answered(whereabouts, ("iPad", FIX), refresh=True)
        self.assertEqual(len(self.asked), 3)
        whereabouts.forget()
        self.assertIsNone(whereabouts.recent())

    async def test_when_every_page_declines_the_most_useful_reason_is_given(self):
        whereabouts = self.make(pages=2)
        result = await self.answered(whereabouts, ("iPhone", {"error": OFF}), ("iPad", {"error": DENIED}))
        self.assertEqual(result, (None, DENIED, False))
        # One page off, the other not answering (in the background): no place in time.
        whereabouts = self.make(pages=2, answer_seconds=0.05)
        result = await self.answered(whereabouts, ("iPhone", {"error": OFF}))
        self.assertEqual(result, (None, UNAVAILABLE, False))

    async def test_a_place_that_cant_be_named_isnt_held(self):
        whereabouts = self.make()
        self.name.side_effect = ValueError
        self.assertEqual(await self.answered(whereabouts, ("iPhone", FIX)), (None, UNNAMED, False))
        self.assertIsNone(whereabouts.recent())

    async def test_answers_to_nothing_or_without_a_position_are_refused(self):
        whereabouts = self.make()
        self.assertFalse(whereabouts.answer("iPhone", {"id": "not-asked", **FIX}))
        checking = asyncio.ensure_future(whereabouts.check())
        await asyncio.sleep(0)
        self.assertFalse(whereabouts.answer("iPhone", {"id": self.asked[-1], "latitude": 200}))
        self.assertTrue(whereabouts.answer("iPhone", {"id": self.asked[-1], **FIX}))
        self.assertEqual((await checking)[0].device, "iPhone")


class LocationRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.feed = MessageFeed()
        self.answers = []

        async def find_device(hashed):
            return IPHONE if hashed == token_hash(TOKEN) else None

        def answer_location(device, body):
            self.answers.append((device.name, body))
            return body.get("id") == "asked"

        access = WebAccess(Pairing(), AsyncMock(), find_device, feed=self.feed, feed_ready=AsyncMock(),
                           answer_location=answer_location)
        self.server = Listener(build_app(web_routes(access)), 0)
        self.server.open()
        port = self.server.socket.getsockname()[1]
        self.serving = asyncio.create_task(self.server.serve())
        for _ in range(50):
            if self.server.server is not None and self.server.server.started:
                break
            await asyncio.sleep(0.05)
        self.client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")
        self.paired = {"Cookie": f"{COOKIE}={TOKEN}"}

    async def asyncTearDown(self):
        await self.client.aclose()
        self.server.stop()
        await asyncio.wait_for(self.serving, 5)

    async def test_only_a_paired_page_answers_and_only_a_waiting_request(self):
        self.assertEqual((await self.client.post("/api/location", json={"id": "asked", **FIX})).status_code, 401)
        answer = await self.client.post("/api/location", json={"id": "asked", **FIX}, headers=self.paired)
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(answer.headers["cache-control"], "no-store")
        self.assertEqual(self.answers[0], ("iPhone", {"id": "asked", **FIX}))
        late = await self.client.post("/api/location", json={"id": "old", "error": "off"}, headers=self.paired)
        self.assertEqual(late.status_code, 409)
        self.assertEqual((await self.client.post("/api/location", content=b"x", headers=self.paired)).status_code, 400)

    async def test_open_pages_are_asked_over_the_feed_and_notices_reach_them(self):
        async def ask():
            while not self.feed.subscribers:
                await asyncio.sleep(0.01)
            self.assertEqual(self.feed.ask_location("abc123"), 1)
            self.feed.publish_notice("Cora checked where you are.")

        asking = asyncio.create_task(ask())
        events, current = [], {}
        async with self.client.stream("GET", "/api/events", headers=self.paired) as response:
            async for text in response.aiter_lines():
                if not text:
                    if "event" in current:
                        events.append(current)
                    current = {}
                    if len(events) == 3:
                        break
                    continue
                field, _, value = text.partition(": ")
                current[field] = value
        await asking
        self.assertEqual([event["event"] for event in events], ["reset", "locate", "notice"])
        # The request isn't part of the conversation: no id, and not kept for pages that connect later.
        self.assertNotIn("id", events[1])
        self.assertEqual(json.loads(events[1]["data"]), {"id": "abc123"})
        self.assertEqual(json.loads(events[2]["data"])["text"], "Cora checked where you are.")
        self.assertEqual([item.kind for item in self.feed.items], [NOTICE])


class WhereIsPersonToolTests(unittest.IsolatedAsyncioTestCase):
    def test_only_the_persons_own_chats_offer_it(self):
        self.assertIn("where_is_person", SIGNAL_EXCLUDED)
        self.assertNotIn("where_is_person", WEB_EXCLUDED)
        self.assertNotIn("where_is_person", STANDBY_EXCLUDED)
        from app_areas.mail import PERSON_EMAIL_EXCLUDED
        self.assertIn("where_is_person", PERSON_EMAIL_EXCLUDED)

    def test_refresh_is_its_only_argument(self):
        validate_arguments({"refresh": False})
        for bad in ({}, {"refresh": "yes"}, {"refresh": True, "where": "x"}):
            with self.assertRaises(ValueError):
                validate_arguments(bad)

    async def test_the_result_goes_back_and_she_continues(self):
        connection = MagicMock()
        connection.response.item.create = AsyncMock()
        connection.response.create = AsyncMock()
        locate = AsyncMock(return_value={"status": "ok", "place": "Leederville"})
        tool = WhereIsPersonTool(lambda: connection, lambda: False, locate, MagicMock())
        await tool.handle("call-1", {"refresh": True})
        locate.assert_awaited_once_with(True)
        sent = connection.response.item.create.await_args.kwargs["item"]
        self.assertEqual((sent["call_id"], json.loads(sent["output"])["place"]), ("call-1", "Leederville"))
        connection.response.create.assert_awaited_once()


class LocatePersonTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_check_shows_a_quiet_line_and_activity_never_has_the_place(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.assertEqual((await app.locate_person(False))["message"], WHY_NOT[NO_PAGE])

            def a_page_answers(request_id):
                asyncio.get_running_loop().call_soon(
                    lambda: app.whereabouts.answer("iPhone", {"id": request_id, **FIX}))
                return 1

            app.whereabouts.ask_pages = a_page_answers
            app.whereabouts.name = AsyncMock(return_value="Leederville, Perth")
            app.activity.write = MagicMock()
            result = await app.locate_person(False)
            self.assertEqual((result["status"], result["place"], result["from_device"]),
                             ("ok", "Leederville, Perth", "iPhone"))
            notice = app.web_feed.items[-1]
            self.assertEqual(notice.kind, NOTICE)
            self.assertIn("Leederville, Perth", notice.data["text"])
            self.assertIn("Leederville, Perth", str(app.conversation.log_entries[-1]))
            self.assertTrue(all("Leederville" not in str(call) for call in app.activity.write.call_args_list))
            # The quiet line isn't history for her, and nothing of it was saved.
            history = json.dumps(app.conversation.get_reconnect_history())
            self.assertNotIn("Leederville", history)
            # Reused the second time, still noted.
            again = await app.locate_person(False)
            self.assertEqual(again["checked_minutes_ago"], 0)
            self.assertIn("just now", app.web_feed.items[-1].data["text"])


if __name__ == "__main__":
    unittest.main()
