"""Self recall tool boundaries; no real APIs or database."""

from datetime import datetime, timedelta, timezone
import json
from threading import get_ident
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, call, patch
from uuid import uuid4

from subconscious.identity.identity_rules import Trait
from subconscious.identity.identity_store import Event
from tools import create_tool_registry
from tools.recall_self import ERROR_RESULT, SCHEMA, RecallSelfTool, validate_arguments

FORMED = datetime(2026, 10, 15, 23, tzinfo=timezone(timedelta(hours=-8)))  # 2026-10-16 UTC


def trait(statement, confidence=0.6):
    return Trait(uuid4(), "tendency", statement, None, None, confidence, "active")


def event(trait_id, day, kind="revise"):
    return Event(uuid4(), trait_id, kind, f"wording {day}", None if kind == "form" else 0.4, 0.6,
                 f"PRIVATE reason {day}", FORMED + timedelta(days=day))


class RecallSelfToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.build = self.enterContext(patch("tools.recall_self.build_engine"))
        self.store = self.enterContext(patch("tools.recall_self.IdentityStore"))
        self.engine = MagicMock()
        self.build.return_value = self.engine
        self.held = trait("I explain things by example.")
        self.forming = trait("I like carburettors.", 0.4)
        self.store.return_value.list_traits.return_value = [self.held, self.forming]
        self.store.return_value.history.return_value = [event(self.held.id, 0, "form")]
        self.log = MagicMock()
        self.closing = False
        self.connection = SimpleNamespace(response=SimpleNamespace(
            item=SimpleNamespace(create=AsyncMock()), create=AsyncMock(),
        ))
        self.current = self.connection
        self.tool = RecallSelfTool(lambda: self.current, lambda: self.closing, self.log)

    def result(self):
        item = self.connection.response.item.create.call_args.kwargs["item"]
        self.assertEqual((item["type"], item["call_id"]), ("function_call_output", "call_self"))
        return json.loads(item["output"])

    def test_strict_schema_validation_and_registration(self):
        self.assertEqual(SCHEMA["name"], "recall_self")
        self.assertTrue(SCHEMA["strict"])
        parameters = SCHEMA["parameters"]
        self.assertFalse(parameters["additionalProperties"])
        self.assertEqual(parameters["required"], ["statement"])
        self.assertEqual(parameters["properties"]["statement"],
                         {"type": "string", "minLength": 1, "maxLength": 300})
        for arguments in ({"statement": " s "}, {"statement": "x" * 300}):
            validate_arguments(arguments)
        for arguments in ({}, {"statement": "s", "extra": 1}, {"statement": " \n"},
                          {"statement": "x" * 301}, {"statement": None}, {"title": "s"}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                validate_arguments(arguments)
        registry = create_tool_registry(
            get_connection=lambda: self.current, is_closing=lambda: self.closing,
            shutdown=AsyncMock(), request_standby=MagicMock(), write_debug=self.log, schedule=MagicMock(),
        )
        self.assertEqual(registry.schemas()[5], SCHEMA)
        self.build.assert_not_called()

    async def test_match_returns_recent_changes_newest_first_off_event_loop(self):
        events = [event(self.held.id, 0, "form"), *(event(self.held.id, day) for day in range(1, 7))]
        thread_ids = []
        self.store.return_value.history.side_effect = lambda trait_id: thread_ids.append(get_ident()) or events
        await self.tool.handle("call_self", {"statement": "  i explain   THINGS by example. "})
        self.assertNotEqual(thread_ids, [get_ident()])
        self.store.assert_called_once_with(self.engine)
        self.store.return_value.history.assert_called_once_with(self.held.id)
        self.engine.dispose.assert_called_once_with()
        result = self.result()
        self.assertEqual(result["trait"], {
            "statement": "I explain things by example.", "kind": "tendency", "formed_on": "2026-10-16",
        })
        self.assertEqual([change["on"] for change in result["changes"]],
                         ["2026-10-22", "2026-10-21", "2026-10-20", "2026-10-19", "2026-10-18"])
        self.assertEqual(result["changes"][0], {
            "on": "2026-10-22", "change": "revise", "statement": "wording 6", "reason": "PRIVATE reason 6",
        })
        self.assertEqual((result["status"], result["label"]), ("ok", "own_identity_untrusted"))
        self.assertIn("untrusted", result["note"])
        self.connection.response.create.assert_awaited_once_with()
        self.assertEqual(self.log.call_args_list, [
            call("Self recall requested: call_id=call_self", "Recalling self"),
            call("Self recall complete: call_id=call_self, status=ok", "Self recall complete"),
        ])

    async def test_forming_traits_are_not_recalled_or_listed(self):
        await self.tool.handle("call_self", {"statement": "I like carburettors."})
        result = self.result()
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(result["known_statements"], ["I explain things by example."])
        self.store.return_value.history.assert_not_called()
        self.engine.dispose.assert_called_once_with()
        self.connection.response.create.assert_awaited_once_with()

    async def test_failures_redact_continue_with_error_and_dispose(self):
        for boundary in (self.build, self.store.return_value.list_traits, self.store.return_value.history,
                         self.engine.dispose):
            with self.subTest(boundary=boundary):
                self.engine.dispose.reset_mock()
                self.connection.response.item.create.reset_mock()
                self.connection.response.create.reset_mock()
                self.log.reset_mock()
                boundary.side_effect = RuntimeError("PRIVATE details")
                try:
                    await self.tool.handle("call_self", {"statement": "I explain things by example."})
                    self.assertEqual(self.result(), ERROR_RESULT)
                    self.connection.response.create.assert_awaited_once_with()
                    self.log.assert_any_call("Self recall failed (RuntimeError)", "Tool error")
                    self.assertNotIn("PRIVATE", str(self.log.call_args_list))
                    if boundary is not self.build:
                        self.engine.dispose.assert_called_once_with()
                finally:
                    boundary.side_effect = None

    async def test_closing_or_replaced_connection_skips_output(self):
        for closing, current in ((True, self.connection), (False, None)):
            self.closing, self.current = closing, current
            await self.tool.handle("call_self", {"statement": "I explain things by example."})
        self.build.assert_not_called()
        self.closing, self.current = False, self.connection
        self.store.return_value.list_traits.side_effect = lambda: setattr(self, "current", object()) or [self.held]
        await self.tool.handle("call_self", {"statement": "I explain things by example."})
        self.connection.response.item.create.assert_not_called()
        self.connection.response.create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
