"""Cora's own Signal account: the signal-cli client, linking her phone, settings and the Signal row. No real Signal."""

import asyncio
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx2
import psycopg
from psycopg import sql
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from textual.widgets import Button, Input, Static, Switch, TabbedContent

from messaging import container
from messaging.qr import qr_text
from messaging.setup import FAILED, LINKED, LINKING, NOT_LINKED, OFF, SignalSetup, SignalStatus
from messaging.signal_cli import SignalClient, SignalError
from migrate import apply_migrations, database_settings
from session_config import BACKEND_MODEL, MODEL_AREAS
from storage.database import build_engine
from storage.signal_settings_store import SignalSettings, SignalSettingsStore, normalise_number
from ui.model_settings import ModelSettingsScreen
import test_keyboard_persistence as keyboard_tests

CORA = "+61400000001"
NICK = "+61400000002"
LINK = "sgnl://linkdevice?uuid=abc&pub_key=def"


class NumberTests(unittest.TestCase):
    def test_numbers_are_international_and_compacted(self):
        self.assertEqual(normalise_number(" +61 400 000-001 "), CORA)
        self.assertEqual(normalise_number("+1 (555) 010-9999"), "+15550109999")
        for bad in ("0400000001", "61400000001", "+0400000001", "+61", "+61400abc001", ""):
            with self.subTest(number=bad), self.assertRaises(ValueError):
                normalise_number(bad)


class SignalClientTests(unittest.IsolatedAsyncioTestCase):
    def client(self, handler):
        self.requests = []

        def record(request):
            self.requests.append(request)
            return handler(request)

        return SignalClient("http://signal.test", transport=httpx2.MockTransport(record))

    @staticmethod
    def result(value):
        return lambda request: httpx2.Response(200, json={"jsonrpc": "2.0", "id": "1", "result": value})

    async def test_check_is_true_only_when_the_daemon_answers(self):
        self.assertTrue(await self.client(lambda request: httpx2.Response(200)).check())
        self.assertFalse(await self.client(lambda request: httpx2.Response(503)).check())

        def refuse(request):
            raise httpx2.ConnectError("refused")
        self.assertFalse(await self.client(refuse).check())

    async def test_calls_are_json_rpc_and_errors_raise(self):
        client = self.client(self.result([{"number": CORA}, {"other": True}]))
        self.assertEqual(await client.accounts(), [CORA])
        body = json.loads(self.requests[0].content)
        self.assertEqual((body["method"], self.requests[0].url.path), ("listAccounts", "/api/v1/rpc"))
        self.assertNotIn("params", body)
        failing = self.client(lambda request: httpx2.Response(
            200, json={"jsonrpc": "2.0", "id": "1", "error": {"code": -1, "message": "Not linked"}},
        ))
        with self.assertRaisesRegex(SignalError, "Not linked"):
            await failing.accounts()

    async def test_linking_sends_the_link_address_and_device_name(self):
        client = self.client(self.result({"deviceLinkUri": LINK}))
        self.assertEqual(await client.start_link(), LINK)
        client = self.client(self.result({"number": CORA}))
        self.assertEqual(await client.finish_link(LINK, "Cora", timeout=5), CORA)
        self.assertEqual(json.loads(self.requests[0].content)["params"], {"deviceLinkUri": LINK, "deviceName": "Cora"})
        await client.delete_account(NICK)
        self.assertEqual(json.loads(self.requests[1].content)["params"], {"account": NICK, "ignoreRegistered": True})


class FakeClient:
    def __init__(self, accounts=(), linked=CORA, ready=True):
        self.linked_accounts = list(accounts)
        self.linked = linked
        self.ready = ready
        self.scanned = asyncio.Event()
        self.deleted = []

    async def check(self):
        return self.ready

    async def accounts(self):
        return self.linked_accounts

    async def start_link(self):
        return LINK

    async def finish_link(self, uri, name, timeout):
        await asyncio.wait_for(self.scanned.wait(), timeout)
        return self.linked

    async def delete_account(self, number):
        self.deleted.append(number)


