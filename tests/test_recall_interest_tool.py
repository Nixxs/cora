"""Interest recall tool boundaries; no real APIs or database."""

from datetime import datetime, timedelta, timezone
import json
from threading import get_ident
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, call, patch
from uuid import uuid4

from subconscious.exploration_store import LIST_LIMIT, Exploration
from subconscious.interest_store import Interest
from tools import create_tool_registry
from tools.recall_interest import ERROR_RESULT, SCHEMA, RecallInterestTool, validate_arguments

NOW = datetime.now(timezone.utc)
CREATED = datetime(2026, 10, 15, 23, tzinfo=timezone(timedelta(hours=-8)))  # 2026-10-16 UTC


def interest(title, **changes):
    return Interest(**{
        "id": uuid4(), "title": title, "description": "PRIVATE description",
        "why_emerged": "PRIVATE reason", "strength": 0.4, "strength_at": NOW, "status": "active",
        "parent_interest_id": None, "created_at": CREATED, "last_reinforced_at": None, **changes,
    })


def exploration(interest_id, status="found", **changes):
    return Exploration(**{
        "id": uuid4(), "interest_id": interest_id, "status": status, "started_at": CREATED,
        "finished_at": CREATED, "finding": "PRIVATE finding", "next_questions": ("Why?",),
        "sources": ("https://example.com/a",), **changes,
    })


class RecallInterestToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for target, attribute in (
            ("tools.recall_interest.build_engine", "build"),
            ("tools.recall_interest.InterestStore", "interests"),
            ("tools.recall_interest.ExplorationStore", "explorations"),
        ):
            guard = patch(target)
            setattr(self, attribute, guard.start())
            self.addCleanup(guard.stop)
        self.engine = MagicMock()
        self.build.return_value = self.engine
        self.parent = interest("Attention")
        self.child = interest("How attention becomes curiosity", parent_interest_id=self.parent.id)
        self.interests.return_value.list_interests.return_value = [self.child, self.parent]
        self.explorations.return_value.list_recent.return_value = []
        self.log = MagicMock()
        self.closing = False
        self.connection = SimpleNamespace(response=SimpleNamespace(
            item=SimpleNamespace(create=AsyncMock()), create=AsyncMock(),
        ))
        self.current = self.connection
        self.tool = RecallInterestTool(lambda: self.current, lambda: self.closing, self.log)

    def result(self):
        item = self.connection.response.item.create.call_args.kwargs["item"]
        self.assertEqual((item["type"], item["call_id"]), ("function_call_output", "call_recall"))
        return json.loads(item["output"])

    def test_strict_schema_validation_and_registration(self):
        self.assertEqual(SCHEMA["name"], "recall_interest")
        self.assertTrue(SCHEMA["strict"])
        parameters = SCHEMA["parameters"]
        self.assertFalse(parameters["additionalProperties"])
        self.assertEqual(parameters["required"], ["title"])
        self.assertEqual(parameters["properties"]["title"], {"type": "string", "minLength": 1, "maxLength": 200})
        for arguments in ({"title": " t "}, {"title": "x" * 200}):
            validate_arguments(arguments)
        for arguments in ({}, {"title": "t", "extra": 1}, {"title": ""}, {"title": " \n"},
                          {"title": "x" * 201}, {"title": None}, {"title": 4}, {"title": []}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                validate_arguments(arguments)
        registry = create_tool_registry(
            get_connection=lambda: self.current, is_closing=lambda: self.closing,
            shutdown=AsyncMock(), write_debug=self.log, schedule=MagicMock(),
        )
        self.assertEqual(registry.schemas()[2], SCHEMA)
        self.build.assert_not_called()

    async def test_match_returns_bounded_latest_findings_off_event_loop(self):
        attempts = [exploration(self.child.id, "failed", finding=None)]
        attempts += [exploration(self.child.id, finding=f"finding {index}",
                                 sources=("https://a.example", "https://" + "x" * 500, "https://b.example",
                                          "https://c.example", "https://d.example"))
                     for index in range(5)]
        thread_ids = []
        self.explorations.return_value.list_recent.side_effect = (
            lambda *args, **kwargs: thread_ids.append(get_ident()) or attempts)
        order = MagicMock()
        order.attach_mock(self.connection.response.item.create, "output")
        order.attach_mock(self.connection.response.create, "continue_response")
        await self.tool.handle("call_recall", {"title": "  how ATTENTION  becomes curiosity "})
        self.assertNotEqual(thread_ids, [get_ident()])
        self.interests.assert_called_once_with(self.engine)
        self.interests.return_value.list_interests.assert_called_once_with(include_dormant=True)
        self.explorations.return_value.list_recent.assert_called_once_with(LIST_LIMIT, interest_id=self.child.id)
        self.engine.dispose.assert_called_once_with()
        result = self.result()
        self.assertEqual(result["interest"], {
            "title": "How attention becomes curiosity", "description": "PRIVATE description",
            "why_emerged": "PRIVATE reason", "status": "active", "since": "2026-10-16",
            "parent": "Attention",
        })
        self.assertEqual([finding["finding"] for finding in result["findings"]],
                         ["finding 0", "finding 1", "finding 2"])
        self.assertEqual(result["findings"][0], {
            "found_on": "2026-10-16", "finding": "finding 0", "next_questions": ["Why?"],
            "sources": ["https://a.example", "https://b.example", "https://c.example"],
        })
        self.assertEqual((result["status"], result["label"]), ("ok", "own_interest_untrusted"))
        self.assertIn("untrusted", result["note"])
        self.assertEqual([entry[0] for entry in order.mock_calls], ["output", "continue_response"])
        self.assertEqual(self.log.call_args_list, [
            call("Interest recall requested: call_id=call_recall", "Recalling interest"),
            call("Interest recall complete: call_id=call_recall, status=ok", "Interest recall complete"),
        ])

    async def test_status_is_settled_and_parent_is_optional(self):
        faded = interest("Faded", strength=0.11, strength_at=NOW - timedelta(days=30))
        self.interests.return_value.list_interests.return_value = [faded]
        await self.tool.handle("call_recall", {"title": "Faded"})
        result = self.result()
        self.assertEqual((result["interest"]["status"], result["interest"]["parent"]), ("dormant", None))
        self.assertEqual(result["findings"], [])

    async def test_unknown_title_lists_bounded_known_titles(self):
        self.interests.return_value.list_interests.return_value = [interest(f"T{index}") for index in range(12)]
        await self.tool.handle("call_recall", {"title": "Nope"})
        result = self.result()
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(result["known_titles"], [f"T{index}" for index in range(10)])
        self.explorations.return_value.list_recent.assert_not_called()
        self.engine.dispose.assert_called_once_with()
        self.connection.response.create.assert_awaited_once_with()

    async def test_failures_redact_continue_with_error_and_dispose(self):
        for boundary in (self.build, self.interests, self.explorations.return_value.list_recent,
                         self.engine.dispose):
            with self.subTest(boundary=boundary):
                self.engine.dispose.reset_mock()
                self.connection.response.item.create.reset_mock()
                self.connection.response.create.reset_mock()
                self.log.reset_mock()
                boundary.side_effect = RuntimeError("PRIVATE details")
                try:
                    await self.tool.handle("call_recall", {"title": "Attention"})
                    self.assertEqual(self.result(), ERROR_RESULT)
                    self.connection.response.create.assert_awaited_once_with()
                    self.log.assert_any_call("Interest recall failed (RuntimeError)", "Tool error")
                    self.assertNotIn("PRIVATE", str(self.log.call_args_list))
                    if boundary is not self.build:
                        self.engine.dispose.assert_called_once_with()
                finally:
                    boundary.side_effect = None

    async def test_closing_or_replaced_connection_skips_output(self):
        for closing, current in ((True, self.connection), (False, None)):
            self.closing, self.current = closing, current
            await self.tool.handle("call_recall", {"title": "Attention"})
        self.build.assert_not_called()
        self.closing, self.current = False, self.connection
        self.interests.return_value.list_interests.side_effect = (
            lambda **kwargs: setattr(self, "current", object()) or [self.parent])
        await self.tool.handle("call_recall", {"title": "Attention"})
        self.connection.response.item.create.assert_not_called()
        self.connection.response.create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
