"""Fading markers for time-bound memories; SDK calls mocked, disposable DB opt-in.

Rows are kept; recall skips faded ones. Model fixtures test plumbing, not semantic quality.
"""

from datetime import date, datetime, time, timedelta, timezone
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import memory.fade_memory as fading
from memory.store import MemoryStore
from session_config import BACKEND_MODEL
from storage.models import MemoryRecord
import test_deduplicate_memory as dedup_tests
import test_memory_store as memory_tests


SOURCE_DATE = datetime(2026, 10, 20, 9, tzinfo=timezone.utc)
SAID_ON = SOURCE_DATE.astimezone().date()


def memory_fixture(**changes):
    return dedup_tests.memory_fixture(**{
        "kind": "task", "text": "Dentist on Friday.", "evidence": "I've got the dentist on Friday.",
        "fades_at": None, "fade_checked_at": None, **changes,
    })


def response_fixture(value=None, **changes):
    return SimpleNamespace(**{
        "status": "completed", "output": [],
        "output_text": json.dumps({"fades_on": value}), **changes,
    })


def local_midnight_after(day):
    return datetime.combine(day + timedelta(days=1), time()).astimezone()


class FadeColumnTests(unittest.TestCase):
    def test_fade_columns_are_optional_timezone_timestamps(self):
        columns = MemoryRecord.__table__.c
        for column in (columns.fades_at, columns.fade_checked_at):
            self.assertTrue(column.nullable)
            self.assertTrue(column.type.timezone)
        constraint = next(item for item in MemoryRecord.__table__.constraints
                          if item.name == "memories_fade_check")
        self.assertIn("fade_checked_at IS NOT NULL", str(constraint.sqltext))


class FadeMemoryTests(unittest.TestCase):
    def setUp(self):
        factory = patch("memory.fade_memory.OpenAI")
        self.factory = factory.start()
        self.addCleanup(factory.stop)
        self.client = MagicMock()
        self.client.responses.create.return_value = response_fixture()
        self.owned = self.factory.return_value
        self.owned.__enter__.return_value = self.client
        self.store = MagicMock(spec=MemoryStore)
        self.memory = memory_fixture()
        self.store.get_memory.return_value = self.memory
        self.store.get_source_created_at.return_value = SOURCE_DATE
        self.store.save_fade.side_effect = lambda _id, _text, fades_at: fades_at

    def test_missing_or_duplicate_memories_raise_without_api(self):
        for memory, error in ((None, LookupError), (memory_fixture(duplicate_of=uuid4()), ValueError)):
            with self.subTest(memory=memory):
                self.store.get_memory.return_value = memory
                with self.assertRaises(error):
                    fading.fade_memory(self.store, self.memory.id)
        self.factory.assert_not_called()
        self.store.save_fade.assert_not_called()

    def test_checked_memories_return_saved_decision_without_api(self):
        for fades_at in (None, SOURCE_DATE):
            with self.subTest(fades_at=fades_at):
                self.memory.fade_checked_at = SOURCE_DATE
                self.memory.fades_at = fades_at
                self.assertEqual(fading.fade_memory(self.store, self.memory.id), fades_at)
        self.factory.assert_not_called()
        self.store.save_fade.assert_not_called()

    def test_request_sends_local_source_date_strict_schema_and_chosen_model(self):
        self.assertIsNone(fading.fade_memory(self.store, self.memory.id, model="utility-model",
                                             client=self.client))
        request = self.client.responses.create.call_args.kwargs
        self.assertEqual(request["model"], "utility-model")
        self.assertEqual(request["instructions"], fading.FADE_INSTRUCTIONS)
        self.assertEqual((request["store"], request["truncation"], request["max_output_tokens"]),
                         (False, "disabled", 512))
        self.assertEqual(request["text"]["format"], {
            "type": "json_schema", "name": "memory_fade", "strict": True, "schema": fading.FADE_SCHEMA,
        })
        self.assertEqual(json.loads(request["input"]), {
            "kind": "task", "text": self.memory.text, "evidence": self.memory.evidence,
            "source_date": SOURCE_DATE.astimezone().isoformat(),
        })
        fading.fade_memory(self.store, self.memory.id, client=self.client)
        self.assertEqual(self.client.responses.create.call_args.kwargs["model"], BACKEND_MODEL)

    def test_null_or_date_is_saved_as_lasting_or_local_midnight_after_that_day(self):
        friday = SAID_ON + timedelta(days=4)
        for value, expected in ((None, None), (SAID_ON.isoformat(), local_midnight_after(SAID_ON)),
                                (friday.isoformat(), local_midnight_after(friday))):
            with self.subTest(value=value):
                self.store.save_fade.reset_mock()
                self.client.responses.create.return_value = response_fixture(value)
                self.assertEqual(fading.fade_memory(self.store, self.memory.id, client=self.client), expected)
                self.store.save_fade.assert_called_once_with(self.memory.id, self.memory.text, expected)
        self.factory.assert_not_called()

    def test_owned_client_zero_retries_closes_before_database_save(self):
        order = []
        self.client.responses.create.side_effect = lambda **_: order.append("request") or response_fixture()
        self.owned.__exit__.side_effect = lambda *_: order.append("closed") or False
        self.store.save_fade.side_effect = lambda *_: order.append("save")
        fading.fade_memory(self.store, self.memory.id)
        self.assertEqual(order, ["request", "closed", "save"])
        self.factory.assert_called_once_with(max_retries=0)

    def test_incomplete_refused_and_invalid_results_never_save(self):
        refusal = SimpleNamespace(type="message", content=[SimpleNamespace(type="refusal")])
        earlier = (SAID_ON - timedelta(days=1)).isoformat()
        cases = [response_fixture(status="incomplete"), response_fixture(output=[refusal])]
        cases += [response_fixture(output_text=value) for value in (
            "not JSON", "null", "{}", '{"fades_on":null,"extra":1}', '{"fades_on":12}',
            '{"fades_on":"Friday"}', '{"fades_on":"20261023"}', '{"fades_on":"2026-02-30"}',
            json.dumps({"fades_on": earlier}),
        )]
        for response in cases:
            with self.subTest(status=response.status, output=response.output_text):
                self.client.responses.create.return_value = response
                with self.assertRaises(ValueError):
                    fading.fade_memory(self.store, self.memory.id, client=self.client)
        self.store.save_fade.assert_not_called()

    def test_prompt_is_conservative_and_treats_memories_as_untrusted(self):
        instructions = fading.FADE_INSTRUCTIONS.lower()
        for safeguard in ("time-bound", "lasting facts", "already happened", "uncertain",
                          "source date", "untrusted", "do not rewrite"):
            self.assertIn(safeguard, instructions)


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1",
                     "Set CORA_RUN_DB_TESTS=1 for disposable database tests")
