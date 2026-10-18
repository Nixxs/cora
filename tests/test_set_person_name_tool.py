"""Person-name tool boundaries; no real APIs or database."""

import json
from threading import get_ident
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, call, patch

from tools import create_tool_registry
from tools.set_person_name import ERROR_RESULT, SCHEMA, SetPersonNameTool, validate_arguments


class SetPersonNameToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for target, attribute in (
            ("tools.set_person_name.build_engine", "build"),
            ("tools.set_person_name.PeopleStore", "people"),
        ):
            guard = patch(target)
            setattr(self, attribute, guard.start())
            self.addCleanup(guard.stop)
        self.engine = MagicMock()
        self.build.return_value = self.engine
        self.log = MagicMock()
        self.closing = False
        self.connection = SimpleNamespace(response=SimpleNamespace(
            item=SimpleNamespace(create=AsyncMock()), create=AsyncMock(),
        ))
        self.current = self.connection
        self.tool = SetPersonNameTool(lambda: self.current, lambda: self.closing, self.log)

    def result(self):
        item = self.connection.response.item.create.call_args.kwargs["item"]
        self.assertEqual((item["type"], item["call_id"]), ("function_call_output", "call_name"))
        return json.loads(item["output"])

    def test_strict_schema_validation_and_registration(self):
        self.assertTrue(SCHEMA["strict"])
        parameters = SCHEMA["parameters"]
        self.assertFalse(parameters["additionalProperties"])
        self.assertEqual(parameters["required"], ["name"])
        self.assertEqual(parameters["properties"]["name"], {"type": "string", "minLength": 1, "maxLength": 100})
        self.assertIn("explicitly", SCHEMA["description"])
        for arguments in ({"name": " Sam "}, {"name": "x" * 100}):
            validate_arguments(arguments)
        for arguments in ({}, {"name": "Sam", "extra": 1}, {"name": ""}, {"name": " \n"},
                          {"name": "x" * 101}, {"name": None}, {"name": 4}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                validate_arguments(arguments)
        registry = create_tool_registry(
            get_connection=lambda: self.current, is_closing=lambda: self.closing,
            shutdown=AsyncMock(), request_standby=MagicMock(), write_debug=self.log, schedule=MagicMock(),
        )
        self.assertEqual(registry.schemas()[4], SCHEMA)
        self.build.assert_not_called()

    async def test_saves_trimmed_name_off_event_loop_then_continues(self):
        thread_ids = []
        self.people.return_value.set_current_name.side_effect = lambda name: thread_ids.append(get_ident())
        order = MagicMock()
        order.attach_mock(self.connection.response.item.create, "output")
        order.attach_mock(self.connection.response.create, "continue_response")
        await self.tool.handle("call_name", {"name": "  Sam Lee "})
        self.assertNotEqual(thread_ids, [get_ident()])
        self.people.assert_called_once_with(self.engine)
        self.people.return_value.set_current_name.assert_called_once_with("Sam Lee")
        self.engine.dispose.assert_called_once_with()
        self.assertEqual(self.result(), {"status": "ok", "name": "Sam Lee"})
        self.assertEqual([entry[0] for entry in order.mock_calls], ["output", "continue_response"])
        self.assertEqual(self.log.call_args_list, [call("Person name saved: call_id=call_name", "Person named")])

    async def test_failures_redact_continue_with_error_and_dispose(self):
        for boundary in (self.build, self.people.return_value.set_current_name, self.engine.dispose):
            with self.subTest(boundary=boundary):
                self.engine.dispose.reset_mock()
                self.connection.response.item.create.reset_mock()
                self.connection.response.create.reset_mock()
                self.log.reset_mock()
                boundary.side_effect = RuntimeError("PRIVATE details")
                try:
                    await self.tool.handle("call_name", {"name": "Sam"})
                    self.assertEqual(self.result(), ERROR_RESULT)
                    self.connection.response.create.assert_awaited_once_with()
                    self.log.assert_called_once_with("Person name save failed (RuntimeError)", "Tool error")
                    self.assertNotIn("PRIVATE", str(self.log.call_args_list))
                    if boundary is not self.build:
                        self.engine.dispose.assert_called_once_with()
                finally:
                    boundary.side_effect = None

    async def test_closing_or_replaced_connection_skips_output(self):
        for closing, current in ((True, self.connection), (False, None)):
            self.closing, self.current = closing, current
            await self.tool.handle("call_name", {"name": "Sam"})
        self.build.assert_not_called()
        self.closing, self.current = False, self.connection
        self.people.return_value.set_current_name.side_effect = (
            lambda name: setattr(self, "current", object()))
        await self.tool.handle("call_name", {"name": "Sam"})
        self.connection.response.item.create.assert_not_called()
        self.connection.response.create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
