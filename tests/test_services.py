"""Outside services over MCP: connections, tokens, saved switches and the Settings page. No real servers."""

import asyncio
import base64
from contextlib import asynccontextmanager
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import psycopg
from psycopg import sql
from sqlalchemy import text
from keyring.errors import PasswordDeleteError
from textual.widgets import Input, Label, Static, Switch, TabbedContent, TextArea

from migrate import apply_migrations, database_settings
from services import credentials
from services.catalogue import SERVICES
from services.confirmation import APPROVED, DECLINED, TIMED_OUT, Confirmations
from ui.confirm_view import ConfirmScreen
from services.connection import (
    CONNECTED, FAILED, OFF, HttpErrors, ServiceConnection, ServiceManager, failure_reason,
)
from session_config import BACKEND_MODEL, MODEL_AREAS
from services.tools import (
    CACHE_ENTRIES, CACHE_SECONDS, PAGE_ARGUMENT, PAGE_SCHEMA, RESULT_LIMIT, ResultCache, ServiceToolHandler,
    page_output, result_text, tool_schema,
)
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

        self.calls = []

    async def list_tools(self, cursor=None):
        return self.pages[cursor]

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="Issue #23: fix standby")],
            structured_content=None, is_error=False,
        )


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


async def until(pilot, condition, seconds=5):
    """Pause until the condition holds; timing varies under a full test run."""
    for _ in range(int(seconds / 0.05)):
        if condition():
            return
        await pilot.pause(0.05)


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


class ServiceCallTests(unittest.IsolatedAsyncioTestCase):
    async def test_calls_go_to_the_connected_client_and_fail_when_not_connected(self):
        client = FakeClient()
        open_client, _ = fake_open(client)
        tasks = []
        manager = ServiceManager(lambda run: tasks.append(asyncio.ensure_future(run)), MagicMock(), open_client)
        with self.assertRaises(ConnectionError):
            await manager.call_tool("github", "search_issues", {})
        manager.start("github", TOKEN)
        await manager.wait_settled(1)
        result = await manager.call_tool("github", "search_issues", {"query": "standby"})
        self.assertEqual(result.content[0].text, "Issue #23: fix standby")
        self.assertEqual(client.calls, [("search_issues", {"query": "standby"})])
        manager.stop_all()
        await asyncio.wait_for(tasks[0], 1)
        with self.assertRaises(ConnectionError):
            await manager.call_tool("github", "search_issues", {})

    async def test_changes_are_reported_for_connecting_failing_and_stopping(self):
        changes = MagicMock()
        open_client, _ = fake_open()
        tasks = []
        manager = ServiceManager(lambda run: tasks.append(asyncio.ensure_future(run)), MagicMock(), open_client,
                                 on_change=changes)
        manager.start("github", TOKEN)
        await manager.wait_settled(1)
        self.assertEqual(changes.call_count, 1)
        manager.stop("github")
        self.assertEqual(changes.call_count, 2)
        await asyncio.wait_for(tasks[0], 1)


