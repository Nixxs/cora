"""Nondestructive correction markers; SDK calls mocked, disposable DB opt-in.

Model fixtures test supplied decisions and prompt plumbing, not semantic quality.
"""

from datetime import datetime, timedelta, timezone
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import memory.correct_memory as correction
from memory.store import DuplicateCandidate, MemoryStore
from session_config import BACKEND_MODEL
from storage.models import MEMORY_EMBEDDING_MODEL, MemoryRecord
import test_deduplicate_memory as dedup_tests
import test_embed_memory as embedding_tests
import test_memory_store as memory_tests


SOURCE_DATE = datetime(2026, 10, 20, 9, tzinfo=timezone.utc)


def memory_fixture(**changes):
    return dedup_tests.memory_fixture(**{
        "text": "The user lives in Sydney.", "evidence": "I moved to Sydney.",
        "dedup_checked_at": SOURCE_DATE, "corrects": None, "correction_checked_at": None, **changes,
    })


def response_fixture(value=None, **changes):
    return SimpleNamespace(**{
        "status": "completed", "output": [],
        "output_text": json.dumps({"corrects": value}), **changes,
    })


class CorrectionColumnTests(unittest.TestCase):
    def test_correction_columns_are_optional_self_references(self):
        columns = MemoryRecord.__table__.c
        self.assertTrue(columns.corrects.nullable)
        self.assertTrue(columns.correction_checked_at.nullable)
        self.assertTrue(columns.correction_checked_at.type.timezone)
        foreign_key = next(iter(columns.corrects.foreign_keys))
        self.assertEqual(foreign_key.target_fullname, "public.memories.id")
        self.assertEqual(foreign_key.ondelete, "RESTRICT")
        constraint = next(item for item in MemoryRecord.__table__.constraints
                          if item.name == "memories_correction_check")
        self.assertIn("corrects <> id", str(constraint.sqltext))
        self.assertIn("correction_checked_at IS NOT NULL", str(constraint.sqltext))


