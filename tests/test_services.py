"""Outside services over MCP: connections, tokens, saved switches and the Settings page. No real servers."""

import asyncio
from contextlib import asynccontextmanager
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import psycopg
from psycopg import sql
from sqlalchemy import text
from keyring.errors import PasswordDeleteError
from textual.widgets import Input, Label, Static, Switch, TabbedContent

from migrate import apply_migrations, database_settings
from services import credentials
from services.catalogue import SERVICES
from services.connection import (
    CONNECTED, FAILED, OFF, HttpErrors, ServiceConnection, ServiceManager, failure_reason,
)
from session_config import BACKEND_MODEL, MODEL_AREAS
from storage.database import build_engine
from storage.keyboard_persistence import KeyboardPersistence
from storage.service_settings_store import ServiceSettingsStore
from ui.model_settings import ModelSettingsScreen
import test_keyboard_persistence as keyboard_tests

TOKEN = "github_pat_secret_value"
MODELS = {area: BACKEND_MODEL for area in MODEL_AREAS}


def tool(name, read_only, description="Does a thing.\nMore detail."):
    return SimpleNamespace(
        name=name, description=description, input_schema={"type": "object"},
        annotations=SimpleNamespace(read_only_hint=read_only) if read_only is not None else None,
    )


class FakeClient:
    """Two pages of tools, like a server that paginates its listing."""

    def __init__(self):
        self.pages = {
            None: SimpleNamespace(tools=[tool("search_issues", True), tool("create_issue", False)], next_cursor="2"),
            "2": SimpleNamespace(tools=[tool("get_me", None)], next_cursor=None),
        }

    async def list_tools(self, cursor=None):
        return self.pages[cursor]


def fake_open(client=None, error=None, http_error=None):
    seen = {}

    @asynccontextmanager
    async def open_client(url, token, errors):
        seen.update(url=url, token=token, closed=False)
        if http_error:
            errors.last = http_error
        if error:
            raise error
        try:
            yield client or FakeClient()
        finally:
            seen["closed"] = True

    return open_client, seen


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