class ServiceToolTests(unittest.IsolatedAsyncioTestCase):
    def service_tool(self, name="search_issues", schema=None):
        from services.connection import ServiceTool
        return ServiceTool(name, "Search issues.", True, schema if schema is not None else {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"],
        })

    def test_schema_is_namespaced_non_strict_and_drops_schema_only_keys(self):
        schema = tool_schema("github", self.service_tool())
        self.assertEqual(schema["name"], "github__search_issues")
        self.assertFalse(schema["strict"])
        # The page argument is Cora's own, added to every service tool and never required.
        self.assertEqual(schema["parameters"], {
            "type": "object", "properties": {"query": {"type": "string"}, PAGE_ARGUMENT: PAGE_SCHEMA},
            "required": ["query"],
        })
        self.assertTrue(schema["description"].startswith("GitHub (outside service): Search issues."))
        self.assertIn("untrusted", schema["description"])
        self.assertIsNone(tool_schema("github", self.service_tool("bad.name")))
        self.assertIsNone(tool_schema("github", self.service_tool("x" * 60)))
        self.assertEqual(tool_schema("github", self.service_tool(schema={}))["parameters"],
                         {"type": "object", "properties": {PAGE_ARGUMENT: PAGE_SCHEMA}})
        own = {"type": "object", "properties": {PAGE_ARGUMENT: {"type": "string"}}}
        self.assertEqual(tool_schema("github", self.service_tool(schema=own))["parameters"], own)

    def test_text_is_kept_other_content_named_and_structured_content_used_alone(self):
        result = SimpleNamespace(content=[
            SimpleNamespace(type="text", text="hello"), SimpleNamespace(type="image"),
        ], structured_content={"ignored": True}, is_error=True)
        self.assertEqual(result_text(result), "hello\n[image not shown]")
        structured = SimpleNamespace(content=[], structured_content={"login": "nick"}, is_error=False)
        self.assertEqual(result_text(structured), '{"login": "nick"}')

    def test_pages_split_long_text_and_say_how_to_read_the_next(self):
        text = "a" * RESULT_LIMIT + "b" * RESULT_LIMIT + "c" * 5
        first = page_output("github", "get_file_contents", text, False, 1)
        self.assertEqual((first["page"], first["pages"], first["status"]), (1, 3, "ok"))
        self.assertEqual(first["output_untrusted"], "a" * RESULT_LIMIT)
        self.assertIn(f"{PAGE_ARGUMENT}=2", first["next"])
        last = page_output("github", "get_file_contents", text, False, 3)
        self.assertEqual(last["output_untrusted"], "ccccc")
        self.assertNotIn("next", last)
        beyond = page_output("github", "get_file_contents", text, False, 4)
        self.assertEqual(beyond["status"], "error")
        self.assertIn("only 3 pages", beyond["message"])
        empty = page_output("github", "get_me", "", True, 1)
        self.assertEqual((empty["pages"], empty["status"], empty["output_untrusted"]), (1, "error", ""))

    def test_file_contents_sent_as_resources_are_kept_and_binary_is_only_named(self):
        text_file = SimpleNamespace(type="resource", resource=SimpleNamespace(
            uri="repo://nixxs/cora/README.md", text="# Cora", mime_type="text/markdown",
        ))
        blob_file = SimpleNamespace(type="resource", resource=SimpleNamespace(
            uri="repo://nixxs/cora/app.py", blob=base64.b64encode(b"import asyncio\n").decode(),
            mime_type="text/x-python",
        ))
        unlabelled_text = SimpleNamespace(type="resource", resource=SimpleNamespace(
            uri="repo://nixxs/cora/cora.tcss", blob=base64.b64encode(b"Screen {}").decode(), mime_type=None,
        ))
        image = SimpleNamespace(type="resource", resource=SimpleNamespace(
            uri="repo://nixxs/cora/logo.png", blob=base64.b64encode(b"\x89PNG\x00\x01").decode(),
            mime_type="image/png",
        ))
        link = SimpleNamespace(type="resource_link", uri="https://github.com/nixxs/cora")
        output = result_text(SimpleNamespace(
            content=[SimpleNamespace(type="text", text="successfully downloaded"), text_file, blob_file,
                     unlabelled_text, image, link],
            structured_content=None, is_error=False,
        ))
        self.assertEqual(output.split("\n"), [
            "successfully downloaded",
            "[repo://nixxs/cora/README.md]", "# Cora",
            "[repo://nixxs/cora/app.py]", "import asyncio", "",
            "[repo://nixxs/cora/cora.tcss]", "Screen {}",
            "[repo://nixxs/cora/logo.png: binary content (image/png) not shown]",
            "[link: https://github.com/nixxs/cora]",
        ])

    async def test_handler_answers_once_and_continues(self):
        connection = MagicMock()
        connection.response.item.create = AsyncMock()
        connection.response.create = AsyncMock()
        call_tool = AsyncMock(return_value=SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")], structured_content=None, is_error=False,
        ))
        log = MagicMock()
        handler = ServiceToolHandler("github", "search_issues", call_tool, lambda: connection, lambda: False, log)
        await handler.handle("call_1", {"query": "standby"})
        call_tool.assert_awaited_once_with("github", "search_issues", {"query": "standby"})
        item = connection.response.item.create.call_args.kwargs["item"]
        self.assertEqual(item["call_id"], "call_1")
        self.assertEqual(json.loads(item["output"])["output_untrusted"], "ok")
        connection.response.create.assert_awaited_once()

    async def test_later_pages_reuse_a_recent_reply_and_the_first_page_asks_again(self):
        now = [0.0]
        cache = ResultCache(clock=lambda: now[0])
        text = "x" * (RESULT_LIMIT * 2)
        call_tool = AsyncMock(return_value=SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)], structured_content=None, is_error=False,
        ))
        handler = ServiceToolHandler("github", "get_file_contents", call_tool, MagicMock(), lambda: False,
                                     MagicMock(), cache=cache)
        arguments = {"owner": "nixxs", "repo": "cora", "path": "app.py"}
        first = await handler.result(arguments)
        self.assertEqual((first["page"], first["pages"]), (1, 2))
        # The page argument is Cora's; the server never sees it.
        second = await handler.result({**arguments, PAGE_ARGUMENT: 2})
        self.assertEqual(second["page"], 2)
        call_tool.assert_awaited_once_with("github", "get_file_contents", arguments)
        await handler.result(arguments)
        self.assertEqual(call_tool.await_count, 2)
        now[0] = CACHE_SECONDS + 1
        await handler.result({**arguments, PAGE_ARGUMENT: 2})
        self.assertEqual(call_tool.await_count, 3)

    async def test_a_bad_page_gets_an_error_without_calling_the_service(self):
        call_tool = AsyncMock()
        handler = ServiceToolHandler("github", "get_me", call_tool, MagicMock(), lambda: False, MagicMock())
        for page in (0, -1, "2", True, 1.5):
            with self.subTest(page=page):
                result = await handler.result({PAGE_ARGUMENT: page})
                self.assertEqual(result["status"], "error")
        call_tool.assert_not_called()

    async def test_a_server_with_its_own_result_page_argument_gets_it_passed_through(self):
        call_tool = AsyncMock(return_value=SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")], structured_content=None, is_error=False,
        ))
        handler = ServiceToolHandler("github", "odd", call_tool, MagicMock(), lambda: False, MagicMock(),
                                     paged=False)
        await handler.result({PAGE_ARGUMENT: "abc"})
        call_tool.assert_awaited_once_with("github", "odd", {PAGE_ARGUMENT: "abc"})

    def test_the_cache_keeps_only_the_most_recent_replies(self):
        cache = ResultCache(clock=lambda: 0.0)
        for index in range(CACHE_ENTRIES + 2):
            cache.put(ResultCache.key("github", "t", {"i": index}), str(index), False)
        self.assertEqual(len(cache.entries), CACHE_ENTRIES)
        self.assertIsNone(cache.get(ResultCache.key("github", "t", {"i": 0})))
        self.assertEqual(cache.get(ResultCache.key("github", "t", {"i": CACHE_ENTRIES + 1})),
                         (str(CACHE_ENTRIES + 1), False))

    async def test_a_failed_call_still_answers_with_an_error_and_logs_only_the_type(self):
        connection = MagicMock()
        connection.response.item.create = AsyncMock()
        connection.response.create = AsyncMock()
        call_tool = AsyncMock(side_effect=TimeoutError("query 'private words'"))
        log = MagicMock()
        handler = ServiceToolHandler("github", "search_issues", call_tool, lambda: connection, lambda: False, log)
        await handler.handle("call_1", {"query": "private words"})
        output = json.loads(connection.response.item.create.call_args.kwargs["item"]["output"])
        self.assertEqual(output["status"], "error")
        connection.response.create.assert_awaited_once()
        self.assertNotIn("private words", repr(log.call_args_list))
        self.assertIn("TimeoutError", repr(log.call_args_list))

    async def test_a_closing_app_or_new_connection_gets_no_answer(self):
        connection = MagicMock()
        connection.response.item.create = AsyncMock()
        handler = ServiceToolHandler("github", "get_me", AsyncMock(), lambda: connection, lambda: True, MagicMock())
        await handler.handle("call_1", {})
        connection.response.item.create.assert_not_called()


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

    def test_tools_are_off_until_switched_on_and_switching_twice_is_harmless(self):
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM public.service_tools"))
        self.assertEqual(self.store.enabled_tools(), {})
        self.store.set_tool_enabled("github", "search_issues", True)
        self.store.set_tool_enabled("github", "search_issues", True)
        self.store.set_tool_enabled("github", "get_me", True)
        self.assertEqual(self.store.enabled_tools(), {"github": {"search_issues", "get_me"}})
        self.store.set_tool_enabled("github", "get_me", False)
        self.store.set_tool_enabled("github", "get_me", False)
        self.assertEqual(self.store.enabled_tools(), {"github": {"search_issues"}})
        with self.assertRaises(ValueError):
            self.store.set_tool_enabled("gmail", "search", True)

    def test_a_service_dropped_from_the_catalogue_is_ignored(self):
        with self.engine.begin() as connection:
            connection.execute(text("INSERT INTO public.service_settings (service, enabled) VALUES ('old', true)"))
        self.assertEqual(self.store.enabled_services(), set())


