"""How Cora pictures herself: stored images and her own words, in her prompts and on her Signal profile."""

import io
import json
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx2
import psycopg
from PIL import Image
from psycopg import sql

from messaging.profile import AVATAR_SIZE, avatar_jpeg
from messaging.signal_cli import SignalClient
from migrate import apply_migrations, database_settings
from session_config import instruction_sections
from storage.database import build_engine
from storage.self_image_store import SelfImageStore


class PromptTests(unittest.TestCase):
    def test_her_self_image_sits_before_her_identity_and_says_she_isnt_human(self):
        context = json.dumps({"self": "A red scarf and round glasses.", "work": "A grey blazer."})
        sections = instruction_sections(identity_context="I like old machines.", self_image_context=context)
        self.assertEqual(list(sections), ["self_image", "identity"])
        text = sections["self_image"]
        self.assertIn("You aren't human", text)
        self.assertIn("'work' is you at work.", text)
        self.assertNotIn("Intelligis", text)
        self.assertIn("You don't see images", text)
        self.assertTrue(text.endswith(context))
        self.assertNotIn("self_image", instruction_sections())
        with self.assertRaises(ValueError):
            instruction_sections(self_image_context="x" * 2501)


class ProfileTests(unittest.IsolatedAsyncioTestCase):
    def test_the_avatar_is_a_square_jpeg(self):
        source = io.BytesIO()
        Image.new("RGB", (1200, 900), "navy").save(source, "PNG")
        avatar = Image.open(io.BytesIO(avatar_jpeg(source.getvalue())))
        self.assertEqual((avatar.format, avatar.size), ("JPEG", (AVATAR_SIZE, AVATAR_SIZE)))

    async def test_update_profile_sends_her_name_and_picture_path(self):
        requests = []

        def handle(request):
            requests.append(json.loads(request.content))
            return httpx2.Response(200, json={"jsonrpc": "2.0", "id": "1", "result": {}})

        client = SignalClient("http://signal.test", transport=httpx2.MockTransport(handle))
        await client.update_profile("+61400000001", "Cora", "/var/lib/signal-cli/cora-avatar.jpg")
        await client.update_profile("+61400000001", "Cora")
        self.assertEqual(requests[0]["method"], "updateProfile")
        self.assertEqual(requests[0]["params"], {
            "account": "+61400000001", "givenName": "Cora", "avatar": "/var/lib/signal-cli/cora-avatar.jpg",
        })
        self.assertNotIn("avatar", requests[1]["params"])


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class SelfImageStoreDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        settings = database_settings()
        admin = psycopg.connect(**settings, autocommit=True)
        cls.addClassCleanup(admin.close)
        cls.database = "cora_test_self_image_" + uuid4().hex
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.database)))
        cls.addClassCleanup(admin.execute, sql.SQL("DROP DATABASE {}").format(sql.Identifier(cls.database)))
        with psycopg.connect(**{**settings, "dbname": cls.database}, autocommit=True) as connection:
            apply_migrations(connection)
        with patch("storage.database.database_settings", return_value={**settings, "dbname": cls.database}):
            cls.engine = build_engine()
        cls.addClassCleanup(cls.engine.dispose)

    def test_images_and_her_words_are_saved_and_can_change(self):
        store = SelfImageStore(self.engine)
        self.assertEqual(store.get_startup_context(), "")
        store.set("work", "A grey blazer.", b"PNG...", "image/png")
        store.set("self", "  A red   scarf. ")
        self.assertEqual(json.loads(store.get_startup_context()),
                         {"self": "A red scarf.", "work": "A grey blazer."})
        self.assertEqual(store.get("work").image, b"PNG...")
        store.set("self", "A new look.", b"JPG", "image/jpeg")
        self.assertEqual(store.get("self").description, "A new look.")
        for kind, description, image, image_type in (
            ("other", "x", None, None), ("self", " ", None, None), ("self", "x" * 1001, None, None),
            ("self", "x", b"PNG", None),
        ):
            with self.subTest(kind=kind, description=description[:5]), self.assertRaises(ValueError):
                store.set(kind, description, image, image_type)