class SignalSetupTests(unittest.IsolatedAsyncioTestCase):
    def setup(self, client, **options):
        self.saved = []
        self.activity = []
        self.started = AsyncMock()
        self.stopped = AsyncMock()

        async def save(account):
            self.saved.append(account)

        return SignalSetup(client, self.started, self.stopped, save,
                           lambda message, summary: self.activity.append(summary),
                           ready_seconds=0.2, link_seconds=1, poll_seconds=0.01, **options)

    async def test_starting_finds_the_saved_account_still_linked(self):
        setup = self.setup(FakeClient(accounts=[CORA]))
        await setup.start(CORA)
        self.started.assert_awaited_once()
        self.assertEqual(setup.status, SignalStatus(LINKED, account=CORA))
        self.assertEqual(self.saved, [])

    async def test_a_saved_account_no_longer_there_is_forgotten(self):
        setup = self.setup(FakeClient(accounts=[]))
        await setup.start(CORA)
        self.assertEqual(setup.status.state, NOT_LINKED)
        self.assertEqual(self.saved, [None])
        self.assertIn("Signal unlinked", self.activity)

    async def test_a_daemon_that_never_answers_fails(self):
        setup = self.setup(FakeClient(ready=False))
        await setup.start(None)
        self.assertEqual(setup.status.state, FAILED)
        self.assertIn("TimeoutError", setup.status.reason)

    async def test_linking_shows_the_code_then_saves_coras_number(self):
        client = FakeClient()
        setup = self.setup(client)
        await setup.start(None)
        linking = asyncio.ensure_future(setup.link(NICK))
        await asyncio.sleep(0.01)
        self.assertEqual(setup.status, SignalStatus(LINKING, link_uri=LINK))
        client.scanned.set()
        await linking
        self.assertEqual(setup.status, SignalStatus(LINKED, account=CORA))
        self.assertEqual(self.saved, [CORA])

    async def test_scanning_with_the_persons_own_phone_is_undone(self):
        client = FakeClient(linked=NICK)
        client.scanned.set()
        setup = self.setup(client)
        await setup.start(None)
        await setup.link(NICK)
        self.assertEqual(client.deleted, [NICK])
        self.assertEqual(self.saved, [])
        self.assertEqual(setup.status.state, NOT_LINKED)
        self.assertIn("your own phone", setup.status.reason)
        self.assertIn("Wrong phone linked", self.activity)

    async def test_a_code_not_scanned_in_time_can_be_tried_again(self):
        setup = self.setup(FakeClient())
        await setup.start(None)
        await setup.link(NICK)
        self.assertEqual(setup.status.state, NOT_LINKED)
        self.assertIn("wasn't scanned in time", setup.status.reason)

    async def test_stopping_while_linking_wins_over_a_late_link(self):
        client = FakeClient()
        setup = self.setup(client)
        await setup.start(None)
        linking = asyncio.ensure_future(setup.link(NICK))
        await asyncio.sleep(0.01)
        await setup.stop()
        client.scanned.set()
        await linking
        self.stopped.assert_awaited_once()
        self.assertEqual(setup.status.state, OFF)

    async def test_linking_only_starts_when_ready_and_not_linked(self):
        setup = self.setup(FakeClient(accounts=[CORA]))
        await setup.link(NICK)
        self.assertEqual(setup.status.state, OFF)
        await setup.start(CORA)
        await setup.link(NICK)
        self.assertEqual(setup.status.state, LINKED)


class QrTests(unittest.TestCase):
    def test_two_rows_per_line_dark_on_light(self):
        drawn = qr_text(LINK)
        lines = drawn.plain.split("\n")
        self.assertEqual(len({len(line) for line in lines}), 1)
        self.assertTrue(set(drawn.plain) <= set("█▀▄ \n"))
        self.assertEqual(str(drawn.style), "#000000 on #ffffff")
        # The light border is kept on every side, including the last line's end.
        self.assertTrue(lines[0].strip() == "" and lines[-1].endswith("  "))
        # 21x21 modules at the least, plus a two-module quiet border.
        self.assertGreaterEqual(len(lines[0]), 25)


class ContainerTests(unittest.TestCase):
    def test_compose_runs_with_the_signal_profile_and_reports_dockers_last_line(self):
        with patch("messaging.container.subprocess.run") as run, \
                patch.object(container, "ENV_FILE", MagicMock(exists=lambda: True, __str__=lambda self: ".env.database")):
            run.return_value = SimpleNamespace(returncode=0, stderr="")
            container._compose("up", "-d", "signal")
            command = run.call_args.args[0]
            self.assertEqual(command[:2], ["docker", "compose"])
            self.assertIn("--profile", command)
            self.assertEqual(command[-3:], ["up", "-d", "signal"])
            run.return_value = SimpleNamespace(returncode=1, stderr="pulling\nError: port is already allocated\n")
            with self.assertRaisesRegex(container.ContainerError, "port is already allocated"):
                container._compose("up", "-d", "signal")

    def test_without_the_database_settings_nothing_runs(self):
        with patch("messaging.container.subprocess.run") as run, \
                patch.object(container, "ENV_FILE", MagicMock(exists=lambda: False)):
            with self.assertRaises(container.ContainerError):
                container._compose("up", "-d", "signal")
            run.assert_not_called()


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class SignalSettingsStoreDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        settings = database_settings()
        admin = psycopg.connect(**settings, autocommit=True)
        cls.addClassCleanup(admin.close)
        cls.database = "cora_test_signal_settings_" + uuid4().hex
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.database)))
        cls.addClassCleanup(admin.execute, sql.SQL("DROP DATABASE {}").format(sql.Identifier(cls.database)))
        with psycopg.connect(**{**settings, "dbname": cls.database}, autocommit=True) as connection:
            apply_migrations(connection)
        with patch("storage.database.database_settings", return_value={**settings, "dbname": cls.database}):
            cls.engine = build_engine()
        cls.addClassCleanup(cls.engine.dispose)

    def setUp(self):
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM public.signal_settings"))
        self.store = SignalSettingsStore(self.engine)

    def test_off_and_empty_until_set_up(self):
        self.assertEqual(self.store.settings(), SignalSettings())
        self.store.set_allowed_number("+61 400 000 002")
        self.store.set_enabled(True)
        self.store.set_account(CORA)
        self.assertEqual(self.store.settings(), SignalSettings(True, CORA, NICK))
        self.store.set_account(None)
        self.assertEqual(self.store.settings().account, None)

    def test_coras_number_and_the_persons_can_never_be_the_same(self):
        self.store.set_allowed_number(NICK)
        with self.assertRaisesRegex(ValueError, "your number"):
            self.store.set_account(NICK)
        self.store.set_account(CORA)
        with self.assertRaisesRegex(ValueError, "Cora's own number"):
            self.store.set_allowed_number(CORA)
        with self.engine.begin() as connection, self.assertRaises(IntegrityError):
            connection.execute(text(f"UPDATE public.signal_settings SET account = '{NICK}'"))


