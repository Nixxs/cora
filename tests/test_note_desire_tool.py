"""Desire note tool boundaries; no real APIs or database."""

from datetime import datetime, timezone
import json
from threading import get_ident
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, call, patch
from uuid import uuid4

from subconscious.desires.desire_store import Desire
from tools import create_tool_registry
from tools.note_desire import ERROR_RESULT, SCHEMA, NoteDesireTool, validate_arguments

FORMED = datetime(2026, 10, 16, tzinfo=timezone.utc)
REEF = "To snorkel the Great Barrier Reef"


def desire(wish, status="held", adapted_from=None):
    return Desire(uuid4(), wish, "PRIVATE motivation", 0.5, FORMED, status, FORMED, None, None, adapted_from)


def arguments(answer="yes", note="They'll come", instead_wish=None, instead_why=None, wish=REEF):
    return {"wish": wish, "answer": answer, "note": note, "instead_wish": instead_wish, "instead_why": instead_why}


class NoteDesireToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.build = self.enterContext(patch("tools.note_desire.build_engine"))
        self.store = self.enterContext(patch("tools.note_desire.DesireStore"))
        self.engine = MagicMock()
        self.build.return_value = self.engine
        self.held = desire(REEF)
        self.store.return_value.held_desires.return_value = [self.held]
        self.store.return_value.answer.return_value = (desire(REEF, "pursuing"), None)
        self.log = MagicMock()
        self.closing = False
        self.connection = SimpleNamespace(response=SimpleNamespace(
            item=SimpleNamespace(create=AsyncMock()), create=AsyncMock(),
        ))
        self.current = self.connection
        self.tool = NoteDesireTool(lambda: self.current, lambda: self.closing, self.log)

    def result(self):
        item = self.connection.response.item.create.call_args.kwargs["item"]
        self.assertEqual((item["type"], item["call_id"]), ("function_call_output", "call_note"))
        return json.loads(item["output"])

    def test_strict_schema_validation_and_registration(self):
        self.assertEqual(SCHEMA["name"], "note_desire")
        self.assertTrue(SCHEMA["strict"])
        parameters = SCHEMA["parameters"]
        self.assertFalse(parameters["additionalProperties"])
        self.assertEqual(set(parameters["required"]), set(parameters["properties"]))
        self.assertEqual(parameters["properties"]["answer"]["enum"], ["yes", "not_now", "instead", "note"])
        for valid in (arguments(), arguments("not_now"), arguments("note"),
                      arguments("instead", instead_wish="To snorkel at Rottnest", instead_why="Close by")):
            validate_arguments(valid)
        for invalid in ({}, {**arguments(), "extra": 1}, arguments("maybe"), arguments(note=" "),
                        arguments(wish="x" * 301), arguments(note="x" * 1001), arguments(instead_wish="To swim"),
                        arguments("instead"), arguments("instead", instead_wish="To swim", instead_why=" ")):
            with self.subTest(arguments=invalid), self.assertRaises(ValueError):
                validate_arguments(invalid)
        for phrase in ("'not_now'", "nearer version", "never press them", "always their choice"):
            self.assertIn(phrase, SCHEMA["description"])
        registry = create_tool_registry(
            get_connection=lambda: self.current, is_closing=lambda: self.closing,
            shutdown=AsyncMock(), request_standby=MagicMock(), write_debug=self.log, schedule=MagicMock(),
        )
        self.assertEqual(registry.schemas()[7], SCHEMA)
        self.build.assert_not_called()

    async def test_an_answer_is_recorded_on_the_matching_wish_off_the_event_loop(self):
        thread_ids = []
        self.store.return_value.answer.side_effect = lambda *args, **kwargs: (
            thread_ids.append(get_ident()) or (desire(REEF, "pursuing"), None))
        await self.tool.handle("call_note", arguments(wish="  to snorkel the  GREAT barrier reef ", note=" Yes! "))
        self.assertNotEqual(thread_ids, [get_ident()])
        args, kwargs = self.store.return_value.answer.call_args
        self.assertEqual(args, (self.held.id, "yes", "Yes!"))
        self.assertIsNone(kwargs["instead"])
        self.engine.dispose.assert_called_once_with()
        result = self.result()
        self.assertEqual((result["status"], result["desire"]), ("ok", {"wish": REEF, "status": "pursuing"}))
        self.assertNotIn("nearer_version", result)
        self.connection.response.create.assert_awaited_once_with()
        self.assertEqual(self.log.call_args_list, [
            call("Desire note requested: call_id=call_note, answer=yes", "Noting a desire"),
            call("Desire note complete: call_id=call_note, status=ok", "Desire note complete"),
        ])

    async def test_a_nearer_version_is_returned_and_a_refusal_says_why(self):
        rottnest = desire("To snorkel at Rottnest", adapted_from=self.held.id)
        self.store.return_value.answer.return_value = (self.held, rottnest)
        await self.tool.handle("call_note", arguments("instead", "Offered Rottnest", " To snorkel at Rottnest ", "Close"))
        self.assertEqual(self.store.return_value.answer.call_args.kwargs["instead"], ("To snorkel at Rottnest", "Close"))
        self.assertEqual(self.result()["nearer_version"], {"wish": "To snorkel at Rottnest", "status": "held"})
        self.store.return_value.answer.side_effect = ValueError("She already wants this")
        await self.tool.handle("call_note", arguments("instead", "Again", "To snorkel at Rottnest", "Close"))
        self.assertEqual(self.result(), {"status": "not_recorded", "reason": "She already wants this"})

    async def test_an_unknown_wish_lists_the_held_ones(self):
        await self.tool.handle("call_note", arguments(wish="To learn the cello"))
        result = self.result()
        self.assertEqual((result["status"], result["known_wishes"]), ("not_found", [REEF]))
        self.store.return_value.answer.assert_not_called()

    async def test_failures_redact_continue_with_error_and_dispose(self):
        for boundary in (self.build, self.store.return_value.held_desires):
            with self.subTest(boundary=boundary):
                self.connection.response.create.reset_mock()
                self.log.reset_mock()
                boundary.side_effect = RuntimeError("PRIVATE details")
                try:
                    await self.tool.handle("call_note", arguments())
                    self.assertEqual(self.result(), ERROR_RESULT)
                    self.connection.response.create.assert_awaited_once_with()
                    self.log.assert_any_call("Desire note failed (RuntimeError)", "Tool error")
                    self.assertNotIn("PRIVATE", str(self.log.call_args_list))
                finally:
                    boundary.side_effect = None

    async def test_closing_or_replaced_connection_skips_output(self):
        for closing, current in ((True, self.connection), (False, None)):
            self.closing, self.current = closing, current
            await self.tool.handle("call_note", arguments())
        self.build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
