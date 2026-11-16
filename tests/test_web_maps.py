"""Maps on the web page (WB7): Google's embed on the page, a Google Maps link elsewhere, a key in Settings."""

import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from backend_chat import SIGNAL_EXCLUDED, WEB_EXCLUDED
from tools.pass_on import CLIENT_ONLY
from tools.show_map import NOT_SET_UP_RESULT, SHOWN_RESULT, ShowMapTool, map_view
from web import maps
from web.feed import MAP
from web.maps import MapView
import test_keyboard_persistence as keyboard_tests

KEY = "AIza" + "x" * 35


def query(url):
    return {name: values[0] for name, values in parse_qs(urlsplit(url).query).items()}


class MapViewTests(unittest.TestCase):
    def test_a_route_embeds_with_the_key_and_links_without_it(self):
        route = MapView("directions", "Fremantle, WA", "Leederville, Perth", "transit")
        self.assertEqual(route.caption(), "Route from Leederville, Perth to Fremantle, WA (transit)")
        embed = route.embed_url(KEY)
        self.assertTrue(embed.startswith("https://www.google.com/maps/embed/v1/directions?"))
        self.assertEqual(query(embed), {"key": KEY, "origin": "Leederville, Perth", "destination": "Fremantle, WA",
                                        "mode": "transit"})
        link = route.link()
        self.assertTrue(link.startswith("https://www.google.com/maps/dir/?"))
        self.assertNotIn(KEY, link)
        self.assertEqual(query(link)["travelmode"], "transit")

    def test_a_place_and_a_search_use_the_query(self):
        search = MapView("search", "cafes near Leederville & Oxford St")
        self.assertEqual(query(search.embed_url(KEY))["q"], "cafes near Leederville & Oxford St")
        self.assertTrue(search.embed_url(KEY).startswith("https://www.google.com/maps/embed/v1/search?"))
        self.assertEqual(query(MapView("place", "Kings Park").link())["query"], "Kings Park")
        self.assertEqual(MapView("place", "Kings Park").caption(), "Kings Park")

    def test_her_arguments_are_checked(self):
        view = map_view({"mode": "directions", "place": " Fremantle ", "origin": "Perth", "travel_mode": None})
        self.assertEqual(view, MapView("directions", "Fremantle", "Perth", "driving"))
        self.assertEqual(map_view({"mode": "place", "place": "Kings Park", "origin": None, "travel_mode": None}),
                         MapView("place", "Kings Park"))
        for bad in (
            {"mode": "directions", "place": "Fremantle", "origin": None, "travel_mode": None},
            {"mode": "street", "place": "x", "origin": None, "travel_mode": None},
            {"mode": "place", "place": " ", "origin": None, "travel_mode": None},
            {"mode": "place", "place": "x", "origin": None, "travel_mode": "flying"},
            {"mode": "place", "place": "x"},
        ):
            with self.assertRaises(ValueError):
                map_view(bad)


class MapsKeyTests(unittest.TestCase):
    def test_only_a_google_key_is_saved_and_empty_forgets_it(self):
        with patch.object(maps.keyring, "set_password") as saved, \
                patch.object(maps.keyring, "delete_password") as deleted:
            self.assertEqual(maps.save_maps_key(f"  {KEY} "), KEY)
            saved.assert_called_once_with("cora", maps.KEY_ACCOUNT, KEY)
            with self.assertRaises(ValueError):
                maps.save_maps_key("not a key!")
            self.assertEqual(maps.save_maps_key(""), "")
            deleted.assert_called_once_with("cora", maps.KEY_ACCOUNT)


class ShowMapToolTests(unittest.IsolatedAsyncioTestCase):
    async def run_tool(self, shown):
        connection = MagicMock()
        connection.response.item.create = AsyncMock()
        connection.response.create = AsyncMock()
        show = MagicMock(return_value=shown)
        tool = ShowMapTool(lambda: connection, lambda: False, show, MagicMock())
        await tool.handle("call-1", {"mode": "place", "place": "Kings Park", "origin": None, "travel_mode": None})
        show.assert_called_once_with(MapView("place", "Kings Park"))
        connection.response.create.assert_awaited_once()
        return json.loads(connection.response.item.create.await_args.kwargs["item"]["output"])

    async def test_she_learns_whether_it_was_shown(self):
        self.assertEqual(await self.run_tool(True), SHOWN_RESULT)
        self.assertEqual(await self.run_tool(False), NOT_SET_UP_RESULT)

    def test_signal_never_offers_it_and_the_web_page_does(self):
        self.assertIn("show_map", SIGNAL_EXCLUDED)
        self.assertNotIn("show_map", WEB_EXCLUDED)


class ShowMapAppTests(unittest.IsolatedAsyncioTestCase):
    async def test_without_a_key_its_not_offered_and_with_one_the_page_gets_the_map(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.maps_key = ""
            self.assertEqual(app.unavailable_tools() - {"call_someone", "claude_code", "run_claude_code"}, frozenset({"show_map"}))
            self.assertNotIn("show_map", [schema["name"] for schema in app.live.tool_schemas()])
            self.assertFalse(app.show_map(MapView("place", "Kings Park")))

            app.maps_key = KEY
            self.assertEqual(app.unavailable_tools() - {"call_someone", "claude_code", "run_claude_code"}, frozenset())
            self.assertIn("show_map", [schema["name"] for schema in app.tools.schemas(excluding=CLIENT_ONLY)])
            app.conversation.record_message = MagicMock()
            route = MapView("directions", "Fremantle", "Leederville", "driving")
            self.assertTrue(app.show_map(route))
            item = app.web_feed.items[-1]
            self.assertEqual((item.kind, item.role), (MAP, "assistant"))
            self.assertEqual(item.data, {"caption": route.caption(), "embed": route.embed_url(KEY), "link": route.link()})
            # The desk shows the caption and link; history keeps it as text, without the key.
            self.assertIn("Cora · map: Route from Leederville to Fremantle (driving)", app.conversation.log_entries)
            app.conversation.record_message.assert_called_once_with(
                "assistant", f"[map] {route.caption()} ({route.link()})", "screen")

    async def test_saving_a_key_turns_maps_on_and_reconnects_her_when_settings_close(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.maps_key = ""
            app.service_tools_changed = False
            with patch.object(maps, "save_maps_key", return_value=KEY):
                await app.save_maps_key(KEY).wait()
            self.assertEqual(app.maps_key, KEY)
            self.assertTrue(app.service_tools_changed)


if __name__ == "__main__":
    unittest.main()
