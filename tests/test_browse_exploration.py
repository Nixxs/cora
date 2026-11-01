"""Exploring by browsing: a fake browser and mocked model, plus the app's handover and the daily limit."""

import asyncio
from dataclasses import replace
from datetime import timedelta
import json
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from openai.types.responses import Response

from services.connection import CONNECTED, ServiceStatus, ServiceTool
from subconscious.interests.browse_exploration import (
    BROWSE_SECONDS, KEEP_PAGES, MAX_CALLS, MAX_PAGES, OUT_OF_ROOM, PAGE_DROPPED, PAGE_MAX_CHARS, RANDOM_PAGE,
    SEARCH_URL, TOOLS, BrowserAccess, browse, page_text, unwrapped,
)
from subconscious.interests.exploration_store import Exploration, Start
from subconscious.interests.explore_interest import (
    BROWSE_EXPLORE_INSTRUCTIONS, BROWSE_WANDER_INSTRUCTIONS, EXPLORE_SCHEMA, WANDER_SCHEMA, explore, explore_next,
    wander,
)
import test_exploration_store as exploration_tests
import test_explore_interest as explore_tests
import test_interest_store as interest_tests
import test_keyboard_persistence as keyboard_tests

NOW = interest_tests.NOW
PAGE = "https://en.wikipedia.org/wiki/Lighthouse"
LINKED = "https://en.wikipedia.org/wiki/Fresnel_lens"
PRIVATE = "http://192.168.1.1/admin"


def response(*output, status="completed"):
    return Response.model_validate({
        "id": "resp_browse", "object": "response", "created_at": 1, "model": "gpt-6-luna", "status": status,
        "parallel_tool_calls": False, "tool_choice": "auto", "tools": [], "output": list(output),
    })


def call(name, n=1, **arguments):
    return {"type": "function_call", "id": f"fc_{n}", "call_id": f"call_{n}", "name": name,
            "arguments": json.dumps(arguments), "status": "completed"}


def answer(payload):
    return {"id": "msg_1", "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": json.dumps(payload), "annotations": []}]}


class FakeBrowser:
    """Navigation lands where asked, except the redirects set up in a test."""

    def __init__(self, redirects=None, text="- paragraph [ref=e1] [cursor=pointer]: A lighthouse keeps ships off rocks."):
        self.redirects = redirects or {}
        self.text = text
        self.calls = []
        self.at = None
        self.pages = []

    def call(self, tool, arguments):
        self.calls.append((tool, arguments))
        if tool == "browser_navigate":
            self.at = self.redirects.get(arguments["url"], arguments["url"])
        elif tool == "browser_navigate_back":
            self.at = PAGE
        if tool == "browser_snapshot":
            return f"- Page URL: {self.at}\n{self.text}", False
        return "done", False

    def refuse(self, url):
        return "Pages on this PC or the local network can't be opened." if "192.168." in url else None

    def access(self, keep_going=lambda: True):
        return BrowserAccess(self.call, self.refuse, self.pages.append, keep_going)

    def navigated(self):
        return [arguments["url"] for tool, arguments in self.calls if tool == "browser_navigate"]


def run(client, browser, *, clock=lambda: 0.0, keep_going=lambda: True):
    return browse(client, "m", "instructions", {"interest": "Lighthouses"}, EXPLORE_SCHEMA, "cora_exploration",
                  browser.access(keep_going), clock=clock)


class BrowseTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.browser = FakeBrowser()

    def replies(self, *replies):
        self.client.responses.create.side_effect = list(replies)

    def requests(self):
        return [item.kwargs for item in self.client.responses.create.call_args_list]

    def test_she_searches_opens_and_follows_links_then_writes_up(self):
        self.replies(
            response(call("search", 1, query="lighthouse keepers")),
            response(call("open_page", 2, url=PAGE)),
            response(call("open_page", 3, url="/wiki/Fresnel_lens")),  # relative to the page she's on
            response(call("go_back", 4)),
            response(answer(explore_tests.FINDING)),
        )
        payload, opened, pages = run(self.client, self.browser)
        self.assertEqual(payload, explore_tests.FINDING)
        # The search results page is never a source; the pages she read are.
        self.assertEqual(opened, {PAGE, LINKED})
        self.assertEqual(pages, 4)
        self.assertEqual(self.browser.navigated(), [SEARCH_URL + "lighthouse+keepers", PAGE, LINKED])
        # The search results settle before she reads them.
        self.assertIn(("browser_wait_for", {"time": 2}), self.browser.calls)
        self.assertEqual(self.browser.pages, [PAGE, LINKED, PAGE])
        self.assertEqual(self.browser.calls[-1], ("browser_close", {}))
        first = self.requests()[0]
        self.assertEqual((first["tools"], first["tool_choice"], first["store"]), (TOOLS, "auto", False))
        self.assertEqual(first["text"]["format"]["schema"], EXPLORE_SCHEMA)
        # Only reading tools, all strict.
        self.assertEqual({tool["name"] for tool in TOOLS}, {"search", "open_page", "go_back"})
        page = self.requests()[-1]["input"][-1]["output"]
        self.assertIn("keeps ships off rocks.", page)
        self.assertNotIn("[ref=", page)
        self.assertNotIn("[cursor=", page)

    def test_long_pages_are_cut_and_older_pages_leave_the_context(self):
        self.browser.text = "- paragraph: " + "x" * (PAGE_MAX_CHARS + 500)
        self.replies(*(response(call("open_page", n, url=f"{PAGE}_{n}")) for n in range(5)),
                     response(answer(explore_tests.FINDING)))
        run(self.client, self.browser)
        outputs = [item["output"] for item in self.requests()[-1]["input"] if item.get("type") == "function_call_output"]
        self.assertEqual(outputs[:-KEEP_PAGES], [PAGE_DROPPED] * (5 - KEEP_PAGES))
        self.assertTrue(all("the rest of the page isn't shown" in output for output in outputs[-KEEP_PAGES:]))
        self.assertTrue(all(len(output) < PAGE_MAX_CHARS + 100 for output in outputs))

    def test_private_addresses_are_refused_before_and_after_a_redirect(self):
        self.browser.redirects = {"https://short.example/x": PRIVATE}
        self.replies(response(call("open_page", 1, url=PRIVATE)),
                     response(call("open_page", 2, url="https://short.example/x")),
                     response(call("open_page", 3, url="file:///etc/passwd")),
                     response(answer(explore_tests.FINDING)))
        self.browser.refuse = lambda url: None if url.startswith("https://short") else "Not allowed."
        _, opened, _ = run(self.client, self.browser)
        self.assertEqual(opened, set())
        self.assertEqual(self.browser.pages, [])
        # The first and third never reach the browser; the redirect is taken straight off the page.
        self.assertEqual(self.browser.navigated(), ["https://short.example/x", "about:blank"])
        outputs = [item["output"] for item in self.requests()[-1]["input"] if item.get("type") == "function_call_output"]
        self.assertEqual(outputs, ["Not allowed."] * 3)

    def test_after_the_page_limit_she_must_write_up(self):
        self.replies(*(response(call("open_page", n, url=f"{PAGE}_{n}")) for n in range(MAX_PAGES)),
                     response(answer(explore_tests.FINDING)))
        run(self.client, self.browser)
        requests = self.requests()
        self.assertEqual([request["tool_choice"] for request in requests], ["auto"] * MAX_PAGES + ["none"])
        self.assertEqual(requests[-1]["input"][-1]["content"][0]["text"], OUT_OF_ROOM)

    def test_after_the_call_limit_she_must_write_up(self):
        self.replies(*(response(call("go_back", n), call("go_back", n + 100)) for n in range(MAX_CALLS // 2 + 1)),
                     response(answer(explore_tests.FINDING)))
        self.browser.text = "- paragraph: short"
        with patch("subconscious.interests.browse_exploration.MAX_PAGES", 100):
            run(self.client, self.browser)
        self.assertEqual(self.requests()[-1]["tool_choice"], "none")
        self.assertEqual(len(self.requests()), MAX_CALLS // 2 + 2)

    def test_time_running_out_or_you_coming_back_ends_the_session(self):
        times = iter((0.0, 1.0, BROWSE_SECONDS + 1.0))
        self.replies(response(call("open_page", 1, url=PAGE)), response(answer(explore_tests.FINDING)))
        run(self.client, self.browser, clock=lambda: next(times))
        self.assertEqual([request["tool_choice"] for request in self.requests()], ["auto", "none"])

        self.client.reset_mock()
        self.replies(response(answer(explore_tests.FINDING)))
        run(self.client, self.browser, keep_going=lambda: False)
        self.assertEqual(self.requests()[0]["tool_choice"], "none")

    def test_misbehaving_output_fails_but_the_browser_still_closes(self):
        for replies in (
            [response(call("open_page", 1, url=PAGE), status="incomplete")],
            [response(answer({"finding": "only this"}))],
        ):
            with self.subTest(replies=replies):
                self.browser.calls.clear()
                self.replies(*replies)
                with self.assertRaises(ValueError):
                    run(self.client, self.browser)
                self.assertEqual(self.browser.calls[-1], ("browser_close", {}))
        # Calling a tool after being told to stop.
        self.browser.calls.clear()
        self.replies(response(call("open_page", 1, url=PAGE)))
        with self.assertRaisesRegex(ValueError, "past its limits"):
            run(self.client, self.browser, keep_going=lambda: False)
        self.assertEqual(self.browser.calls, [("browser_close", {})])


SNAPSHOT = """### Page
- Page URL: https://en.wikipedia.org/wiki/Lighthouse
- Page Title: Lighthouse - Wikipedia
### Snapshot
```yaml
- generic [ref=e1]:
  - link "Jump to content" [ref=e2]:
    - /url: "#bodyContent"
  - button "Main menu" [ref=e3]
  - heading "Lighthouse" [level=1] [ref=e4]
  - paragraph [ref=e5]:
    - text: A
    - link "lighthouse" [ref=e6] [cursor=pointer]:
      - /url: /wiki/Lighthouse
    - text: "keeps ships off rocks."
    - link "[1]" [ref=e7]:
      - /url: "#cite_note-1"
  - heading "History" [level=2] [ref=e8]
  - img "A lamp" [ref=e9]
  - listitem [ref=e10]:
    - link "Bing result" [ref=e11]:
      - /url: https://www.bing.com/ck/a?!&&p=abc&u=a1aHR0cHM6Ly9saWdodGhvdXNlY2FyZS5vcmcuYXUv&ntb=1
```"""


class PageTextTests(unittest.TestCase):
    def test_a_snapshot_reads_as_text_with_links_from_the_main_heading(self):
        self.assertEqual(page_text(SNAPSHOT), "\n".join([
            "Address: https://en.wikipedia.org/wiki/Lighthouse", "Title: Lighthouse - Wikipedia",
            "# Lighthouse", "A [lighthouse](/wiki/Lighthouse) keeps ships off rocks. [1]", "## History",
            "[Bing result](https://lighthousecare.org.au/)",
        ]))

    def test_only_readable_bing_redirects_are_unwrapped(self):
        self.assertEqual(unwrapped("https://www.bing.com/ck/a?u=a1aHR0cHM6Ly9leGFtcGxlLmNvbS8"), "https://example.com/")
        for address in ("https://www.bing.com/ck/a?u=a1L2ltYWdlcw",  # /images: not a web address
                        "https://www.bing.com/ck/a?u=zz", "https://www.bing.com/ck/a?u=a1!!!",
                        "https://example.com/ck/a?u=a1aHR0cHM6Ly9leGFtcGxlLmNvbS8"):
            with self.subTest(address=address):
                self.assertEqual(unwrapped(address), address)


class BrowsingExploreTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.browser = FakeBrowser()

    def test_an_interest_is_browsed_and_only_opened_pages_are_sources(self):
        self.client.responses.create.side_effect = [
            response(call("open_page", 1, url=PAGE)),
            response(answer({**explore_tests.FINDING, "sources": [PAGE, "https://invented.example/"]})),
        ]
        found = explore(self.client, "m", explore_tests.INTEREST, [], self.browser.access())
        self.assertEqual((found.sources, found.pages), ((PAGE,), 1))
        self.assertEqual(self.client.responses.create.call_args.kwargs["instructions"], BROWSE_EXPLORE_INSTRUCTIONS)
        for phrase in ("search(query)", "open_page(url)", "go_back()", f"at most {MAX_PAGES} pages",
                       "no logins, forms, purchases or downloads", "never a search results page"):
            self.assertIn(phrase, BROWSE_EXPLORE_INSTRUCTIONS)
        self.assertNotIn("web search, at most", BROWSE_EXPLORE_INSTRUCTIONS)

    def test_a_random_wander_starts_from_a_random_page(self):
        self.client.responses.create.side_effect = [response(answer(explore_tests.WANDERED))]
        wander(self.client, "m", explore_tests.RANDOM, self.browser.access())
        request = self.client.responses.create.call_args.kwargs
        self.assertEqual(request["instructions"], BROWSE_WANDER_INSTRUCTIONS)
        self.assertEqual(request["text"]["format"]["schema"], WANDER_SCHEMA)
        sent = json.loads(request["input"][0]["content"][0]["text"])
        self.assertEqual(sent["start_url"], RANDOM_PAGE)
        self.assertNotIn("area", sent)
        self.assertIn("start_url", BROWSE_WANDER_INSTRUCTIONS)

    def explore_next(self, browsed):
        store = MagicMock()
        attempt = Exploration(uuid4(), explore_tests.INTEREST.id, "running", NOW, None, None, (), (), browsed=browsed)
        store.start_next.return_value = Start("started", attempt, explore_tests.INTEREST)
        store.list_recent.return_value = []
        store.finish.return_value = replace(explore_tests.earlier(), sources=(PAGE,))
        interests = MagicMock()
        interests.pull.return_value = None
        outcome = explore_next(store, self.client, now=NOW, interests=interests, browser=self.browser.access())
        store.start_next.assert_called_once_with(now=NOW, browse=True)
        return outcome

    def test_browsing_only_when_the_attempt_may_browse(self):
        self.client.responses.create.side_effect = [
            response(call("open_page", 1, url=PAGE)), response(answer(explore_tests.FINDING)),
        ]
        self.assertEqual(self.explore_next(browsed=True),
                         "Explored Old mechanical design: saved a finding (browsed 1 pages, 1 sources).")
        # Over today's browsing limit: web search, as without a browser.
        self.client.responses.create.side_effect = None
        self.client.responses.create.return_value = explore_tests.explore_response(explore_tests.FINDING)
        self.assertEqual(self.explore_next(browsed=False), "Explored Old mechanical design: saved a finding (1 sources).")
        self.assertEqual(self.client.responses.create.call_args.kwargs["tools"], [{"type": "web_search"}])


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class BrowseLimitDatabaseTests(unittest.TestCase):
    setUpClass = classmethod(interest_tests.summary_tests.SummaryDatabaseTests.setUpClass.__func__)

    def setUp(self):
        exploration_tests.ExplorationStoreDatabaseTests.setUp(self)

    def test_at_most_two_browsing_sessions_a_day(self):
        for title in ("Second", "Third", "Fourth"):
            self.interests.spawn(*interest_tests.spawn_args(title), now=NOW)
        started = [self.explorations.start_next(now=NOW + timedelta(hours=6.1 * n), browse=True).exploration
                   for n in range(3)]
        # The third that day searches instead.
        self.assertEqual([attempt.browsed for attempt in started], [True, True, False])
        # Once the first has left the 24 hours, she may browse again; without a browser, never.
        self.assertFalse(self.explorations.start_next(now=NOW + timedelta(hours=24, seconds=1)).exploration.browsed)
        self.assertTrue(self.explorations.start_next(
            now=NOW + timedelta(hours=30.2), browse=True,
        ).exploration.browsed)


class AppBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_her_browser_is_handed_over_only_while_the_service_is_connected(self):
        async with keyboard_tests.running_app() as (app, pilot):
            loop = asyncio.get_running_loop()
            self.assertIsNone(app.exploration_browser(loop))
            tools = tuple(ServiceTool(name, "", False, {}) for name in (
                "browser_navigate", "browser_navigate_back", "browser_snapshot", "browser_wait_for", "browser_close",
            ))
            with patch.object(app.services, "status", return_value=ServiceStatus(CONNECTED, None, tools[:2])):
                self.assertIsNone(app.exploration_browser(loop))  # missing tools it needs
            reply = MagicMock(content=[MagicMock(type="text", text="- Page URL: x")], is_error=False)
            app.services.call_tool = AsyncMock(return_value=reply)
            with patch.object(app.services, "status", return_value=ServiceStatus(CONNECTED, None, tools)):
                browser = app.exploration_browser(loop)
            # From the exploration thread, calls run on the app's loop.
            result = await asyncio.to_thread(browser.call, "browser_snapshot", {})
            self.assertEqual(result, ("- Page URL: x", False))
            app.services.call_tool.assert_awaited_once_with("playwright", "browser_snapshot", {})
            self.assertIsNotNone(await asyncio.to_thread(browser.refuse, "http://localhost:8000"))
            with patch.object(app, "can_explore", return_value=False):
                self.assertFalse(browser.keep_going())


if __name__ == "__main__":
    unittest.main()
