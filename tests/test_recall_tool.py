"""recall: one tool for her views of herself, her interests and her wishes, each kind answered by its
own lookup (tested in test_recall_{self,interest,desire}_tool)."""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tools import create_tool_registry
from tools.recall import KINDS, SCHEMA, TEXT_MAX_CHARS, RecallTool, validate_arguments


class RecallToolTests(unittest.IsolatedAsyncioTestCase):
    def test_strict_schema_and_validation(self):
        self.assertEqual(SCHEMA["name"], "recall")
        self.assertTrue(SCHEMA["strict"])
        self.assertEqual(SCHEMA["parameters"]["required"], ["kind", "text"])
        self.assertEqual(SCHEMA["parameters"]["properties"]["kind"]["enum"], ["self", "interest", "desire"])
        validate_arguments({"kind": "self", "text": " I like quiet. "})
        for bad in ({}, {"kind": "self"}, {"kind": "mood", "text": "x"}, {"kind": "self", "text": " "},
                    {"kind": "self", "text": "x" * (TEXT_MAX_CHARS + 1)}, {"kind": "self", "text": "x", "extra": 1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_arguments(bad)

    def test_one_tool_registered_for_all_three(self):
        registry = create_tool_registry(get_connection=MagicMock(), is_closing=lambda: False, shutdown=AsyncMock(),
                                        request_standby=MagicMock(), write_debug=MagicMock(), schedule=MagicMock())
        names = [schema["name"] for schema in registry.schemas()]
        self.assertIn("recall", names)
        self.assertFalse({"recall_self", "recall_interest", "recall_desire"} & set(names))

    async def test_each_kind_goes_to_its_own_lookup_with_its_own_argument(self):
        tool = RecallTool(MagicMock(), lambda: False, MagicMock())
        for kind, (_, name, longest) in KINDS.items():
            with patch.object(tool.tools[kind], "handle", AsyncMock()) as handled:
                await tool.handle("c1", {"kind": kind, "text": "  Old maps  "})
                handled.assert_awaited_once_with("c1", {name: "Old maps"})
                # Too long for its kind can't match: cut to fit, so she's told the real wordings.
                await tool.handle("c2", {"kind": kind, "text": "x" * TEXT_MAX_CHARS})
                self.assertEqual(len(handled.await_args.args[1][name]), longest)


if __name__ == "__main__":
    unittest.main()