class FadeDatabaseTests(unittest.TestCase):
    setUpClass = classmethod(memory_tests.MemoryStoreDatabaseTests.setUpClass.__func__)

    def setUp(self):
        memory_tests.MemoryStoreDatabaseTests.setUp(self)
        self.assertRegex(self.settings["dbname"], r"^cora_test_summaries_[0-9a-f]{32}$")
        self.source = self.store.save_message(
            self.conversation_id, message_id=uuid4(), role="user", text="Dentist on Friday.",
            channel="voice", status="completed",
        )
        with Session(self.engine) as session, session.begin():
            session.add(MemoryRecord(id=UUID(int=1), source_message_id=self.source.id, kind="task",
                                     text="Dentist on Friday", evidence=self.source.text))

    def test_constraint_needs_a_check_time_for_a_fade_date(self):
        with self.assertRaises(IntegrityError):
            with Session(self.engine) as session, session.begin():
                session.get(MemoryRecord, UUID(int=1)).fades_at = SOURCE_DATE

    def test_guarded_save_keeps_first_decision_and_the_row(self):
        with self.assertRaises(LookupError):
            self.memories.save_fade(uuid4(), "missing", None)
        with self.assertRaises(ValueError):
            self.memories.save_fade(UUID(int=1), "changed text", SOURCE_DATE)
        self.assertIsNone(self.memories.get_memory(UUID(int=1)).fade_checked_at)
        self.assertEqual(self.memories.save_fade(UUID(int=1), "Dentist on Friday", SOURCE_DATE), SOURCE_DATE)
        checked = self.memories.get_memory(UUID(int=1))
        self.assertEqual(checked.fades_at, SOURCE_DATE)
        self.assertIsNotNone(checked.fade_checked_at.tzinfo)
        self.assertEqual(self.memories.save_fade(UUID(int=1), "Dentist on Friday", None), SOURCE_DATE)
        self.assertEqual(self.memories.get_memory(UUID(int=1)).fade_checked_at, checked.fade_checked_at)

    def test_pending_ids_are_correction_checked_unfaded_canonical_user_memories_oldest_first(self):
        assistant = self.store.save_message(
            self.conversation_id, message_id=uuid4(), role="assistant", text="Enjoy the dentist.",
            channel="screen", status="completed",
        )
        with Session(self.engine) as session, session.begin():
            session.get(MemoryRecord, UUID(int=1)).correction_checked_at = SOURCE_DATE
            for number, changes in ((2, {"created_at": SOURCE_DATE - timedelta(days=1)}),
                                    (3, {"correction_checked_at": None}),
                                    (4, {"duplicate_of": UUID(int=1), "dedup_checked_at": SOURCE_DATE}),
                                    (5, {"fade_checked_at": SOURCE_DATE}),
                                    (6, {"source_message_id": assistant.id})):
                session.add(MemoryRecord(**{
                    "id": UUID(int=number), "source_message_id": self.source.id, "kind": "task",
                    "text": f"Memory {number}", "evidence": self.source.text,
                    "correction_checked_at": SOURCE_DATE, **changes,
                }))
        self.assertEqual(self.memories.get_pending_fade_ids(), [UUID(int=2), UUID(int=1)])

    def test_service_saves_date_with_mocked_model(self):
        day = date.today() + timedelta(days=3)
        with patch("memory.fade_memory.OpenAI") as factory:
            client = factory.return_value.__enter__.return_value
            client.responses.create.return_value = response_fixture(day.isoformat())
            self.assertEqual(fading.fade_memory(self.memories, UUID(int=1)), local_midnight_after(day))
            self.assertEqual(fading.fade_memory(self.memories, UUID(int=1)), local_midnight_after(day))
            client.responses.create.assert_called_once()
        self.assertEqual(self.memories.get_memory(UUID(int=1)).text, "Dentist on Friday")


if __name__ == "__main__":
    unittest.main()
