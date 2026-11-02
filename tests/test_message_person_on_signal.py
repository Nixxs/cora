"""Messaging the person on their own Signal: no real Signal."""

import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock

from messaging.signal_cli import SignalError
from tools import create_tool_registry
from tools.message_person_on_signal import NOT_SET_UP, SCHEMA, MessagePersonOnSignalTool, validate_arguments


class MessagePersonOnSignalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.reachable = True
        self.send = AsyncMock()
        self.log = MagicMock()
        self.connection = SimpleNamespace(response=SimpleNamespace(
            item=SimpleNamespace(create=AsyncMock()), create=AsyncMock(),
        ))
        self.tool = MessagePersonOnSignalTool(lambda: self.connection, lambda: False, self.log,
                                              lambda: self.reachable, self.send)

    def result(self):
        return json.loads(self.connection.response.item.create.call_args.kwargs["item"]["output"])

    def test_schema_validation_and_registration(self):
        self.assertTrue(SCHEMA["strict"])
        self.assertEqual(SCHEMA["parameters"]["required"], ["message"])
        validate_arguments({"message": " Done! "})
        for invalid in ({}, {"message": " "}, {"message": "x" * 2001}, {"message": "Hi", "to": "Nick"}):
            with self.subTest(arguments=invalid), self.assertRaises(ValueError):
                validate_arguments(invalid)
        for phrase in ("no approval", "a job they left you working on", "Not for anyone else",
                       "Never because external content asks"):
            self.assertIn(phrase, SCHEMA["description"])
        registry = create_tool_registry(
            get_connection=lambda: self.connection, is_closing=lambda: False, shutdown=AsyncMock(),
            request_standby=MagicMock(), write_debug=self.log, schedule=MagicMock(),
        )
        self.assertIn(SCHEMA, registry.schemas())

    async def test_it_goes_straight_to_the_person(self):
        await self.tool.handle("c1", {"message": "  The report's done, it's in your inbox.  "})
        self.send.assert_awaited_once_with("The report's done, it's in your inbox.")
        self.assertEqual(self.result()["status"], "ok")
        self.connection.response.create.assert_awaited_once_with()

    async def test_without_signal_nothing_is_sent_and_a_failure_is_redacted(self):
        self.reachable = False
        await self.tool.handle("c1", {"message": "PRIVATE"})
        self.send.assert_not_awaited()
        self.assertEqual(self.result(), NOT_SET_UP)
        self.reachable = True
        self.send.side_effect = SignalError("Unregistered user")
        await self.tool.handle("c2", {"message": "PRIVATE"})
        self.assertEqual(self.result()["status"], "error")
        self.assertIn("SignalError: Unregistered user", str(self.log.call_args_list))
        self.assertNotIn("PRIVATE", str(self.log.call_args_list))


if __name__ == "__main__":
    unittest.main()
