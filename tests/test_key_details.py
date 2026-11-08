"""Key details: what the person told Cora to keep to hand, saved by note_detail and in her prompts."""

import json
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import psycopg
from psycopg import sql

from migrate import apply_migrations, database_settings
from session_config import instruction_sections
from storage.database import build_engine
from storage.key_details_store import DETAILS_LIMIT, FullError, KeyDetailsStore
from tools import note_detail
from tools.note_detail import NoteDetailTool, validate_arguments


class PromptTests(unittest.TestCase):
    def test_key_details_follow_her_name_and_say_to_use_them_without_asking(self):
        context = json.dumps({"Nick's email": "nick@example.com", "Wife (Sarah) mobile": "0400 000 000"})
        sections = instruction_sections(person_name="Nick", key_details_context=context)
        self.assertEqual(list(sections), ["name", "key_details"])
        text = sections["key_details"]
        self.assertIn("rather than asking again", text)
        self.assertIn("note_detail", text)
        self.assertIn("They're private", text)
        self.assertTrue(text.endswith(context))
        self.assertNotIn("key_details", instruction_sections())
        with self.assertRaises(ValueError):
            instruction_sections(key_details_context="x" * 100_000)


class ArgumentTests(unittest.TestCase):
    def test_a_label_with_a_value_or_null(self):
        validate_arguments({"label": "Nick's email", "value": "nick@example.com"})
        validate_arguments({"label": "Nick's email", "value": None})
        for arguments in (
            {"label": "x"}, {"label": " ", "value": "v"}, {"label": "x" * 101, "value": "v"},
            {"label": "x", "value": " "}, {"label": "x", "value": "v" * 501}, {"label": "x", "value": 3},
            {"label": "x", "value": "v", "extra": 1},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                validate_arguments(arguments)


class ToolTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, arguments, outcome):
        connection = MagicMock()
        connection.response.item.create = AsyncMock()
        connection.response.create = AsyncMock()
        tool = NoteDetailTool(lambda: connection, lambda: False, MagicMock())
        with patch.object(note_detail, "_note", side_effect=outcome) as noted:
            await tool.handle("call-1", arguments)
        output = json.loads(connection.response.item.create.await_args.kwargs["item"]["output"])
        connection.response.create.assert_awaited_once()
        return noted, output

    async def test_a_detail_is_saved_tidied(self):
        noted, output = await self.call({"label": " Nick's  email ", "value": " nick@example.com "},
                                        lambda label, value: {"status": "ok", "label": label, "replaced": False})
        noted.assert_called_once_with("Nick's email", "nick@example.com")
        self.assertEqual(output["status"], "ok")

    async def test_null_forgets(self):
        noted, _ = await self.call({"label": "Old address", "value": None},
                                   lambda label, value: {"status": "ok", "forgotten": label})
        noted.assert_called_once_with("Old address", None)

    async def test_a_failure_says_it_wasnt_saved_without_the_value(self):
        def fail(label, value):
            raise RuntimeError("secret 0400 000 000")
        _, output = await self.call({"label": "Phone", "value": "0400 000 000"}, fail)
        self.assertEqual(output["status"], "error")
        self.assertNotIn("0400", json.dumps(output))

    def test_it_is_left_out_of_clients_chats_and_task_work(self):
        from app_areas.contact_calls import CALL_TOOLS
        from app_areas.task_calls import TASK_CALL_TOOLS
        from app_areas.tasks import COORDINATOR_SEND_TOOLS, COORDINATOR_TOOLS
        from tools.pass_on import CLIENT_ONLY
        for allowed in (CALL_TOOLS, TASK_CALL_TOOLS, COORDINATOR_TOOLS | COORDINATOR_SEND_TOOLS,
                        frozenset({"search_memory", "feel", *CLIENT_ONLY})):
            self.assertNotIn("note_detail", allowed)


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class KeyDetailsStoreDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        settings = database_settings()
        admin = psycopg.connect(**settings, autocommit=True)
        cls.addClassCleanup(admin.close)
        cls.database = "cora_test_key_details_" + uuid4().hex
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.database)))
        cls.addClassCleanup(admin.execute, sql.SQL("DROP DATABASE {}").format(sql.Identifier(cls.database)))
        with psycopg.connect(**{**settings, "dbname": cls.database}, autocommit=True) as connection:
            apply_migrations(connection)
        with patch("storage.database.database_settings", return_value={**settings, "dbname": cls.database}):
            cls.engine = build_engine()
        cls.addClassCleanup(cls.engine.dispose)

    def setUp(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql("DELETE FROM public.key_details")
        self.store = KeyDetailsStore(self.engine)

    def test_details_are_saved_replaced_by_label_and_forgotten(self):
        self.assertEqual(self.store.get_startup_context(), "")
        self.assertFalse(self.store.save("Nick's email", "nick@example.com"))
        self.assertFalse(self.store.save("Wife (Sarah) mobile", "0400  000 000"))
        self.assertTrue(self.store.save("nick's EMAIL", "nick@new.example"))
        self.assertEqual(json.loads(self.store.get_startup_context()), {
            "nick's EMAIL": "nick@new.example", "Wife (Sarah) mobile": "0400 000 000",
        })
        self.assertTrue(self.store.forget("NICK'S email"))
        self.assertFalse(self.store.forget("Nick's email"))
        self.assertEqual(self.store.all(), [("Wife (Sarah) mobile", "0400 000 000")])

    def test_a_full_list_refuses_a_new_label_but_still_replaces(self):
        for number in range(DETAILS_LIMIT):
            self.store.save(f"Detail {number}", "value")
        with self.assertRaises(FullError):
            self.store.save("One more", "value")
        self.assertTrue(self.store.save("Detail 0", "new value"))

    def test_bad_details_are_refused(self):
        for label, value in ((" ", "v"), ("x" * 101, "v"), ("x", " "), ("x", "v" * 501)):
            with self.subTest(label=label[:5], value=value[:5]), self.assertRaises(ValueError):
                self.store.save(label, value)


if __name__ == "__main__":
    unittest.main()
