"""Desire recall tool boundaries; no real APIs or database."""

from datetime import datetime, timedelta, timezone
import json
from threading import get_ident
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, call, patch
from uuid import uuid4

from subconscious.desires.desire_store import Desire, DesireEvent
from tools import create_tool_registry
from tools.recall_desire import ERROR_RESULT, SCHEMA, RecallDesireTool, validate_arguments

FORMED = datetime(2026, 10, 15, 23, tzinfo=timezone(timedelta(hours=-8)))  # 2026-10-16 UTC


def desire(wish, status="held"):
    return Desire(uuid4(), wish, "PRIVATE motivation", 0.5, FORMED, status, FORMED, None, None)


def event(day, kind="reinforce"):
    return DesireEvent(kind, None if kind == "form" else 0.4, 0.5, f"PRIVATE note {day}", FORMED + timedelta(days=day))


class RecallDesireToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.build = self.enterContext(patch("tools.recall_desire.build_engine"))
        self.store = self.enterContext(patch("tools.recall_desire.DesireStore"))
        self.grew_from = self.enterContext(patch("tools.recall_desire._grew_from",
                                                 return_value=[{"interest": "Forests"}]))
        self.engine = MagicMock()
        self.build.return_value = self.engine
        self.held = desire("To walk through an old-growth forest.")
        self.store.return_value.held_desires.return_value = [self.held]
        self.store.return_value.history.return_value = [event(0, "form")]
        self.log = MagicMock()
        self.closing = False
        self.connection = SimpleNamespace(response=SimpleNamespace(
            item=SimpleNamespace(create=AsyncMock()), create=AsyncMock(),
        ))
        self.current = self.connection
        self.tool = RecallDesireTool(lambda: self.current, lambda: self.closing, self.log)

    def result(self):
        item = self.connection.response.item.create.call_args.kwargs["item"]
        self.assertEqual((item["type"], item["call_id"]), ("function_call_output", "call_desire"))
        return json.loads(item["output"])

    def test_strict_schema_validation_and_registration(self):
        self.assertEqual(SCHEMA["name"], "recall_desire")
        self.assertTrue(SCHEMA["strict"])
        parameters = SCHEMA["parameters"]
        self.assertFalse(parameters["additionalProperties"])
        self.assertEqual(parameters["required"], ["wish"])
        self.assertEqual(parameters["properties"]["wish"], {"type": "string", "minLength": 1, "maxLength": 300})
        for arguments in ({"wish": " w "}, {"wish": "x" * 300}):
            validate_arguments(arguments)
        for arguments in ({}, {"wish": "w", "extra": 1}, {"wish": " \n"}, {"wish": "x" * 301}, {"wish": None},
                          {"statement": "w"}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                validate_arguments(arguments)
        registry = create_tool_registry(
            get_connection=lambda: self.current, is_closing=lambda: self.closing,
            shutdown=AsyncMock(), request_standby=MagicMock(), write_debug=self.log, schedule=MagicMock(),
        )
        self.assertEqual(registry.schemas()[6], SCHEMA)
        self.build.assert_not_called()

    async def test_match_returns_what_it_grew_from_and_recent_changes_newest_first_off_event_loop(self):
        events = [event(0, "form"), *(event(day) for day in range(1, 7))]
        thread_ids = []
        self.store.return_value.history.side_effect = lambda desire_id: thread_ids.append(get_ident()) or events
        await self.tool.handle("call_desire", {"wish": "  to walk through an   OLD-GROWTH forest. "})
        self.assertNotEqual(thread_ids, [get_ident()])
        self.store.assert_called_once_with(self.engine)
        self.store.return_value.history.assert_called_once_with(self.held.id)
        self.grew_from.assert_called_once_with(self.store.return_value, self.held.id)
        self.engine.dispose.assert_called_once_with()
        result = self.result()
        self.assertEqual(result["desire"], {
            "wish": "To walk through an old-growth forest.", "why": "PRIVATE motivation", "status": "held",
            "formed_on": "2026-10-16",
        })
        self.assertEqual(result["grew_from"], [{"interest": "Forests"}])
        self.assertEqual([change["on"] for change in result["changes"]],
                         ["2026-10-22", "2026-10-21", "2026-10-20", "2026-10-19", "2026-10-18"])
        self.assertEqual(result["changes"][0], {"on": "2026-10-22", "change": "reinforce", "note": "PRIVATE note 6"})
        self.assertEqual((result["status"], result["label"]), ("ok", "own_desire_untrusted"))
        self.assertIn("untrusted", result["note"])
        self.connection.response.create.assert_awaited_once_with()
        self.assertEqual(self.log.call_args_list, [
            call("Desire recall requested: call_id=call_desire", "Recalling a desire"),
            call("Desire recall complete: call_id=call_desire, status=ok", "Desire recall complete"),
        ])

    async def test_an_unknown_wish_lists_the_held_ones(self):
        await self.tool.handle("call_desire", {"wish": "To learn the cello"})
        result = self.result()
        self.assertEqual((result["status"], result["known_wishes"]),
                         ("not_found", ["To walk through an old-growth forest."]))
        self.store.return_value.history.assert_not_called()
        self.engine.dispose.assert_called_once_with()

    async def test_failures_redact_continue_with_error_and_dispose(self):
        for boundary in (self.build, self.store.return_value.held_desires, self.store.return_value.history,
                         self.grew_from):
            with self.subTest(boundary=boundary):
                self.engine.dispose.reset_mock()
                self.connection.response.item.create.reset_mock()
                self.connection.response.create.reset_mock()
                self.log.reset_mock()
                boundary.side_effect = RuntimeError("PRIVATE details")
                try:
                    await self.tool.handle("call_desire", {"wish": "To walk through an old-growth forest."})
                    self.assertEqual(self.result(), ERROR_RESULT)
                    self.connection.response.create.assert_awaited_once_with()
                    self.log.assert_any_call("Desire recall failed (RuntimeError)", "Tool error")
                    self.assertNotIn("PRIVATE", str(self.log.call_args_list))
                    if boundary is not self.build:
                        self.engine.dispose.assert_called_once_with()
                finally:
                    boundary.side_effect = None

    async def test_closing_or_replaced_connection_skips_output(self):
        for closing, current in ((True, self.connection), (False, None)):
            self.closing, self.current = closing, current
            await self.tool.handle("call_desire", {"wish": "To walk through an old-growth forest."})
        self.build.assert_not_called()
        self.closing, self.current = False, self.connection
        self.store.return_value.held_desires.side_effect = lambda now: setattr(self, "current", object()) or [self.held]
        await self.tool.handle("call_desire", {"wish": "To walk through an old-growth forest."})
        self.connection.response.item.create.assert_not_called()
        self.connection.response.create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