class ServiceConnectionTests(unittest.IsolatedAsyncioTestCase):
    def manager(self, open_client):
        self.activity = []
        self.tasks = []
        return ServiceManager(
            schedule=lambda run: self.tasks.append(asyncio.ensure_future(run)),
            write_activity=lambda message, summary: self.activity.append((message, summary)),
            open_client=open_client,
        )

    async def test_connects_lists_every_page_of_tools_and_stays_open_until_stopped(self):
        open_client, seen = fake_open()
        manager = self.manager(open_client)
        manager.start("github", TOKEN)
        await settle()
        status = manager.status("github")
        self.assertEqual(status.state, CONNECTED)
        self.assertEqual([t.name for t in status.tools], ["create_issue", "get_me", "search_issues"])
        self.assertEqual([t.read_only for t in status.tools], [False, False, True])
        self.assertEqual(status.tools[0].description, "Does a thing.\nMore detail.")
        self.assertEqual(seen, {"url": SERVICES["github"].url, "token": TOKEN, "closed": False})
        self.assertEqual(self.activity[-1][1], "GitHub connected · 3 tools")
        self.assertIn("(1 marked read-only)", self.activity[-1][0])

        manager.stop("github")
        await asyncio.wait_for(self.tasks[0], 1)
        self.assertTrue(seen["closed"])
        self.assertEqual(manager.status("github").state, OFF)
        self.assertEqual(self.activity[-1], ("MCP GitHub: disconnected.", "GitHub off"))

    async def test_a_failure_reports_the_http_reply_never_the_token(self):
        error = ExceptionGroup("task group", [RuntimeError("Server returned an error response")])
        open_client, _ = fake_open(error=error, http_error="HTTP 401: unauthorized: token authentication failed")
        manager = self.manager(open_client)
        manager.start("github", TOKEN)
        await asyncio.wait_for(self.tasks[0], 1)
        status = manager.status("github")
        self.assertEqual(status.state, FAILED)
        self.assertEqual(status.reason, "HTTP 401: unauthorized: token authentication failed")
        self.assertEqual(self.activity[-1][1], "GitHub connection failed")
        self.assertNotIn(TOKEN, repr(self.activity))

    async def test_without_an_http_reply_the_innermost_error_is_named(self):
        open_client, _ = fake_open(error=ExceptionGroup("outer", [ExceptionGroup("inner", [OSError("no route")])]))
        manager = self.manager(open_client)
        manager.start("github", TOKEN)
        await asyncio.wait_for(self.tasks[0], 1)
        self.assertEqual(manager.status("github").reason, "OSError: no route")

    async def test_starting_again_replaces_the_old_connection_which_then_stays_quiet(self):
        open_client, _ = fake_open()
        manager = self.manager(open_client)
        manager.start("github", "old")
        manager.start("github", TOKEN)
        await settle()
        await asyncio.wait_for(self.tasks[0], 1)
        self.assertEqual(manager.connections["github"].token, TOKEN)
        # Only the current connection reported, and the replaced one wrote no "disconnected".
        self.assertEqual([summary for _, summary in self.activity], ["GitHub connected · 3 tools"])
        manager.stop_all()
        await asyncio.wait_for(self.tasks[1], 1)
        self.assertEqual(manager.connections, {})

    async def test_a_dropped_connection_after_connecting_is_reported_as_failed(self):
        dropped = asyncio.Event()

        @asynccontextmanager
        async def open_client(url, token, errors):
            yield FakeClient()
            await dropped.wait()

        connection = ServiceConnection(SERVICES["github"], TOKEN, MagicMock())
        connection.open_client = open_client
        task = asyncio.ensure_future(connection.run())
        await settle()
        self.assertEqual(connection.status.state, CONNECTED)
        connection.stop()
        dropped.set()
        await asyncio.wait_for(task, 1)
        self.assertEqual(connection.status.state, OFF)


class HttpErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_hook_keeps_only_error_replies_with_their_first_line(self):
        errors = HttpErrors()
        ok = SimpleNamespace(status_code=200, aread=AsyncMock())
        await errors.hook(ok)
        ok.aread.assert_not_called()
        self.assertIsNone(errors.last)
        bad = SimpleNamespace(
            status_code=400, aread=AsyncMock(), reason_phrase="Bad Request",
            text="bad request: Authorization header is badly formatted\nmore\n",
        )
        await errors.hook(bad)
        self.assertEqual(errors.last, "HTTP 400: bad request: Authorization header is badly formatted")
        await errors.hook(SimpleNamespace(status_code=502, aread=AsyncMock(), reason_phrase="Bad Gateway", text=""))
        self.assertEqual(errors.last, "HTTP 502: Bad Gateway")

    def test_reason_falls_back_to_the_error_type(self):
        self.assertEqual(failure_reason(TimeoutError(), HttpErrors()), "TimeoutError")


class CredentialTests(unittest.TestCase):
    def test_tokens_use_the_system_store_under_cora(self):
        with patch("services.credentials.keyring") as keyring:
            keyring.get_password.return_value = ""
            self.assertIsNone(credentials.get_token("github"))
            credentials.save_token("github", f"  {TOKEN}\n")
            keyring.set_password.assert_called_once_with("cora", "mcp:github", TOKEN)
            with self.assertRaises(ValueError):
                credentials.save_token("github", "   ")
            keyring.delete_password.side_effect = PasswordDeleteError()
            credentials.forget_token("github")  # nothing saved is not an error
            keyring.delete_password.assert_called_once_with("cora", "mcp:github")