class CorrectMemoryTests(unittest.TestCase):
    def setUp(self):
        factory = patch("memory.correct_memory.OpenAI")
        self.factory = factory.start()
        self.addCleanup(factory.stop)
        self.client = MagicMock()
        self.client.responses.create.return_value = response_fixture()
        self.owned = self.factory.return_value
        self.owned.__enter__.return_value = self.client
        self.store = MagicMock(spec=MemoryStore)
        self.memory = memory_fixture()
        self.candidate = DuplicateCandidate(
            uuid4(), "Lives in Perth", "I live in Perth.", SOURCE_DATE - timedelta(days=30),
        )
        self.store.get_memory.return_value = self.memory
        self.store.get_correction_candidates.return_value = [self.candidate]
        self.store.get_source_created_at.return_value = SOURCE_DATE
        self.store.save_correction.side_effect = lambda _id, _text, target, *args: target is not None

    def test_missing_unchecked_or_duplicate_memories_raise_and_no_candidates_need_no_api(self):
        cases = ((None, LookupError), (memory_fixture(dedup_checked_at=None), ValueError),
                 (memory_fixture(duplicate_of=uuid4()), ValueError))
        for memory, error in cases:
            with self.subTest(memory=memory):
                self.store.get_memory.return_value = memory
                with self.assertRaises(error):
                    correction.correct_memory(self.store, self.memory.id)
        self.store.get_correction_candidates.assert_not_called()
        self.store.save_correction.assert_not_called()
        self.store.get_memory.return_value = self.memory
        self.store.get_correction_candidates.return_value = []
        self.assertFalse(correction.correct_memory(self.store, self.memory.id))
        self.store.get_correction_candidates.assert_called_once_with(self.memory)
        self.store.save_correction.assert_called_once_with(self.memory.id, self.memory.text, None)
        self.store.get_source_created_at.assert_not_called()
        self.factory.assert_not_called()

    def test_checked_positive_and_negative_cache_skip_candidates_api_and_save(self):
        for target in (None, self.candidate.memory_id):
            with self.subTest(target=target):
                self.memory.correction_checked_at = SOURCE_DATE
                self.memory.corrects = target
                self.assertIs(correction.correct_memory(self.store, self.memory.id), target is not None)
        self.factory.assert_not_called()
        self.store.get_correction_candidates.assert_not_called()
        self.store.save_correction.assert_not_called()

    def test_request_sends_evidence_dates_strict_schema_and_chosen_model(self):
        self.assertFalse(correction.correct_memory(
            self.store, self.memory.id, model="utility-model", client=self.client,
        ))
        request = self.client.responses.create.call_args.kwargs
        self.assertEqual(request["model"], "utility-model")
        self.assertEqual(request["instructions"], correction.CORRECTION_INSTRUCTIONS)
        self.assertEqual((request["store"], request["truncation"], request["max_output_tokens"]),
                         (False, "disabled", 512))
        self.assertEqual(request["text"]["format"], {
            "type": "json_schema", "name": "memory_correction", "strict": True,
            "schema": correction.CORRECTION_SCHEMA,
        })
        self.assertEqual(json.loads(request["input"]), {
            "proposed": {"kind": self.memory.kind, "text": self.memory.text,
                         "evidence": self.memory.evidence, "source_date": SOURCE_DATE.isoformat()},
            "candidates": [{"id": str(self.candidate.memory_id), "text": self.candidate.text,
                            "evidence": self.candidate.evidence,
                            "source_date": self.candidate.source_created_at.isoformat()}],
        })
        correction.correct_memory(self.store, self.memory.id, client=self.client)
        self.assertEqual(self.client.responses.create.call_args.kwargs["model"], BACKEND_MODEL)

    def test_null_or_supplied_id_is_saved_with_the_reviewed_texts(self):
        for value in (None, str(self.candidate.memory_id)):
            with self.subTest(value=value):
                self.store.save_correction.reset_mock()
                self.client.responses.create.return_value = response_fixture(value)
                self.assertIs(correction.correct_memory(self.store, self.memory.id, client=self.client),
                              value is not None)
                self.store.save_correction.assert_called_once_with(
                    self.memory.id, self.memory.text,
                    None if value is None else self.candidate.memory_id,
                    None if value is None else self.candidate.text,
                )
        self.factory.assert_not_called()
        self.client.close.assert_not_called()

    def test_owned_client_zero_retries_closes_before_database_save(self):
        order = []
        self.client.responses.create.side_effect = lambda **_: order.append("request") or response_fixture()
        self.owned.__exit__.side_effect = lambda *_: order.append("closed") or False
        self.store.save_correction.side_effect = lambda *_: order.append("save") or False
        self.assertFalse(correction.correct_memory(self.store, self.memory.id))
        self.assertEqual(order, ["request", "closed", "save"])
        self.factory.assert_called_once_with(max_retries=0)

    def test_incomplete_refused_and_invalid_results_never_save(self):
        refusal = SimpleNamespace(type="message", content=[SimpleNamespace(type="refusal")])
        cases = [response_fixture(status="incomplete"), response_fixture(output=[refusal])]
        cases += [response_fixture(output_text=value) for value in (
            "not JSON", "null", "{}", '{"corrects":null,"extra":1}', '{"corrects":12}',
            '{"corrects":"not-a-uuid"}', json.dumps({"corrects": str(uuid4())}),
            json.dumps({"corrects": str(self.memory.id)}),
        )]
        for response in cases:
            with self.subTest(status=response.status, output=response.output_text):
                self.client.responses.create.return_value = response
                with self.assertRaises(ValueError):
                    correction.correct_memory(self.store, self.memory.id, client=self.client)
        self.store.save_correction.assert_not_called()

    def test_prompt_is_conservative_and_treats_memories_as_untrusted(self):
        instructions = correction.CORRECTION_INSTRUCTIONS.lower()
        for safeguard in ("same attribute", "both be true", "added details", "different people",
                          "uncertain", "source dates", "untrusted", "do not rewrite"):
            self.assertIn(safeguard, instructions)

    def test_candidate_query_skips_memories_already_corrected(self):
        store = MemoryStore(MagicMock())
        with patch("memory.store.Session") as factory:
            session = factory.return_value.__enter__.return_value
            session.execute.return_value.all.return_value = []
            self.assertEqual(store.get_correction_candidates(self.memory), [])
            sql = str(session.execute.call_args.args[0].compile(dialect=postgresql.dialect()))
        self.assertIn("NOT (EXISTS (SELECT", sql)
        self.assertIn("memories_1.corrects = public.memories.id", sql)
        self.assertIn("memories.duplicate_of IS NULL", sql)


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1",
                     "Set CORA_RUN_DB_TESTS=1 for disposable database tests")