class SignalRowTests(unittest.IsolatedAsyncioTestCase):
    async def open_signal(self, app, pilot, settings=SignalSettings()):
        app.persistence.load_models.return_value = {area: BACKEND_MODEL for area in MODEL_AREAS}
        app.persistence.load_signal_settings.return_value = settings
        await pilot.click("#settings-button")
        await asyncio.wait_for(app.workers.wait_for_complete(), 3)
        await pilot.pause()
        self.assertIsInstance(app.screen, ModelSettingsScreen)
        app.screen.query_one(TabbedContent).active = "settings-services"
        await pilot.pause()
        await pilot.click("#service-title-signal")
        await pilot.pause()

    async def test_the_switch_starts_and_stops_signal(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.signal.start = AsyncMock()
            app.signal.stop = AsyncMock()
            await self.open_signal(app, pilot)
            self.assertIn("off", str(app.screen.query_one("#signal-status", Static).render()))
            app.screen.query_one("#signal-enabled", Switch).value = True
            await pilot.pause()
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            app.persistence.save_signal_enabled.assert_called_once_with(True)
            app.signal.start.assert_awaited_once_with(None)
            app.screen.query_one("#signal-enabled", Switch).value = False
            await pilot.pause()
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            app.signal.stop.assert_awaited_once()

    async def test_your_number_is_checked_and_saved(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.open_signal(app, pilot)
            field = app.screen.query_one("#signal-person", Input)
            field.value = "0400 000 002"
            with patch.object(app.screen, "notify") as notify:
                await pilot.click("#signal-save-number")
                await pilot.pause()
            self.assertIn("international", notify.call_args.args[0])
            app.persistence.save_signal_allowed_number.assert_not_called()
            field.value = "+61 400 000 002"
            await pilot.click("#signal-save-number")
            await pilot.pause()
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            app.persistence.save_signal_allowed_number.assert_called_once_with(NICK)
            self.assertEqual(field.value, NICK)
            self.assertEqual(app.signal_settings.allowed_number, NICK)

    async def test_link_needs_your_number_and_shows_the_code_while_waiting(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.signal.status = SignalStatus(NOT_LINKED)
            await self.open_signal(app, pilot, SignalSettings(enabled=True))
            link = app.screen.query_one("#signal-link", Button)
            self.assertTrue(link.disabled)
            app.screen.query_one("#signal-person", Input).value = NICK
            await pilot.click("#signal-save-number")
            await pilot.pause(0.6)
            self.assertFalse(link.disabled)
            app.signal.link = AsyncMock()
            await pilot.click("#signal-link")
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            app.signal.link.assert_awaited_once_with(NICK)
            qr = app.screen.query_one("#signal-qr", Static)
            self.assertFalse(qr.display)
            app.signal.status = SignalStatus(LINKING, link_uri=LINK)
            await pilot.pause(0.6)
            self.assertTrue(qr.display)
            self.assertIn("▀", str(qr.render()))
            self.assertIn("waiting for Cora's phone", str(app.screen.query_one("#signal-status", Static).render()))
            app.signal.status = SignalStatus(LINKED, account=CORA)
            await pilot.pause(0.6)
            self.assertFalse(qr.display)
            self.assertIn(f"Cora's number · {CORA}", str(app.screen.query_one("#signal-status", Static).render()))

    async def test_coras_own_number_is_refused_as_yours(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.signal.status = SignalStatus(LINKED, account=CORA)
            await self.open_signal(app, pilot, SignalSettings(enabled=True, account=CORA))
            app.screen.query_one("#signal-person", Input).value = CORA
            with patch.object(app.screen, "notify") as notify:
                await pilot.click("#signal-save-number")
                await pilot.pause()
            self.assertIn("Cora's own number", notify.call_args.args[0])
            app.persistence.save_signal_allowed_number.assert_not_called()

    async def test_launch_starts_signal_only_when_it_was_switched_on(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                async with keyboard_tests.running_app() as (app, pilot):
                    app.persistence.load_signal_settings.return_value = SignalSettings(enabled, CORA, NICK)
                    app.signal.start = AsyncMock()
                    await app.start_signal_if_enabled().wait()
                    self.assertEqual(app.signal.start.await_count, int(enabled))