class ServiceSettingsValidationTests(unittest.TestCase):
    def test_unknown_service_is_rejected_before_touching_the_database(self):
        engine = MagicMock()
        with self.assertRaises(ValueError):
            ServiceSettingsStore(engine).set_enabled("gmail", True)
        engine.connect.assert_not_called()

    def test_adapter_uses_the_conversation_engine(self):
        store = MagicMock()
        with patch("storage.keyboard_persistence.ServiceSettingsStore") as settings_type:
            settings_type.return_value.enabled_services.return_value = {"github"}
            persistence = KeyboardPersistence(store)
            self.assertEqual(persistence.load_enabled_services(), {"github"})
            persistence.save_service_enabled("github", False)
        settings_type.assert_called_with(store.engine)
        settings_type.return_value.set_enabled.assert_called_once_with("github", False)
        store.create_conversation.assert_not_called()


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class ServiceSettingsStoreDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        settings = database_settings()
        admin = psycopg.connect(**settings, autocommit=True)
        cls.addClassCleanup(admin.close)
        cls.database = "cora_test_service_settings_" + uuid4().hex
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.database)))
        cls.addClassCleanup(
            admin.execute, sql.SQL("DROP DATABASE {}").format(sql.Identifier(cls.database))
        )
        with psycopg.connect(**{**settings, "dbname": cls.database}, autocommit=True) as connection:
            apply_migrations(connection)
        with patch("storage.database.database_settings", return_value={**settings, "dbname": cls.database}):
            cls.engine = build_engine()
        cls.addClassCleanup(cls.engine.dispose)

    def setUp(self):
        # Only this disposable test database is cleared, never Cora's database.
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM public.service_settings"))
        self.store = ServiceSettingsStore(self.engine)

    def test_services_are_off_until_switched_on_and_can_be_switched_off(self):
        self.assertEqual(self.store.enabled_services(), set())
        self.store.set_enabled("github", True)
        self.assertEqual(self.store.enabled_services(), {"github"})
        self.store.set_enabled("github", False)
        self.assertEqual(self.store.enabled_services(), set())

    def test_a_service_dropped_from_the_catalogue_is_ignored(self):
        with self.engine.begin() as connection:
            connection.execute(text("INSERT INTO public.service_settings (service, enabled) VALUES ('old', true)"))
        self.assertEqual(self.store.enabled_services(), set())


