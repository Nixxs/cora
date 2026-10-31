"""Cora's webhook listener (T4a): only her own subscriptions, under a secret path, ever count."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
import socket
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx2 as httpx
import psycopg
from psycopg import sql
from sqlalchemy import text

from migrate import apply_migrations, database_settings
from storage.database import build_engine
from storage.webhook_settings_store import WebhookSettingsStore

from mail import webhook
from mail.account import READY, EmailStatus, GraphError, MailSubscription, Mailbox
from mail.webhook import BODY_MAX_BYTES, WebhookServer, mail_urls, webhook_app, webhook_secrets
from storage.webhook_settings_store import WebhookSettings, check_port, normalise_public_url
import test_keyboard_persistence as keyboard_tests
from test_email import FakeClient, FakeResponse

PATH, STATE = "path-secret", "state-secret"
EXPIRES = datetime(2026, 11, 2, 9, tzinfo=timezone.utc)


class WebhookAppTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.on_mail, self.on_lifecycle = MagicMock(), MagicMock()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=webhook_app(lambda: (PATH, STATE), self.on_mail, self.on_lifecycle)),
            base_url="http://test",
        )

    async def asyncTearDown(self):
        await self.client.aclose()

    async def post(self, path, items=None, content=None, **params):
        body = content if content is not None else json.dumps({"value": items or []})
        return await self.client.post(path, content=body, params=params, headers={"Content-Type": "application/json"})

    async def test_microsofts_handshake_is_echoed_as_plain_text_only_on_the_secret_path(self):
        answer = await self.post(f"/webhooks/{PATH}/mail", validationToken="abc <b>")
        self.assertEqual((answer.status_code, answer.text), (200, "abc <b>"))
        self.assertTrue(answer.headers["content-type"].startswith("text/plain"))
        self.assertEqual((await self.post(f"/webhooks/{PATH}/mail/lifecycle", validationToken="t")).text, "t")
        self.assertEqual((await self.post("/webhooks/wrong/mail", validationToken="abc")).status_code, 404)
        self.assertEqual((await self.post(f"/webhooks/{PATH}/mail", validationToken="x" * 2000)).status_code, 400)
        self.on_mail.assert_not_called()

    async def test_only_notifications_carrying_her_secret_count_and_once_per_call(self):
        ours = {"clientState": STATE, "changeType": "created"}
        answer = await self.post(f"/webhooks/{PATH}/mail", [ours, ours, {"clientState": "nope"}])
        self.assertEqual((answer.status_code, answer.text), (202, ""))
        self.on_mail.assert_called_once_with()
        self.on_mail.reset_mock()
        for items in ([{"clientState": "nope"}], [{}], [{"clientState": 7}], ["junk"]):
            self.assertEqual((await self.post(f"/webhooks/{PATH}/mail", items)).status_code, 202)
        self.assertEqual((await self.post(f"/webhooks/{PATH}/mail", content="not json")).status_code, 202)
        self.assertEqual((await self.post(f"/webhooks/{PATH}/mail", content='{"value": 3}')).status_code, 202)
        self.on_mail.assert_not_called()

    async def test_wrong_paths_methods_and_oversized_bodies_are_refused(self):
        ours = [{"clientState": STATE}]
        self.assertEqual((await self.post("/webhooks/other/mail", ours)).status_code, 404)
        self.assertEqual((await self.post("/", ours)).status_code, 404)
        self.assertEqual((await self.client.get(f"/webhooks/{PATH}/mail")).status_code, 405)
        big = json.dumps({"value": ours, "pad": "x" * BODY_MAX_BYTES})
        self.assertEqual((await self.post(f"/webhooks/{PATH}/mail", content=big)).status_code, 413)
        self.on_mail.assert_not_called()

    async def test_lifecycle_notes_are_passed_on_and_health_needs_the_path(self):
        await self.post(f"/webhooks/{PATH}/mail/lifecycle", [
            {"clientState": STATE, "lifecycleEvent": "missed"},
            {"clientState": "nope", "lifecycleEvent": "subscriptionRemoved"},
            {"clientState": STATE, "lifecycleEvent": "somethingElse"},
        ])
        self.on_lifecycle.assert_called_once_with("missed")
        self.on_mail.assert_not_called()
        self.assertEqual((await self.client.get(f"/webhooks/{PATH}/health")).text, "ok")
        self.assertEqual((await self.client.get("/webhooks/wrong/health")).status_code, 404)


class WebhookSetupTests(unittest.TestCase):
    def test_secrets_are_made_once_kept_and_new_when_rotated(self):
        saved = {}
        with patch.object(webhook.keyring, "get_password", lambda service, account: saved.get(account)), \
                patch.object(webhook.keyring, "set_password", lambda service, account, value: saved.update({account: value})):
            first = webhook_secrets()
            self.assertEqual(webhook_secrets(), first)
            rotated = webhook_secrets(rotate=True)
        self.assertNotEqual(first[0], first[1])
        self.assertTrue(rotated[0] != first[0] and rotated[1] != first[1])
        self.assertGreaterEqual(len(first[0]), 40)

    def test_her_urls_carry_the_path_secret(self):
        self.assertEqual(mail_urls("https://cora.example.com", "s"),
                         ("https://cora.example.com/webhooks/s/mail", "https://cora.example.com/webhooks/s/mail/lifecycle"))

    def test_the_address_must_be_https_without_a_path_and_the_port_in_range(self):
        self.assertEqual(normalise_public_url(" https://Cora.Example.com/ "), "https://cora.example.com")
        self.assertEqual(normalise_public_url("https://cora.example.com:8443"), "https://cora.example.com:8443")
        for bad in ("http://cora.example.com", "cora.example.com", "https://cora.example.com/x",
                    "https://cora.example.com?a=1", "https://user@cora.example.com", "https://"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalise_public_url(bad)
        self.assertEqual(check_port(8000), 8000)
        for bad in (80, 70000):
            with self.subTest(port=bad), self.assertRaises(ValueError):
                check_port(bad)

    def test_a_port_already_in_use_is_reported_not_fatal(self):
        taken = socket.socket()
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        try:
            with self.assertRaises(OSError):
                WebhookServer(MagicMock(), taken.getsockname()[1]).open()
        finally:
            taken.close()


class WebhookServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_it_serves_on_localhost_until_stopped(self):
        import asyncio
        on_mail = MagicMock()
        server = WebhookServer(webhook_app(lambda: (PATH, STATE), on_mail, MagicMock()), 0)
        server.open()
        port = server.socket.getsockname()[1]
        self.assertEqual(server.socket.getsockname()[0], "127.0.0.1")
        serving = asyncio.create_task(server.serve())
        for _ in range(50):
            if server.server is not None and server.server.started:
                break
            await asyncio.sleep(0.05)
        async with httpx.AsyncClient() as client:
            answer = await client.post(f"http://127.0.0.1:{port}/webhooks/{PATH}/mail",
                                       content=json.dumps({"value": [{"clientState": STATE}]}))
        self.assertEqual(answer.status_code, 202)
        on_mail.assert_called_once_with()
        server.stop()
        await asyncio.wait_for(serving, 5)


class SubscriptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_subscribing_clears_her_old_subscription_to_that_url_first(self):
        url, lifecycle = mail_urls("https://cora.example.com", PATH)
        client = FakeClient([
            FakeResponse(200, {"value": [{"id": "old", "notificationUrl": url},
                                         {"id": "other", "notificationUrl": "https://elsewhere"}]}),
            FakeResponse(204, None),
            FakeResponse(201, {"id": "new", "expirationDateTime": "2026-11-02T09:00:00.1234567Z"}),
        ])
        made = await Mailbox(lambda: "tok", client).subscribe(url, lifecycle, STATE, EXPIRES)
        self.assertEqual(made, MailSubscription("new", datetime(2026, 11, 2, 9, 0, 0, 123456, tzinfo=timezone.utc)))
        (listed, _, _), (deleted, deleted_url, _), (posted, posted_url, body) = client.sent
        self.assertEqual((listed, deleted, posted), ("GET", "DELETE", "POST"))
        self.assertTrue(deleted_url.endswith("/subscriptions/old") and posted_url.endswith("/subscriptions"))
        self.assertEqual(body, {
            "changeType": "created", "resource": "me/mailFolders('inbox')/messages", "notificationUrl": url,
            "lifecycleNotificationUrl": lifecycle, "clientState": STATE, "expirationDateTime": "2026-11-02T09:00:00Z",
        })

    async def test_renewing_and_ending(self):
        client = FakeClient([FakeResponse(200, {"expirationDateTime": "2026-11-02T09:00:00Z"}), FakeResponse(204, None)])
        mailbox = Mailbox(lambda: "tok", client)
        self.assertEqual((await mailbox.renew("s1", EXPIRES)).expires, EXPIRES)
        await mailbox.unsubscribe("s1")
        self.assertEqual([(method, url.rsplit("/", 1)[1], body) for method, url, body in client.sent], [
            ("PATCH", "s1", {"expirationDateTime": "2026-11-02T09:00:00Z"}), ("DELETE", "s1", None)])


class WebhookAppWiringTests(unittest.IsolatedAsyncioTestCase):
    async def ready(self, app, pilot, listening=True):
        for _ in range(2):
            await pilot.pause()
            await app.workers.wait_for_complete()
        app.email_status = EmailStatus(READY, address="cora@intelligis.io")
        app.mailbox = MagicMock()
        app.mailbox.subscribe = AsyncMock(return_value=MailSubscription("s1", EXPIRES))
        app.mailbox.renew = AsyncMock(return_value=MailSubscription("s1", EXPIRES))
        app.mailbox.unsubscribe = AsyncMock()
        app.webhook_settings = WebhookSettings(True, "https://cora.example.com", 8000)
        app.webhook_secrets = (PATH, STATE)
        app.webhook_server = MagicMock()
        app.webhook_server.server.started = listening

    async def settle(self, app, pilot):
        for _ in range(3):
            await pilot.pause()
            await app.workers.wait_for_complete()

    async def test_subscribed_once_listening_with_her_email_ready_and_renewed_only_when_due(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.ready(app, pilot)
            app.ensure_mail_subscription()
            await self.settle(app, pilot)
            url, lifecycle = mail_urls("https://cora.example.com", PATH)
            app.mailbox.subscribe.assert_awaited_once()
            self.assertEqual(app.mailbox.subscribe.await_args.args[:3], (url, lifecycle, STATE))
            self.assertIn("straight away", app.mail_subscription_status)
            # Not due: nothing asked of Microsoft.
            app.mail_subscription = MailSubscription("s1", datetime.now(timezone.utc) + timedelta(days=1))
            app.ensure_mail_subscription()
            await self.settle(app, pilot)
            app.mailbox.renew.assert_not_awaited()
            app.mail_subscription = MailSubscription("s1", datetime.now(timezone.utc) + timedelta(hours=2))
            app.ensure_mail_subscription()
            await self.settle(app, pilot)
            app.mailbox.renew.assert_awaited_once()
            self.assertEqual(app.mailbox.subscribe.await_count, 1)

    async def test_no_subscription_without_her_email_or_the_listener(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.ready(app, pilot)
            app.email_status = EmailStatus()
            app.ensure_mail_subscription()
            await self.ready(app, pilot)
            app.webhook_server = None
            app.ensure_mail_subscription()
            await self.settle(app, pilot)
            app.mailbox.subscribe.assert_not_awaited()

    async def test_a_failed_subscription_says_why_and_is_tried_again(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.ready(app, pilot)
            app.mailbox.subscribe = AsyncMock(side_effect=GraphError("Microsoft said HTTP 400 (ValidationError)."))
            app.ensure_mail_subscription()
            await self.settle(app, pilot)
            self.assertIsNone(app.mail_subscription)
            self.assertIn("ValidationError", app.mail_subscription_status)

    async def test_mail_events_reach_the_task_mail_check_once_per_burst(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.ready(app, pilot)
            app.check_task_mail = MagicMock()
            with patch.object(app_module(), "MAIL_NOTIFY_SECONDS", 0.05):
                for _ in range(3):
                    app.on_mail_webhook()
                await asyncio.sleep(0.2)
                await pilot.pause()
            app.check_task_mail.assert_called_once_with()
            app.on_mail_lifecycle("missed")
            self.assertIsNotNone(app.mail_notify_timer)

    async def test_mail_landing_mid_check_runs_the_check_once_more(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.ready(app, pilot)
            started = asyncio.Event()
            release = asyncio.Event()
            runs = []

            async def read():
                runs.append(1)
                started.set()
                await release.wait()

            app.read_task_mail = read
            app.check_task_mail()
            await started.wait()
            app.check_task_mail()
            app.check_task_mail()
            release.set()
            await self.settle(app, pilot)
            self.assertEqual(len(runs), 2)

    async def test_stopping_ends_the_subscription_before_the_listener(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.ready(app, pilot)
            server = app.webhook_server
            app.mail_subscription = MailSubscription("s1", EXPIRES)
            await app.stop_webhooks()
            app.mailbox.unsubscribe.assert_awaited_once_with("s1")
            server.stop.assert_called_once_with()
            self.assertIsNone(app.webhook_server)
            self.assertEqual(app.mail_subscription_status, "")


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class WebhookSettingsStoreDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        settings = database_settings()
        admin = psycopg.connect(**settings, autocommit=True)
        cls.addClassCleanup(admin.close)
        cls.database = "cora_test_webhook_settings_" + uuid4().hex
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.database)))
        cls.addClassCleanup(admin.execute, sql.SQL("DROP DATABASE {}").format(sql.Identifier(cls.database)))
        with psycopg.connect(**{**settings, "dbname": cls.database}, autocommit=True) as connection:
            apply_migrations(connection)
        with patch("storage.database.database_settings", return_value={**settings, "dbname": cls.database}):
            cls.engine = build_engine()
        cls.addClassCleanup(cls.engine.dispose)

    def test_off_on_port_8000_until_set_up_and_only_a_checked_address_is_saved(self):
        store = WebhookSettingsStore(self.engine)
        self.assertEqual(store.settings(), WebhookSettings())
        store.set_address("https://Cora.Example.com/", 8001)
        store.set_enabled(True)
        self.assertEqual(store.settings(), WebhookSettings(True, "https://cora.example.com", 8001))
        for url, port in (("http://cora.example.com", 8000), ("https://cora.example.com", 80)):
            with self.subTest(url=url, port=port), self.assertRaises(ValueError):
                store.set_address(url, port)
        self.assertEqual(store.settings().port, 8001)
        # The database holds the line too.
        with self.assertRaises(Exception), self.engine.begin() as connection:
            connection.execute(text("UPDATE public.webhook_settings SET public_url = 'http://x'"))


def app_module():
    import app
    return app


if __name__ == "__main__":
    unittest.main()
