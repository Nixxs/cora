"""use_browser: her browser jobs run by a helper on the Browser model, with a small context. Mocked APIs only."""

import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from backend_chat import SLOW_TOOLS, TOOL_RESULT_SECONDS
from services.connection import CONNECTED, ServiceStatus, ServiceTool
from session_config import MODEL_AREAS
from tools import use_browser
from tools.use_browser import BrowserHelper, DROPPED, KEEP_RESULTS, MAX_STEPS, UseBrowserTool
from ui.model_settings import AREA_LABELS
import test_keyboard_persistence as keyboard_tests


def call(name, arguments, call_id):
    return SimpleNamespace(type="function_call", name=name, arguments=json.dumps(arguments), call_id=call_id,
                           model_dump=lambda **_: {"type": "function_call", "name": name, "call_id": call_id,
                                                   "arguments": json.dumps(arguments)})


def answer(text):
    content = SimpleNamespace(type="output_text", text=text)
    return SimpleNamespace(type="message", content=[content],
                           model_dump=lambda **_: {"type": "message", "role": "assistant", "content": text})


class FakeClient:
    """Answers each request with the next of `outputs`; keeps what each request sent."""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.requests = []
        self.responses = SimpleNamespace(create=self.create)

    async def create(self, **request):
        # A copy: the helper keeps changing its list of items.
        self.requests.append(json.loads(json.dumps(request, default=str)))
        output = self.outputs.pop(0) if self.outputs else [answer("Done.")]
        return SimpleNamespace(output=output)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


def helper(client, tools, details="Name: Sam", clock=None):
    write = MagicMock()
    made = BrowserHelper(lambda: tools, AsyncMock(return_value="gpt-6-luna"), lambda: details, write,
                         client_factory=lambda: client, **({"clock": clock} if clock else {}))
    return made, write


class HelperTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_job_runs_on_the_browser_model_with_a_small_context_and_reports(self):
        snapshot = AsyncMock(return_value={"status": "ok", "output_untrusted": "Flights from $420"})
        tools = {"playwright__browser_snapshot": ({"type": "function", "name": "playwright__browser_snapshot"},
                                                  snapshot)}
        client = FakeClient([[call("playwright__browser_snapshot", {}, "c1")],
                             [answer("Cheapest fare on 3 Dec is $420 (https://example.com/fares).")]])
        made, write = helper(client, tools)
        result = await made.run("Find the cheapest fare to Perth on 3 Dec", "https://example.com")
        self.assertEqual(result["status"], "ok")
        self.assertIn("$420", result["report_untrusted"])
        self.assertIn("data", result["note"])
        first = client.requests[0]
        self.assertEqual(first["model"], "gpt-6-luna")
        self.assertEqual([tool["name"] for tool in first["tools"]], ["playwright__browser_snapshot"])
        self.assertFalse(first["store"])
        # Only the job and the person's details: none of her own context.
        self.assertEqual(len(first["input"]), 1)
        self.assertIn("Perth", first["input"][0]["content"])
        self.assertIn("https://example.com", first["input"][0]["content"])
        self.assertIn("Name: Sam", first["instructions"])
        snapshot.assert_awaited_once_with({})
        # Activity names tools, never the job, pages or details.
        logged = " ".join(str(entry) for entry in write.call_args_list)
        self.assertNotIn("Perth", logged)
        self.assertNotIn("Sam", logged)

    async def test_older_pages_leave_its_context(self):
        snapshot = AsyncMock(return_value={"status": "ok", "output_untrusted": "page"})
        tools = {"snap": ({"type": "function", "name": "snap"}, snapshot)}
        client = FakeClient([[call("snap", {}, f"c{index}")] for index in range(5)] + [[answer("Report.")]])
        made, _ = helper(client, tools)
        await made.run("Read five pages", None)
        outputs = [item["output"] for item in client.requests[-1]["input"] if item["type"] == "function_call_output"]
        self.assertEqual(len(outputs), 5)
        self.assertEqual(outputs[:-KEEP_RESULTS], [DROPPED] * (5 - KEEP_RESULTS))
        self.assertTrue(all(output != DROPPED for output in outputs[-KEEP_RESULTS:]))

    async def test_past_its_step_limit_it_must_report(self):
        snapshot = AsyncMock(return_value={"status": "ok"})
        tools = {"snap": ({"type": "function", "name": "snap"}, snapshot)}
        client = FakeClient([[call("snap", {}, f"c{index}")] for index in range(MAX_STEPS)] + [[answer("Partly done.")]])
        made, _ = helper(client, tools)
        result = await made.run("Keep going", None)
        self.assertEqual(snapshot.await_count, MAX_STEPS)
        self.assertEqual(client.requests[-1]["tool_choice"], "none")
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["report_untrusted"], "Partly done.")

    async def test_without_the_browser_it_says_so_and_asks_no_model(self):
        client = FakeClient([])
        made, _ = helper(client, {})
        result = await made.run("Anything", None)
        self.assertEqual(result["status"], "error")
        self.assertEqual(client.requests, [])

    async def test_an_unknown_tool_or_a_failing_one_answers_with_an_error(self):
        failing = AsyncMock(side_effect=RuntimeError("secret page text"))
        tools = {"snap": ({"type": "function", "name": "snap"}, failing)}
        client = FakeClient([[call("nope", {}, "c1")], [call("snap", {}, "c2")], [answer("Couldn't.")]])
        made, write = helper(client, tools)
        await made.run("Try", None)
        outputs = [item["output"] for item in client.requests[-1]["input"] if item["type"] == "function_call_output"]
        self.assertIn("isn't one of your browser tools", outputs[0])
        self.assertIn("didn't answer", outputs[1])
        self.assertNotIn("secret", " ".join(str(entry) for entry in write.call_args_list))

    async def test_the_tool_answers_her_once_and_continues(self):
        made = MagicMock()
        made.run = AsyncMock(return_value={"status": "ok", "report_untrusted": "Found it."})
        connection = MagicMock()
        connection.response.item.create = AsyncMock()
        connection.response.create = AsyncMock()
        tool = UseBrowserTool(lambda: connection, lambda: False, MagicMock(), made)
        await tool.handle("call-1", {"job": "Look", "start_url": None})
        made.run.assert_awaited_once_with("Look", None)
        sent = connection.response.item.create.await_args.kwargs["item"]
        self.assertEqual(sent["call_id"], "call-1")
        self.assertIn("Found it.", sent["output"])
        connection.response.create.assert_awaited_once()

    def test_arguments_are_checked(self):
        use_browser.validate_arguments({"job": "Look", "start_url": None})
        for bad in ({"job": " ", "start_url": None}, {"job": "Look"}, {"job": "Look", "start_url": 3}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                use_browser.validate_arguments(bad)

    def test_written_chats_wait_long_enough_for_a_job(self):
        self.assertGreater(SLOW_TOOLS["use_browser"], TOOL_RESULT_SECONDS)
        self.assertGreater(SLOW_TOOLS["use_browser"], use_browser.JOB_SECONDS)

    def test_the_browser_model_is_an_area_of_its_own(self):
        self.assertIn("browser", MODEL_AREAS)
        self.assertIn("browser", AREA_LABELS)


BROWSER_TOOLS = (
    ServiceTool("browser_navigate", "Navigate.", False, {}, unasked=True),
    ServiceTool("browser_click", "Click.", False, {}, unasked=True),
    ServiceTool("browser_snapshot", "Snapshot.", True, {}),
)


class WiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_she_gets_use_browser_instead_of_the_browsers_tools_which_go_to_the_helper(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.service_tool_choices = {"playwright": {"browser_navigate", "browser_click", "browser_snapshot"}}
            connected = ServiceStatus(CONNECTED, tools=BROWSER_TOOLS)
            with patch.object(app.services, "status",
                              side_effect=lambda service: connected if service == "playwright" else ServiceStatus()):
                app.service_tools_changed = False
                app.sync_service_tools()
            self.assertIn("use_browser", app.tools.names())
            self.assertFalse(any(name.startswith("playwright__") for name in app.tools.names()))
            self.assertEqual(set(app.browser_tools), {"playwright__browser_navigate", "playwright__browser_click",
                                                      "playwright__browser_snapshot"})
            self.assertTrue(app.service_tools_changed)
            # The helper's click still asks before anything is sent.
            click = app.browser_tools["playwright__browser_click"][1].__self__
            self.assertEqual(click.confirm, app.confirmations.ask)
            self.assertIsNotNone(click.asks_when)

            app.service_tools_changed = False
            app.sync_service_tools()  # the browser is off now
            self.assertNotIn("use_browser", app.tools.names())
            self.assertEqual(app.browser_tools, {})
            self.assertTrue(app.service_tools_changed)

    async def test_forms_get_the_persons_name_and_key_details(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.live.person_name = "Sam"
            app.live.key_details_context = '{"email": "sam@example.com"}'
            details = app.details_for_forms()
            self.assertIn("Name: Sam", details)
            self.assertIn("sam@example.com", details)
            app.persistence.load_models.return_value = {"browser": "gpt-5.6-terra"}
            self.assertEqual(await app.browser_model(), "gpt-5.6-terra")


if __name__ == "__main__":
    unittest.main()
