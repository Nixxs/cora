"""Local MCP services such as the Playwright browser: no token, read-only overrides and the address guard. No real servers."""

import asyncio
from contextlib import asynccontextmanager
import re
import socket
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from textual.widgets import Switch, TabbedContent

from services.catalogue import ROOT, SERVICES
from services.connection import (
    CONNECTED, FAILED, ServiceManager, local_log_path, open_stdio_client, passed_environment,
)
from services.tools import ServiceToolHandler, tool_schema
from services.web_guard import refused_address
from ui.model_settings import ModelSettingsScreen
from test_services import MODELS, settle, tool
import test_keyboard_persistence as keyboard_tests


class BrowserClient:
    def __init__(self):
        self.calls = []

    async def list_tools(self, cursor=None):
        return SimpleNamespace(tools=[
            tool("browser_navigate", False), tool("browser_click", False), tool("browser_snapshot", True),
            tool("browser_type", False), tool("browser_file_upload", False), tool("browser_run_code_unsafe", False),
            tool("browser_evaluate", False),
        ], next_cursor=None)

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="Page loaded")],
                               structured_content=None, is_error=False)


def fake_local(error=None):
    seen = {}

    @asynccontextmanager
    async def open_local(service, errors):
        seen["service"] = service.id
        if error:
            raise error
        yield BrowserClient()

    return open_local, seen


def resolving_to(*addresses):
    return AsyncMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0)) for address in addresses])


class LocalServiceTests(unittest.IsolatedAsyncioTestCase):
    def manager(self, open_local):
        self.activity, self.tasks = [], []
        return ServiceManager(
            schedule=lambda run: self.tasks.append(asyncio.ensure_future(run)),
            write_activity=lambda message, summary: self.activity.append((message, summary)),
            open_client=MagicMock(side_effect=AssertionError("a local service never uses HTTP")),
            open_local=open_local,
        )

    def test_the_browser_is_local_needs_no_token_and_github_still_does(self):
        browser = SERVICES["playwright"]
        self.assertFalse(browser.needs_token)
        # A throwaway run of the compose file's browser service, removed when it stops.
        self.assertEqual(browser.command[:2], ("docker", "compose"))
        self.assertEqual(browser.command[-5:], ("browser", "run", "--rm", "-T", "browser"))
        self.assertTrue(SERVICES["github"].needs_token)

    def test_the_compose_browser_is_pinned_limited_and_on_its_own_network(self):
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        # The browser service's block: from its name to the next top-level key.
        browser = re.search(r"\n  browser:\n(.*?)\n\S", compose, re.S).group(1)
        self.assertRegex(browser, r"image: mcr\.microsoft\.com/playwright/mcp:v\d+\.\d+\.\d+\n")
        self.assertIn('profiles: ["browser"]', browser)
        self.assertIn('"--isolated"', browser)
        self.assertIn('security_opt: ["no-new-privileges:true"]', browser)
        self.assertIn("networks: [browser]", browser)
        # Nothing else joins its network, so it can't reach the database or Signal.
        self.assertEqual(compose.count("networks: [browser]"), 1)

    def test_a_local_server_gets_dockers_own_settings_but_nothing_else(self):
        environment = {"ProgramFiles": r"C:\Program Files", "DOCKER_HOST": "npipe:////./pipe/docker",
                       "OPENAI_API_KEY": "secret", "CORA_DB_PASSWORD": "secret"}
        with patch.dict("services.connection.environ", environment, clear=True):
            # Windows upper-cases the names.
            passed = {name.upper(): value for name, value in passed_environment().items()}
        self.assertEqual(passed, {"PROGRAMFILES": r"C:\Program Files", "DOCKER_HOST": "npipe:////./pipe/docker"})

    async def test_starts_without_a_token_lets_browsing_run_and_hides_running_code(self):
        open_local, seen = fake_local()
        manager = self.manager(open_local)
        manager.start("playwright")
        await settle()
        status = manager.status("playwright")
        self.assertEqual(seen, {"service": "playwright"})
        self.assertEqual(status.state, CONNECTED)
        tools = {t.name: t for t in status.tools}
        # Running her own code is never offered, nor listed.
        self.assertNotIn("browser_run_code_unsafe", tools)
        self.assertNotIn("browser_evaluate", tools)
        # Opening, clicking and typing run without asking; the server's own hint is kept as it was.
        self.assertEqual({name: t.asks_first for name, t in tools.items()}, {
            "browser_click": False, "browser_navigate": False, "browser_snapshot": False, "browser_type": False,
            "browser_file_upload": True,
        })
        self.assertFalse(tools["browser_click"].read_only)
        self.assertTrue(tools["browser_click"].unasked)
        manager.stop_all()
        await asyncio.wait_for(self.tasks[0], 1)

    async def test_a_local_failure_points_to_its_own_output(self):
        open_local, _ = fake_local(FileNotFoundError("docker not found; install Docker and start it"))
        manager = self.manager(open_local)
        manager.start("playwright")
        await asyncio.wait_for(self.tasks[0], 1)
        status = manager.status("playwright")
        self.assertEqual(status.state, FAILED)
        self.assertIn("docker not found", status.reason)
        self.assertIn(str(local_log_path("playwright")), status.reason)

    async def test_without_docker_the_server_is_never_started(self):
        with patch("services.connection.shutil.which", return_value=None), \
                patch("services.connection.stdio_client") as stdio:
            with self.assertRaisesRegex(FileNotFoundError, "Docker"):
                async with open_stdio_client(SERVICES["playwright"], None):
                    pass
        stdio.assert_not_called()


class AddressGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_public_web_pages_may_be_opened(self):
        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", resolving_to("93.184.215.14")):
            self.assertIsNone(await refused_address("https://example.com/birds"))
            self.assertIsNone(await refused_address("http://en.wikipedia.org/wiki/Swift"))
        for url in (
            "file:///C:/Users/Nicho/secrets.txt", "javascript:alert(1)", "about:blank", "ftp://example.com",
            "http://localhost:8000", "http://127.0.0.1/", "http://192.168.1.1/admin", "http://10.0.0.5",
            "http://169.254.169.254/latest", "http://[::1]/", "http://router/", "http://printer.local/",
            "https:///no-host", None, 42,
        ):
            with self.subTest(url=url):
                self.assertIsNotNone(await refused_address(url))

    async def test_a_public_name_that_points_into_the_home_network_is_refused(self):
        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", resolving_to("93.184.215.14", "192.168.1.1")):
            self.assertIn("local network", await refused_address("https://sneaky.example.com"))

    async def test_an_unknown_name_is_left_for_the_browser_to_fail(self):
        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", AsyncMock(side_effect=socket.gaierror("no such host"))):
            self.assertIsNone(await refused_address("https://no-such-site.example"))


class GuardedNavigationTests(unittest.IsolatedAsyncioTestCase):
    def handler(self, tool_name, confirm=None):
        self.call_tool = AsyncMock(return_value=SimpleNamespace(
            content=[SimpleNamespace(type="text", text="Page loaded")], structured_content=None, is_error=False,
        ))
        return ServiceToolHandler("playwright", tool_name, self.call_tool, MagicMock(), lambda: False, MagicMock(),
                                  confirm=confirm)

    async def test_opening_a_home_network_page_is_not_done_and_never_reaches_the_browser(self):
        confirm = AsyncMock()
        result = await self.handler("browser_navigate", confirm).result({"url": "http://192.168.1.1"})
        self.assertEqual(result["status"], "not_done")
        self.assertIn("local network", result["message"])
        self.call_tool.assert_not_awaited()
        confirm.assert_not_awaited()

    async def test_a_public_page_opens_without_asking(self):
        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", resolving_to("93.184.215.14")):
            result = await self.handler("browser_navigate").result({"url": "https://example.com"})
        self.assertEqual(result["status"], "ok")
        self.call_tool.assert_awaited_once_with("playwright", "browser_navigate", {"url": "https://example.com"})

    def test_clicking_and_typing_carry_their_limits(self):
        from services.connection import ServiceTool
        for name in ("browser_type", "browser_fill_form", "browser_select_option"):
            description = tool_schema("playwright", ServiceTool(name, "Type text.", False, {}))["description"]
            self.assertIn("Never enter the person's personal details", description)
            self.assertIn("payment", description)
        click = tool_schema("playwright", ServiceTool("browser_click", "Click.", False, {}))["description"]
        self.assertIn("Never click to book, buy, pay", click)

    def test_opening_a_page_tells_cora_to_read_it_with_a_snapshot(self):
        from services.connection import ServiceTool
        navigate = tool_schema("playwright", ServiceTool("browser_navigate", "Navigate to a URL.", True, {}))
        self.assertIn("call browser_snapshot next", navigate["description"])
        snapshot = tool_schema("playwright", ServiceTool("browser_snapshot", "Capture the page.", True, {}))
        self.assertNotIn("call browser_snapshot next", snapshot["description"])

    async def test_other_tools_are_not_address_checked(self):
        result = await self.handler("browser_snapshot").result({})
        self.assertEqual(result["status"], "ok")


class BrowserSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def open_browser(self, app, pilot, enabled=frozenset()):
        app.persistence.load_models.return_value = MODELS
        app.persistence.load_enabled_services.return_value = set(enabled)
        app.persistence.load_service_tools.return_value = {}
        app.services.start = MagicMock()
        app.services.stop = MagicMock()
        await pilot.click("#settings-button")
        await asyncio.wait_for(app.workers.wait_for_complete(), 3)
        await pilot.pause()
        self.assertIsInstance(app.screen, ModelSettingsScreen)
        app.screen.query_one(TabbedContent).active = "settings-services"
        await pilot.pause()

    async def test_the_browser_has_no_token_field_and_switches_on_without_one(self):
        async with keyboard_tests.running_app() as (app, pilot):
            with patch("services.credentials.keyring") as keyring:
                keyring.get_password.return_value = None
                await self.open_browser(app, pilot)
                self.assertFalse(app.screen.query("#service-token-playwright"))
                switch = app.screen.query_one("#service-enabled-playwright", Switch)
                switch.value = True
                await pilot.pause()
                await asyncio.wait_for(app.workers.wait_for_complete(), 3)
                await pilot.pause()
                self.assertTrue(switch.value)
                app.persistence.save_service_enabled.assert_called_once_with("playwright", True)
                app.services.start.assert_called_once_with("playwright")
                # Only GitHub's token is looked up; the browser has none.
                self.assertEqual({call.args[1] for call in keyring.get_password.call_args_list}, {"mcp:github"})

    async def test_at_launch_a_switched_on_browser_starts_without_a_token(self):
        async with keyboard_tests.running_app() as (app, pilot):
            with patch("services.credentials.keyring") as keyring:
                app.persistence.load_enabled_services.return_value = {"playwright"}
                app.persistence.load_service_tools.return_value = {}
                app.services.start = MagicMock()
                app.start_enabled_services()
                await asyncio.wait_for(app.workers.wait_for_complete(), 3)
                app.services.start.assert_called_once_with("playwright")
                keyring.get_password.assert_not_called()