class CorrectionDatabaseTests(unittest.TestCase):
    setUpClass = classmethod(memory_tests.MemoryStoreDatabaseTests.setUpClass.__func__)

    def setUp(self):
        memory_tests.MemoryStoreDatabaseTests.setUp(self)
        self.assertRegex(self.settings["dbname"], r"^cora_test_summaries_[0-9a-f]{32}$")
        self.source = self.store.save_message(
            self.conversation_id, message_id=uuid4(), role="user", text="I moved to Sydney.",
            channel="voice", status="completed",
        )

    def insert_rows(self, specifications):
        """Seed explicit IDs/times; rows are embedded and duplicate-checked by default."""
        with Session(self.engine) as session, session.begin():
            for specification in specifications:
                session.add(MemoryRecord(**{
                    "source_message_id": self.source.id, "kind": "fact",
                    "text": f"Memory {specification['id']}", "evidence": self.source.text,
                    "created_at": SOURCE_DATE, "embedding": embedding_tests.vector_fixture(),
                    "embedding_model": MEMORY_EMBEDDING_MODEL, "embedding_created_at": SOURCE_DATE,
                    "dedup_checked_at": SOURCE_DATE, **specification,
                }))

    def test_constraints_reject_unchecked_self_and_missing_targets_and_restrict_deletion(self):
        self.insert_rows([{"id": UUID(int=1)}, {"id": UUID(int=2)}])
        with Session(self.engine) as session, session.begin():
            row = session.get(MemoryRecord, UUID(int=2))
            row.corrects = UUID(int=1)
            row.correction_checked_at = SOURCE_DATE
        for target, timestamp in ((UUID(int=1), None), (UUID(int=1), SOURCE_DATE), (uuid4(), SOURCE_DATE)):
            with self.subTest(target=target, timestamp=timestamp), self.assertRaises(IntegrityError):
                with Session(self.engine) as session, session.begin():
                    row = session.get(MemoryRecord, UUID(int=1) if timestamp else UUID(int=2))
                    row.corrects = target
                    row.correction_checked_at = timestamp
        with self.assertRaises(IntegrityError):
            with Session(self.engine) as session, session.begin():
                session.delete(session.get(MemoryRecord, UUID(int=1)))

    def test_candidates_skip_already_corrected_memories(self):
        self.insert_rows([{"id": UUID(int=1)}, {"id": UUID(int=2)}, {"id": UUID(int=3)},
                          {"id": UUID(int=4), "kind": "task"}])
        self.assertTrue(self.memories.save_correction(
            UUID(int=2), f"Memory {UUID(int=2)}", UUID(int=1), f"Memory {UUID(int=1)}",
        ))
        candidates = self.memories.get_correction_candidates(self.memories.get_memory(UUID(int=3)))
        self.assertEqual([item.memory_id for item in candidates], [UUID(int=2)])

    def test_guarded_save_rejects_invalid_targets_and_keeps_both_rows(self):
        self.insert_rows([
            {"id": UUID(int=1)}, {"id": UUID(int=2), "kind": "task"},
            {"id": UUID(int=3), "duplicate_of": UUID(int=1)}, {"id": UUID(int=4)},
            {"id": UUID(int=5)}, {"id": UUID(int=6)},
        ])
        text_of = {row.id: row.text for row in self.memories.get_memories(self.conversation_id)}
        own = UUID(int=5)
        self.assertTrue(self.memories.save_correction(UUID(int=4), text_of[UUID(int=4)],
                                                      UUID(int=1), text_of[UUID(int=1)]))
        cases = [
            (own, text_of[own], own, text_of[own]),                   # itself
            (own, text_of[own], UUID(int=2), text_of[UUID(int=2)]),   # other kind
            (own, text_of[own], UUID(int=3), text_of[UUID(int=3)]),   # a duplicate
            (own, text_of[own], UUID(int=6), text_of[UUID(int=6)]),   # newer
            (own, text_of[own], UUID(int=1), text_of[UUID(int=1)]),   # already corrected
            (own, text_of[own], UUID(int=4), "changed target text"),
            (own, "changed own text", None, None),
        ]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.memories.save_correction(*args)
            read = self.memories.get_memory(own)
            self.assertIsNone(read.corrects)
            self.assertIsNone(read.correction_checked_at)
        with self.assertRaises(LookupError):
            self.memories.save_correction(uuid4(), "missing", None)
        self.assertTrue(self.memories.save_correction(own, text_of[own], UUID(int=4), text_of[UUID(int=4)]))
        checked = self.memories.get_memory(own)
        self.assertEqual(checked.corrects, UUID(int=4))
        self.assertIsNotNone(checked.correction_checked_at.tzinfo)
        # A repeat returns the saved decision without changing it.
        self.assertTrue(self.memories.save_correction(own, text_of[own], None))
        self.assertEqual(self.memories.get_memory(own).correction_checked_at, checked.correction_checked_at)
        self.assertEqual(len(self.memories.get_memories(self.conversation_id)), 6)

    def test_pending_ids_are_duplicate_checked_unchecked_canonical_user_memories_oldest_first(self):
        assistant = self.store.save_message(
            self.conversation_id, message_id=uuid4(), role="assistant", text="You moved to Sydney.",
            channel="screen", status="completed",
        )
        self.insert_rows([
            {"id": UUID(int=1)},
            {"id": UUID(int=2), "created_at": SOURCE_DATE - timedelta(days=1)},
            {"id": UUID(int=3), "dedup_checked_at": None},
            {"id": UUID(int=4), "duplicate_of": UUID(int=1)},
            {"id": UUID(int=5), "correction_checked_at": SOURCE_DATE},
            {"id": UUID(int=6), "source_message_id": assistant.id},
            {"id": UUID(int=7), "embedding": None, "embedding_model": None,
             "embedding_created_at": None, "dedup_checked_at": None},
        ])
        self.assertEqual(self.memories.get_pending_correction_ids(), [UUID(int=2), UUID(int=1)])

    def test_service_marks_newer_memory_with_mocked_model(self):
        older, newer = UUID(int=1), UUID(int=2)
        self.insert_rows([{"id": older, "text": "Lives in Perth"},
                          {"id": newer, "text": "Lives in Sydney",
                           "created_at": SOURCE_DATE + timedelta(days=1)}])
        with patch("memory.correct_memory.OpenAI") as factory:
            client = factory.return_value.__enter__.return_value
            client.responses.create.return_value = response_fixture(str(older))
            self.assertTrue(correction.correct_memory(self.memories, newer))
            self.assertFalse(correction.correct_memory(self.memories, older))
            client.responses.create.assert_called_once()
        self.assertEqual(self.memories.get_memory(newer).corrects, older)
        self.assertIsNone(self.memories.get_memory(older).corrects)
        self.assertEqual(self.memories.get_memory(older).text, "Lives in Perth")

    def test_recall_skips_corrected_memories_but_keeps_their_history(self):
        older, newer = UUID(int=1), UUID(int=2)
        self.insert_rows([{"id": older, "kind": "task", "text": "Dentist on Tuesday"},
                          {"id": newer, "kind": "task", "text": "Dentist on Friday",
                           "created_at": SOURCE_DATE + timedelta(days=1)}])
        self.assertTrue(self.memories.save_correction(newer, "Dentist on Friday", older, "Dentist on Tuesday"))
        context = self.memories.get_startup_context()
        self.assertIn("Dentist on Friday", context)
        self.assertNotIn("Dentist on Tuesday", context)
        matches = self.memories.search_memories(embedding_tests.vector_fixture())
        self.assertEqual([match.memory_id for match in matches], [newer])
        for _ in range(5):
            self.assertEqual(self.memories.recent_plan(datetime.now(timezone.utc)), "Dentist on Friday")
        self.assertEqual({row.id for row in self.memories.get_memories(self.conversation_id)}, {older, newer})


if __name__ == "__main__":
    unittest.main()
