"""The web interface (WB1): pairing codes, the API that only paired devices reach, and the device list."""

from datetime import datetime, timezone
import json
import os
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

import httpx2 as httpx
import psycopg
from psycopg import sql

from migrate import apply_migrations, database_settings
from server.listener import build_app
from storage.database import build_engine
from storage.web_device_store import WebDevice, WebDeviceStore, device_name
from storage.webhook_settings_store import WebhookSettings, WebhookSettingsStore
from web.pairing import (
    CODE_ALPHABET, CODE_ATTEMPTS, CODE_SECONDS, NO_CODE, PAIRED, WRONG, Pairing, shown_code, token_hash, typed_code,
)
from web.routes import COOKIE, WebAccess, web_routes


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class PairingTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.pairing = Pairing(self.clock)

    def test_no_code_is_open_until_settings_opens_one(self):
        self.assertIsNone(self.pairing.current())
        self.assertEqual(self.pairing.try_code("ANYTHING"), NO_CODE)

    def test_a_code_is_eight_easy_characters_shown_in_two_halves(self):
        code = self.pairing.start()
        self.assertEqual(len(code), 8)
        self.assertTrue(set(code) <= set(CODE_ALPHABET))
        self.assertFalse(set("0O1IL5S") & set(CODE_ALPHABET))
        self.assertEqual(shown_code("K7QFM2XP"), "K7QF-M2XP")

    def test_case_spaces_and_dashes_dont_matter_and_it_works_once(self):
        code = self.pairing.start()
        self.assertEqual(typed_code(f" {code[:4].lower()} - {code[4:]} "), code)
        self.assertEqual(self.pairing.try_code(f"{code[:4].lower()}-{code[4:]}"), PAIRED)
        self.assertEqual(self.pairing.try_code(code), NO_CODE)

    def test_wrong_guesses_cancel_the_code(self):
        code = self.pairing.start()
        for _ in range(CODE_ATTEMPTS - 1):
            self.assertEqual(self.pairing.try_code("WRONGONE"), WRONG)
        self.assertIsNotNone(self.pairing.current())
        self.assertEqual(self.pairing.try_code("WRONGONE"), WRONG)
        # Even the right code is too late now.
        self.assertEqual(self.pairing.try_code(code), NO_CODE)

    def test_a_code_expires_and_a_new_one_replaces_the_old(self):
        self.pairing.start()
        self.clock.now += CODE_SECONDS - 1
        self.assertEqual(self.pairing.seconds_left(), 1)
        second = self.pairing.start()
        self.assertEqual(self.pairing.seconds_left(), CODE_SECONDS)
        self.clock.now += CODE_SECONDS
        self.assertEqual(self.pairing.try_code(second), NO_CODE)
        self.assertEqual(self.pairing.seconds_left(), 0)

    def test_cancelling_closes_it(self):
        code = self.pairing.start()
        self.pairing.cancel()
        self.assertEqual(self.pairing.try_code(code), NO_CODE)

    def test_only_the_tokens_hash_is_kept(self):
        self.assertEqual(token_hash("abc"), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")


class WebRoutesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.pairing = Pairing()
        self.devices: dict[str, WebDevice] = {}
        self.on_paired = MagicMock()

        async def add_device(name, hashed):
            now = datetime.now(timezone.utc)
            self.devices[hashed] = WebDevice(uuid4(), device_name(name), now, now)
            return self.devices[hashed]

        async def find_device(hashed):
            return self.devices.get(hashed)

        access = WebAccess(self.pairing, add_device, find_device, self.on_paired)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(web_routes(access))),
                                        base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def pair(self, code, name="iPhone"):
        return await self.client.post("/api/pair", json={"code": code, "name": name})

    async def test_the_pages_files_are_checked_before_reuse_so_updates_show(self):
        for path in ("/", "/style.css", "/main.js"):
            answer = await self.client.get(path)
            self.assertEqual((answer.status_code, answer.headers["cache-control"]), (200, "no-cache"))
            # Unchanged: a cheap 304, still marked for checking next time.
            again = await self.client.get(path, headers={"If-None-Match": answer.headers["etag"]})
            self.assertEqual((again.status_code, again.headers["cache-control"]), (304, "no-cache"))
        self.assertEqual((await self.client.get("/missing.css")).status_code, 404)

    async def test_an_unpaired_browser_gets_the_page_but_not_the_api(self):
        self.assertEqual((await self.client.get("/")).status_code, 200)
        answer = await self.client.get("/api/session")
        self.assertEqual((answer.status_code, answer.json()), (401, {"paired": False}))
        self.assertEqual(answer.headers["cache-control"], "no-store")
        wrong = await self.client.get("/api/session", headers={"Cookie": f"{COOKIE}=made-up"})
        self.assertEqual(wrong.status_code, 401)

    async def test_pairing_needs_an_open_code_and_the_right_one(self):
        self.assertEqual((await self.pair("ABCD-EFGH")).status_code, 403)
        code = self.pairing.start()
        self.assertEqual((await self.pair("WRONG")).status_code, 403)
        self.assertEqual(self.devices, {})
        self.on_paired.assert_not_called()
        answer = await self.pair(shown_code(code))
        self.assertEqual((answer.status_code, answer.json()), (200, {"paired": True, "device": "iPhone"}))
        self.on_paired.assert_called_once()
        # Used up.
        self.assertEqual((await self.pair(code)).status_code, 403)

    async def test_the_cookie_is_locked_down_and_only_its_hash_is_kept(self):
        code = self.pairing.start()
        answer = await self.pair(code)
        cookie = answer.headers["set-cookie"]
        for part in ("HttpOnly", "Secure", "SameSite=strict", "Path=/"):
            self.assertIn(part.lower(), cookie.lower())
        token = cookie.split(";")[0].split("=", 1)[1]
        self.assertGreaterEqual(len(token), 40)
        self.assertEqual(list(self.devices), [token_hash(token)])
        self.assertNotIn(token, json.dumps([str(device) for device in self.devices.values()]))
        session = await self.client.get("/api/session", headers={"Cookie": f"{COOKIE}={token}"})
        self.assertEqual((session.status_code, session.json()), (200, {"paired": True, "device": "iPhone"}))
        # Unpaired in Settings: the same cookie no longer works.
        self.devices.clear()
        self.assertEqual((await self.client.get("/api/session", headers={"Cookie": f"{COOKIE}={token}"})).status_code, 401)

    async def test_only_small_json_is_taken(self):
        code = self.pairing.start()
        form = await self.client.post("/api/pair", data={"code": code})
        self.assertEqual(form.status_code, 400)
        big = await self.client.post("/api/pair", content=json.dumps({"code": code, "pad": "x" * 5000}),
                                     headers={"Content-Type": "application/json"})
        self.assertEqual(big.status_code, 400)
        not_object = await self.client.post("/api/pair", content="[1]", headers={"Content-Type": "application/json"})
        self.assertEqual(not_object.status_code, 400)
        # None of that counted as a guess.
        self.assertEqual((await self.pair(code)).status_code, 200)

    async def test_device_names_are_tidied(self):
        self.assertEqual(device_name("  iPad \n (Home Screen app) "), "iPad (Home Screen app)")
        self.assertEqual(device_name(""), "Browser")
        self.assertEqual(len(device_name("x" * 500)), 60)


