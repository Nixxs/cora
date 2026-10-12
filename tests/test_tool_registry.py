import unittest
from unittest.mock import AsyncMock, MagicMock

from tools.registry import ToolRegistry


class ToolRegistryTests(unittest.TestCase):
    def setUp(self):
        self.log = MagicMock()
        self.schedule = MagicMock()
        self.registry = ToolRegistry(self.log, lambda: False, self.schedule)
        self.schema = {
            "type": "function",
            "name": "example",
            "parameters": {"type": "object", "properties": {}},
        }

    def test_exported_schemas_cannot_change_registered_definitions(self):
        self.registry.register(self.schema, MagicMock(), AsyncMock())
        self.schema["name"] = "changed"
        self.schema["parameters"]["properties"]["unexpected"] = {"type": "string"}
        exported = self.registry.schemas()
        self.assertEqual(exported[0]["name"], "example")
        self.assertEqual(exported[0]["parameters"]["properties"], {})
        exported[0]["parameters"]["properties"]["also_unexpected"] = {"type": "string"}
        self.assertEqual(self.registry.schemas()[0]["parameters"]["properties"], {})

    def test_duplicate_and_invalid_schemas_are_rejected(self):
        self.registry.register(self.schema, MagicMock(), AsyncMock())
        with self.assertRaisesRegex(ValueError, "already registered"):
            self.registry.register(self.schema, MagicMock(), AsyncMock())
        for schema in ({"type": "web_search"}, {"type": "function", "name": " "}):
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                self.registry.register(schema, MagicMock(), AsyncMock())

    def test_calls_are_ignored_while_application_is_closing(self):
        self.registry.register(self.schema, MagicMock(), AsyncMock())
        self.registry.is_closing = lambda: True
        self.registry.handle_function_call({
            "name": "example", "call_id": "example_1", "arguments": "{}", "status": "completed",
        })
        self.schedule.assert_not_called()
        self.assertFalse(self.registry.handled_call_ids)


if __name__ == "__main__":
    unittest.main()