class ServicesPageTests(unittest.IsolatedAsyncioTestCase):
    async def open_services(self, app, pilot, enabled=frozenset(), mock_connections=True):
        app.persistence.load_models.return_value = MODELS
        app.persistence.load_enabled_services.return_value = set(enabled)
        app.persistence.load_service_tools.return_value = {}
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

    async def test_tool_rows_follow_the_connection_and_only_read_only_tools_switch(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.open_services(app, pilot, mock_connections=False)
            heading = app.screen.query_one("#service-tools-heading-github", Static)
            self.assertIn("Tools appear here once connected.", str(heading.render()))
            open_client, _ = fake_open()
            app.services.open_client = open_client
            app.services.start("github", TOKEN)
            # The page refreshes twice a second; wait for the rows rather than a fixed time.
            await until(pilot, lambda: len(app.screen.query(".tool-label")) == 3)
            status = str(app.screen.query_one("#service-status-github", Static).render())
            self.assertIn("connected · 3 tools", status)
            self.assertIn("0 of 3 tools on", str(heading.render()))
            labels = [str(label.render()) for label in app.screen.query(".tool-label").results(Static)]
            # Read-only first, then tools that ask the person each time.
            self.assertTrue(labels[0].startswith("search_issues  read-only"))
            self.assertIn("changes things · asks you first", labels[1])
            switches = list(app.screen.query("#service-tools-github Switch").results(Switch))
            self.assertEqual([s.disabled for s in switches], [False, False, False])

            app.persistence.save_service_tool.reset_mock()
            switches[0].value = True
            # The connection's worker stays open, so wait for the save rather than every worker.
            await until(pilot, lambda: app.persistence.save_service_tool.called and app.service_tool_choices)
            app.persistence.save_service_tool.assert_called_once_with("github", "search_issues", True)
            self.assertEqual(app.service_tool_choices, {"github": {"search_issues"}})
            self.assertIn("github__search_issues", app.tools.names())
            self.assertTrue(app.service_tools_changed)
            await until(pilot, lambda: "1 of 3 tools on" in str(heading.render()))
            self.assertIn("1 of 3 tools on", str(heading.render()))
            app.services.stop_all()
            self.assertNotIn("github__search_issues", app.tools.names())

    async def test_a_tool_that_changes_things_is_registered_to_ask_first(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.service_tool_choices = {"github": {"search_issues", "create_issue"}}
            open_client, _ = fake_open()
            app.services.open_client = open_client
            app.services.start("github", TOKEN)
            await pilot.pause(0.3)
            handlers = {name: tool.handle.__self__ for name, tool in app.tools._tools.items()
                        if name.startswith("github__")}
            self.assertIsNone(handlers["github__search_issues"].confirm)
            self.assertEqual(handlers["github__create_issue"].confirm, app.confirmations.ask)
            app.services.stop_all()

    async def wait_for_screen(self, app, pilot, showing):
        """Pushing and dismissing screens takes a few frames, more under load."""
        for _ in range(30):
            if isinstance(app.screen, ConfirmScreen) == showing:
                return
            await pilot.pause(0.1)
        self.fail(f"pop-up {'never appeared' if showing else 'never went away'}")

    async def test_the_pop_up_shows_the_request_and_the_answer_reaches_the_tool(self):
        async with keyboard_tests.running_app() as (app, pilot):
            asked = asyncio.ensure_future(app.confirmations.ask("github", "create_issue", {"title": "Standby bug"}))
            await self.wait_for_screen(app, pilot, True)
            self.assertIn('"title": "Standby bug"', app.screen.query_one("#confirm-arguments", TextArea).text)
            # Focus lands a frame after the pop-up mounts; under load that can take a moment.
            deny = app.screen.query_one("#confirm-deny")
            await until(pilot, lambda: deny.has_focus)
            self.assertTrue(deny.has_focus)
            # Typing letters does nothing: there are no shortcuts to approve.
            await pilot.press("y", "e", "s", "enter")
            self.assertEqual(await asyncio.wait_for(asked, 5), DECLINED)
            await self.wait_for_screen(app, pilot, False)

            asked = asyncio.ensure_future(app.confirmations.ask("github", "create_issue", {}))
            await self.wait_for_screen(app, pilot, True)
            await pilot.click("#confirm-approve")
            self.assertEqual(await asyncio.wait_for(asked, 5), APPROVED)
            await self.wait_for_screen(app, pilot, False)

            asked = asyncio.ensure_future(app.confirmations.ask("github", "create_issue", {}))
            await self.wait_for_screen(app, pilot, True)
            await pilot.press("escape")
            self.assertEqual(await asyncio.wait_for(asked, 5), DECLINED)
            self.assertIn("GitHub tool create_issue is waiting for your approval.", "\n".join(app.activity.entries))

    async def test_an_unanswered_pop_up_times_out_and_goes_away(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.confirmations.timeout = 2
            asked = asyncio.ensure_future(app.confirmations.ask("github", "create_issue", {}))
            # Showing is asynchronous too.
            for _ in range(10):
                if isinstance(app.screen, ConfirmScreen):
                    break
                await pilot.pause(0.1)
            self.assertIsInstance(app.screen, ConfirmScreen)
            self.assertEqual(await asyncio.wait_for(asked, 5), TIMED_OUT)
            # Dismissing is asynchronous; under load it can take a few frames.
            for _ in range(20):
                if not isinstance(app.screen, ConfirmScreen):
                    break
                await pilot.pause(0.1)
            self.assertNotIsInstance(app.screen, ConfirmScreen)


    async def test_closing_settings_after_a_tool_change_reconnects_an_active_live_once(self):
        cases = [  # changed, ready, standby, reconnects
            (True, True, False, True), (False, True, False, False),
            (True, False, False, False), (True, True, True, False),
        ]
        for changed, ready, standby, reconnects in cases:
            with self.subTest(changed=changed, ready=ready, standby=standby):
                async with keyboard_tests.running_app() as (app, pilot):
                    await self.open_services(app, pilot)
                    app.service_tools_changed = changed
                    app.live.ready = ready
                    app.standby = standby
                    with patch.object(type(app), "action_reconnect") as reconnect:
                        await pilot.press("escape")
                        await pilot.pause()
                    self.assertEqual(reconnect.called, reconnects)


class ConfirmationQueueTests(unittest.IsolatedAsyncioTestCase):
    def queue(self, timeout=1):
        self.shown, self.closed, self.activity = [], [], []
        return Confirmations(self.shown.append, self.closed.append,
                             lambda message, summary: self.activity.append(summary), timeout=timeout)

    async def test_requests_are_shown_one_at_a_time_in_order(self):
        queue = self.queue()
        first = asyncio.ensure_future(queue.ask("github", "create_issue", {"n": 1}))
        second = asyncio.ensure_future(queue.ask("github", "add_comment", {"n": 2}))
        await settle()
        self.assertEqual([r.tool for r in self.shown], ["create_issue"])
        queue.answer(self.shown[0].id, True)
        self.assertEqual(await first, APPROVED)
        await settle()
        self.assertEqual([r.tool for r in self.shown], ["create_issue", "add_comment"])
        self.assertEqual(self.closed, [self.shown[0]])
        queue.answer(self.shown[1].id, False)
        self.assertEqual(await second, DECLINED)
        self.assertEqual(self.activity[-1], "GitHub · add_comment declined")

    async def test_answers_only_count_once_and_only_for_their_request(self):
        queue = self.queue()
        asked = asyncio.ensure_future(queue.ask("github", "create_issue", {}))
        await settle()
        request = self.shown[0]
        queue.answer(request.id + 1000, True)
        queue.answer(request.id, False)
        queue.answer(request.id, True)
        self.assertEqual(await asked, DECLINED)

    async def test_no_answer_times_out_and_closing_declines_everything(self):
        queue = self.queue(timeout=0.05)
        self.assertEqual(await queue.ask("github", "create_issue", {}), TIMED_OUT)
        queue = self.queue()
        asked = [asyncio.ensure_future(queue.ask("github", "create_issue", {})) for _ in range(2)]
        await settle()
        queue.decline_all()
        self.assertEqual(await asyncio.gather(*asked), [DECLINED, DECLINED])
        self.assertEqual(queue.waiting, [])

    async def test_a_declined_or_timed_out_tool_never_calls_the_service(self):
        call_tool = AsyncMock()
        for outcome in (DECLINED, TIMED_OUT):
            with self.subTest(outcome=outcome):
                handler = ServiceToolHandler("github", "create_issue", call_tool, MagicMock(), lambda: False,
                                             MagicMock(), confirm=AsyncMock(return_value=outcome))
                result = await handler.result({"title": "x"})
                self.assertEqual(result["status"], "not_done")
                self.assertIn("not done", result["message"])
        call_tool.assert_not_called()

    async def test_an_approved_tool_calls_the_service_once_with_what_was_shown(self):
        call_tool = AsyncMock(return_value=SimpleNamespace(
            content=[SimpleNamespace(type="text", text="created #24")], structured_content=None, is_error=False,
        ))
        confirm = AsyncMock(return_value=APPROVED)
        handler = ServiceToolHandler("github", "create_issue", call_tool, MagicMock(), lambda: False,
                                     MagicMock(), confirm=confirm)
        result = await handler.result({"title": "x", PAGE_ARGUMENT: 1})
        # The page argument is Cora's own, so it is neither shown for approval nor sent.
        confirm.assert_awaited_once_with("github", "create_issue", {"title": "x"})
        call_tool.assert_awaited_once_with("github", "create_issue", {"title": "x"})
        self.assertEqual(result["output_untrusted"], "created #24")

    async def test_one_reminder_while_still_waiting_and_none_for_requests_not_yet_shown(self):
        reminded = []
        queue = Confirmations(MagicMock(), MagicMock(), MagicMock(), timeout=0.3,
                              remind=reminded.append, remind_after=0.1)
        first = asyncio.ensure_future(queue.ask("github", "create_issue", {}))
        second = asyncio.ensure_future(queue.ask("github", "add_comment", {}))
        self.assertEqual(await first, TIMED_OUT)
        # Only the request on screen was reminded about, and only once.
        self.assertEqual([request.tool for request in reminded], ["create_issue"])
        second.cancel()

    async def test_an_answer_before_the_reminder_means_no_reminder(self):
        reminded = []
        shown = []
        queue = Confirmations(shown.append, MagicMock(), MagicMock(), timeout=1,
                              remind=reminded.append, remind_after=0.2)
        asked = asyncio.ensure_future(queue.ask("github", "create_issue", {}))
        await settle()
        queue.answer(shown[0].id, True)
        self.assertEqual(await asked, APPROVED)
        await asyncio.sleep(0.3)
        self.assertEqual(reminded, [])


class ApprovalNoteTests(unittest.IsolatedAsyncioTestCase):
    async def test_cora_is_told_when_the_pop_up_opens(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.live.send_approval_waiting = AsyncMock()
            asked = asyncio.ensure_future(app.confirmations.ask("github", "create_issue", {"title": "x"}))
            await pilot.pause()
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            app.live.send_approval_waiting.assert_awaited_once_with("GitHub", "create_issue", reminder=False,
                                                                    on_behalf=None)
            await pilot.press("escape")
            await asyncio.wait_for(asked, 1)

    async def test_one_she_didnt_make_is_named_as_such_and_she_hears_how_it_ended(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.live.send_approval_waiting = AsyncMock()
            app.live.send_approval_outcome = AsyncMock()
            app.live.connection = MagicMock()
            app.live.ready = True
            behalf = "Claude Code (working on ComboDots for them)"
            asked = asyncio.ensure_future(app.confirmations.ask(
                "claude_code", "PowerShell", {"command": "ls"}, title="Claude Code · ComboDots", on_behalf=behalf))
            for _ in range(30):
                if isinstance(app.screen, ConfirmScreen):
                    break
                await pilot.pause(0.1)
            await pilot.click("#confirm-approve")
            await asyncio.wait_for(asked, 1)
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            app.live.send_approval_waiting.assert_awaited_once_with(
                "Claude Code · ComboDots", "PowerShell", reminder=False, on_behalf=behalf)
            app.live.send_approval_outcome.assert_awaited_once_with("PowerShell", "approved", behalf)
            # Not hers to chase.
            app.remind_cora_approval_waiting(SimpleNamespace(on_behalf=behalf, title="Claude Code · ComboDots",
                                                             tool="PowerShell"))
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            self.assertEqual(app.live.send_approval_waiting.await_count, 1)

    async def test_the_notes_name_the_tool_ask_for_a_click_and_never_carry_arguments(self):
        from live import CoraLive
        live = CoraLive()
        live.connection = MagicMock()
        live.ready = True
        live.append_commentary = AsyncMock()
        await live.send_approval_waiting("GitHub", "create_issue")
        await live.send_approval_waiting("GitHub", "create_issue", reminder=True)
        first, reminder = (call.args[2] for call in live.append_commentary.await_args_list)
        for note in (first, reminder):
            self.assertIn("GitHub tool create_issue", note)
            self.assertIn("Approve or Deny", note)
        self.assertIn("nothing said aloud approves it", first)
        self.assertIn("remind them once", reminder)

    async def test_notes_for_someone_elses_request_say_it_isnt_hers_and_how_it_ended(self):
        from live import CoraLive
        live = CoraLive()
        live.connection = MagicMock()
        live.ready = True
        live.append_commentary = AsyncMock()
        behalf = "Claude Code (working on ComboDots for them)"
        await live.send_approval_waiting("Claude Code · ComboDots", "PowerShell", on_behalf=behalf)
        await live.send_approval_outcome("PowerShell", "approved", behalf)
        await live.send_approval_outcome("PowerShell", "timed out", behalf)
        waiting, approved, lapsed = (call.args[2] for call in live.append_commentary.await_args_list)
        self.assertIn(f"{behalf} wants to use PowerShell", waiting)
        self.assertIn("isn't your own request", waiting)
        self.assertIn("The person approved it", approved)
        self.assertIn("no longer waiting", approved)
        self.assertIn("Nobody answered in time", lapsed)
