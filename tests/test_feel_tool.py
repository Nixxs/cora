"""The feel tool: silent, hers, offered in voice, typed chat and Signal but not email. No real API."""

from datetime import datetime, timezone
import json
import unittest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from backend_chat import SIGNAL_EXCLUDED, STANDBY_EXCLUDED, BackendRoute
from app_areas.mail import PERSON_EMAIL_EXCLUDED
from messaging.receiver import IncomingMessage
from session_config import BACKEND_INSTRUCTIONS, LIVE_INSTRUCTIONS
from subconscious.emotions.feeling_store import FeelingLimit
from tools import create_tool_registry
from tools.feel import NOTED, SCHEMA, FeelTool, validate_arguments
import test_keyboard_persistence as keyboard_tests


class FeelToolTests(unittest.IsolatedAsyncioTestCase):
    def tool(self, connection, problem=None):
        connection.response.item.create = AsyncMock()
        connection.response.create = AsyncMock()
        self.relay = AsyncMock(return_value=problem)
        return FeelTool(lambda: connection, lambda: False, MagicMock(), self.relay)

    def output(self, connection):
        return json.loads(connection.response.item.create.call_args.kwargs["item"]["output"])

    def test_schema_is_strict_with_only_the_six_feelings(self):
        self.assertTrue(SCHEMA["strict"])
        properties = SCHEMA["parameters"]["properties"]
        self.assertEqual(properties["feeling"]["enum"],
                         ["excitement", "contentment", "curiosity", "fulfilment", "irritation", "hurt"])
        self.assertEqual(properties["strength"]["enum"], ["small", "medium", "strong"])
        validate_arguments({"feeling": "hurt", "strength": "strong", "reason": "told I don't care"})
        for bad in ({}, {"feeling": "guilt", "strength": "small", "reason": "x"},
                    {"feeling": "hurt", "strength": "huge", "reason": "x"},
                    {"feeling": "hurt", "strength": "small", "reason": " "},
                    {"feeling": "hurt", "strength": "small", "reason": "x" * 201},
                    {"feeling": "hurt", "strength": "small", "reason": "x", "extra": 1}):
            with self.subTest(arguments=bad), self.assertRaises(ValueError):
                validate_arguments(bad)

    async def test_with_the_person_it_is_noted_for_their_conversation(self):
        for connection in (MagicMock(spec=["response"]), BackendRoute()):
            with self.subTest(connection=type(connection).__name__):
                tool = self.tool(connection)
                await tool.handle("c1", {"feeling": "hurt", "strength": "strong", "reason": " told I  don't care "})
                self.relay.assert_awaited_once_with(None, "hurt", "strong", "told I don't care")
                self.assertEqual(self.output(connection), {"status": "ok", "message": NOTED})
                connection.response.create.assert_awaited_once()

    async def test_in_a_contacts_chat_it_is_noted_for_theirs_and_a_limit_is_passed_back(self):
        client = (uuid4(), "Sarah")
        route = BackendRoute(memory_scope=client)
        tool = self.tool(route, problem="You noted hurt only a moment ago; it's still there.")
        await tool.handle("c1", {"feeling": "hurt", "strength": "small", "reason": "brushed off"})
        self.relay.assert_awaited_once_with(client, "hurt", "small", "brushed off")
        self.assertEqual(self.output(route)["status"], "not_noted")
        self.assertIn("only a moment ago", self.output(route)["message"])

    def test_offered_in_voice_typed_chat_and_signal_but_not_email(self):
        registry = create_tool_registry(get_connection=lambda: None, is_closing=lambda: False, shutdown=MagicMock(),
                                        request_standby=MagicMock(), write_debug=MagicMock(), schedule=MagicMock())
        self.assertIn("feel", registry.names())
        for excluded in (STANDBY_EXCLUDED, SIGNAL_EXCLUDED):
            self.assertNotIn("feel", excluded)
        self.assertIn("feel", PERSON_EMAIL_EXCLUDED)
        self.assertEqual(PERSON_EMAIL_EXCLUDED - {"feel"}, SIGNAL_EXCLUDED)

    def test_she_is_told_it_is_hers_and_never_mentioned(self):
        self.assertIn("quietly note the feeling", LIVE_INSTRUCTIONS)
        self.assertIn("never mention noting it", LIVE_INSTRUCTIONS)
        self.assertIn("Use feel, quietly", BACKEND_INSTRUCTIONS)
        for phrase in ("never to perform, to steer the person", "only for you", "harder to hide"):
            self.assertIn(phrase, SCHEMA["description"])


class NoteFeelingAppTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_and_shown_in_activity_or_the_reason_it_wasnt(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.assertIsNone(await app.note_feeling(None, "hurt", "strong", "told I don't care"))
            app.persistence.record_feeling.assert_called_with("hurt", "strong", "told I don't care", None)
            self.assertIn("Cora feels hurt (strong): told I don't care", "\n".join(app.activity.entries))
            client_id = uuid4()
            self.assertIsNone(await app.note_feeling((client_id, "Sarah"), "curiosity", "small", "her moss garden"))
            app.persistence.record_feeling.assert_called_with("curiosity", "small", "her moss garden", client_id)
            self.assertIn("Cora feels curious (small) in Sarah's chat: her moss garden", "\n".join(app.activity.entries))
            app.persistence.record_feeling.side_effect = FeelingLimit("You've noted enough feelings.")
            self.assertEqual(await app.note_feeling(None, "hurt", "small", "again"), "You've noted enough feelings.")
            app.persistence.record_feeling.side_effect = RuntimeError("private")
            self.assertIn("couldn't be noted", await app.note_feeling(None, "hurt", "small", "again"))
            self.assertIn("wasn't saved (RuntimeError)", "\n".join(app.activity.entries))

    async def test_a_contacts_chat_offers_it(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.backend_chat.reply = AsyncMock(return_value="Hi")
            app.client_sections = AsyncMock(return_value={})
            client = MagicMock(id=uuid4())
            client.name = "Sarah"
            await app.reply_to_client(client, [IncomingMessage("+61400000003", "hi", datetime.now(timezone.utc))], [])
            route = app.backend_chat.reply.call_args.kwargs["route"]
            self.assertNotIn("feel", route.excluded)
            self.assertIn("send_email", route.excluded)


if __name__ == "__main__":
    unittest.main()