class ListenerSettingsTests(unittest.TestCase):
    def test_the_listener_runs_for_webhooks_or_the_web_interface_once_there_is_an_address(self):
        self.assertFalse(WebhookSettings().listening)
        self.assertFalse(WebhookSettings(web_enabled=True).listening)
        self.assertTrue(WebhookSettings(public_url="https://cora.example.com", web_enabled=True).listening)
        self.assertTrue(WebhookSettings(enabled=True, public_url="https://cora.example.com").listening)
        self.assertFalse(WebhookSettings(public_url="https://cora.example.com").listening)


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class WebDeviceStoreDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        settings = database_settings()
        admin = psycopg.connect(**settings, autocommit=True)
        cls.addClassCleanup(admin.close)
        cls.database = "cora_test_web_devices_" + uuid4().hex
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.database)))
        cls.addClassCleanup(admin.execute, sql.SQL("DROP DATABASE {}").format(sql.Identifier(cls.database)))
        with psycopg.connect(**{**settings, "dbname": cls.database}, autocommit=True) as connection:
            apply_migrations(connection)
        with patch("storage.database.database_settings", return_value={**settings, "dbname": cls.database}):
            cls.engine = build_engine()
        cls.addClassCleanup(cls.engine.dispose)

    def test_devices_are_added_found_by_token_hash_and_removed(self):
        store = WebDeviceStore(self.engine)
        hashed = token_hash("a-long-random-token")
        added = store.add("  iPhone  ", hashed)
        self.assertEqual(added.name, "iPhone")
        found = store.find(hashed)
        self.assertEqual(found.id, added.id)
        self.assertGreaterEqual(found.last_seen_at, added.last_seen_at)
        self.assertIsNone(store.find(token_hash("another")))
        self.assertEqual([device.id for device in store.devices()], [added.id])
        store.remove(added.id)
        self.assertIsNone(store.find(hashed))
        self.assertEqual(store.devices(), [])

    def test_only_a_hash_fits_and_each_is_unique(self):
        store = WebDeviceStore(self.engine)
        with self.assertRaises(Exception):
            store.add("Laptop", "not-a-hash")
        hashed = token_hash("unique")
        store.add("Laptop", hashed)
        with self.assertRaises(Exception):
            store.add("Laptop again", hashed)

    def test_the_web_switch_is_saved_beside_the_webhook_settings(self):
        store = WebhookSettingsStore(self.engine)
        store.set_web_enabled(True)
        self.assertTrue(store.settings().web_enabled)
        self.assertFalse(store.settings().enabled)