class ServicesPageTests(unittest.IsolatedAsyncioTestCase):
    async def open_services(self, app, pilot, enabled=frozenset(), mock_connections=True):
        app.persistence.load_models.return_value = MODELS
        app.persistence.load_enabled_services.return_value = set(enabled)
        if mock_connections:
            app.services.start = MagicMock()
            app.services.stop = MagicMock()
        await pilot.click("#settings-button")
        await asyncio.wait_for(app.workers.wait_for_complete(), 3)
        await pilot.pause()
        self.assertIsInstance(app.screen, ModelSettingsScreen)
        app.screen.query_one(TabbedContent).active = "settings-services"
        await pilot.pause()
        # Details start closed; clicking the service's row opens them.
        await pilot.click("#service-title-github")
        await pilot.pause()

    async def settle(self, app, pilot):
        await pilot.pause()
        await asyncio.wait_for(app.workers.wait_for_complete(), 3)
        await pilot.pause()

    async def test_switching_on_without_a_token_asks_for_one_and_stays_off(self):
        async with keyboard_tests.running_app() as (app, pilot):
            with patch("services.credentials.keyring") as keyring:
                keyring.get_password.return_value = None
                await self.open_services(app, pilot)
                switch = app.screen.query_one("#service-enabled-github", Switch)
                switch.value = True
                await self.settle(app, pilot)
                self.assertFalse(switch.value)
                app.persistence.save_service_enabled.assert_not_called()
                app.services.start.assert_not_called()

    async def test_switching_on_with_a_token_saves_and_connects_then_off_disconnects(self):
        async with keyboard_tests.running_app() as (app, pilot):
            with patch("services.credentials.keyring") as keyring:
                keyring.get_password.return_value = TOKEN
                await self.open_services(app, pilot)
                switch = app.screen.query_one("#service-enabled-github", Switch)
                switch.value = True
                await self.settle(app, pilot)
                app.persistence.save_service_enabled.assert_called_once_with("github", True)
                app.services.start.assert_called_once_with("github", TOKEN)
                switch.value = False
                await self.settle(app, pilot)
                app.persistence.save_service_enabled.assert_called_with("github", False)
                app.services.stop.assert_called_once_with("github")

    async def test_saving_a_token_stores_it_and_clears_the_field(self):
        async with keyboard_tests.running_app() as (app, pilot):
            with patch("services.credentials.keyring") as keyring:
                keyring.get_password.return_value = None
                await self.open_services(app, pilot)
                field = app.screen.query_one("#service-token-github", Input)
                field.value = TOKEN
                await pilot.click("#service-save-github")
                await self.settle(app, pilot)
                keyring.set_password.assert_called_once_with("cora", "mcp:github", TOKEN)
                self.assertEqual(field.value, "")
                self.assertEqual(field.placeholder, "token saved · paste to replace")
                self.assertNotIn(TOKEN, "\n".join(app.activity.entries))
                # Not switched on, so saving a token connects nothing.
                app.services.start.assert_not_called()

    async def test_forgetting_the_token_switches_the_service_off(self):
        async with keyboard_tests.running_app() as (app, pilot):
            with patch("services.credentials.keyring") as keyring:
                keyring.get_password.return_value = TOKEN
                await self.open_services(app, pilot, enabled={"github"})
                switch = app.screen.query_one("#service-enabled-github", Switch)
                self.assertTrue(switch.value)
                await pilot.click("#service-forget-github")
                await self.settle(app, pilot)
                keyring.delete_password.assert_called_once_with("cora", "mcp:github")
                self.assertFalse(switch.value)
                app.persistence.save_service_enabled.assert_called_once_with("github", False)
                app.services.stop.assert_called_once_with("github")

    async def test_the_row_opens_and_closes_its_details_but_the_switch_does_not(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.open_services(app, pilot)
            details = app.screen.query_one("#service-details-github")
            title = app.screen.query_one("#service-title-github", Label)
            self.assertTrue(details.display)
            self.assertEqual(str(title.render()), "▾ GitHub")
            await pilot.click("#service-status-github")
            await pilot.pause()
            self.assertFalse(details.display)
            self.assertEqual(str(title.render()), "▸ GitHub")
            await pilot.click("#service-enabled-github")
            await pilot.pause()
            self.assertFalse(details.display)

    async def test_a_failure_shows_briefly_in_the_row_and_in_full_in_the_details(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.open_services(app, pilot, mock_connections=False)
            error = ExceptionGroup("task group", [RuntimeError("Server returned an error response")])
            open_client, _ = fake_open(error=error, http_error="HTTP 401: unauthorized: token authentication failed")
            app.services.open_client = open_client
            app.services.start("github", TOKEN)
            await pilot.pause(0.7)
            self.assertEqual(str(app.screen.query_one("#service-status-github", Static).render()), "× failed")
            reason = app.screen.query_one("#service-reason-github", Static)
            self.assertTrue(reason.display)
            self.assertIn("HTTP 401: unauthorized", str(reason.render()))

    async def test_status_and_tools_follow_the_connection(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.open_services(app, pilot, mock_connections=False)
            tools = app.screen.query_one("#service-tools-github", Static)
            self.assertIn("Tools appear here once connected.", str(tools.render()))
            open_client, _ = fake_open()
            app.services.open_client = open_client
            app.services.start("github", TOKEN)
            await pilot.pause(0.7)
            status = str(app.screen.query_one("#service-status-github", Static).render())
            self.assertIn("connected · 3 tools", status)
            listing = str(tools.render())
            self.assertIn("search_issues", listing)
            self.assertIn("read-only", listing)
            self.assertIn("changes things", listing)
            app.services.stop_all()
