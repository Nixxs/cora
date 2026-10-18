"""Identity store: validation, CLI, plus opt-in disposable PostgreSQL tests."""

from contextlib import redirect_stdout
from datetime import datetime, timedelta
import io
import os
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from storage.models import (
    ConversationSummaryRecord, IdentityEventEvidenceRecord, IdentityEventTraitRecord,
    IdentityTraitRecord, InterestEventRecord,
)
from subconscious.identity import identity_store
from subconscious.identity.identity_rules import Change, Evidence, Trait
from subconscious.identity.identity_store import IdentityStore
from subconscious.interests.exploration_store import ExplorationStore
import test_exploration_store as exploration_tests
import test_interest_store as interest_tests

NOW = interest_tests.NOW


def form(evidence, **changes):
    values = {"action": "form", "reason": "Kept noticing it", "kind": "perspective",
              "statement": "Old machines show their reasoning.", "confidence": 0.3, "evidence": evidence}
    return Change(**{**values, **changes})


class IdentityStoreValidationTests(unittest.TestCase):
    def test_invalid_inputs_are_rejected_before_connecting(self):
        engine = MagicMock()
        store = IdentityStore(engine)
        evidence = (Evidence("exploration", uuid4()),)
        with patch("subconscious.identity.identity_store.Session") as session:
            for changes in (
                {"reason": ""}, {"reason": None}, {"reason": "x" * 1001}, {"statement": " "},
                {"statement": "x" * 301}, {"kind": "preference", "polarity": "like", "subject": "x" * 201},
            ):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    store.apply_reflection([form(evidence, **changes)], read_after=None, read_until=NOW, now=NOW)
            for times in ({"read_until": NOW, "now": datetime(2026, 10, 15)},
                          {"read_until": datetime(2026, 10, 15), "now": NOW}):
                with self.subTest(times=times), self.assertRaises(ValueError):
                    store.apply_reflection([], read_after=None, **times)
            for reason in ("", " ", "x" * 1001):
                with self.subTest(reason=reason), self.assertRaises(ValueError):
                    store.retire(uuid4(), reason, now=NOW)
            with self.assertRaises(ValueError):
                store.retire(uuid4(), "Because", now=datetime(2026, 10, 15))
            session.assert_not_called()
        engine.connect.assert_not_called()


class IdentityCliTests(unittest.TestCase):
    def run_cli(self, store, *args):
        output = io.StringIO()
        with patch("sys.argv", ["identity_store", *args]), patch.object(identity_store, "load_dotenv"), \
                patch.object(identity_store, "build_engine"), patch("sys.stderr", io.StringIO()), \
                patch.object(identity_store, "IdentityStore", return_value=store), redirect_stdout(output):
            identity_store.main()
        return output.getvalue()

    def test_list_shows_held_forming_and_preferences(self):
        held = Trait(uuid4(), "perspective", "Old machines show their reasoning.", None, None, 0.6, "active")
        liked = Trait(uuid4(), "preference", "I like engines.", "like", "Engines", 0.3, "active")
        store = MagicMock()
        store.list_traits.return_value = [held, liked]
        self.assertEqual(self.run_cli(store).splitlines(), [
            f"{held.id}  held    0.60  perspective: Old machines show their reasoning.",
            f"{liked.id}  forming 0.30  preference (like Engines): I like engines.",
        ])
        store.list_traits.assert_called_once_with(include_retired=False)
        store = MagicMock()
        store.list_traits.return_value = []
        self.assertEqual(self.run_cli(store, "list", "--all"), "No traits yet.\n")
        store.list_traits.assert_called_once_with(include_retired=True)

    def test_retire_needs_a_trait_id_and_reason(self):
        trait_id = uuid4()
        store = MagicMock()
        store.retire.return_value = None
        self.assertEqual(self.run_cli(store, "retire", str(trait_id), "--reason", "Not me"), "Already retired.\n")
        self.assertEqual(store.retire.call_args.args, (trait_id, "Not me"))
        self.assertIsNotNone(store.retire.call_args.kwargs["now"].tzinfo)
        store.retire.side_effect = LookupError
        with self.assertRaises(SystemExit) as exit:
            self.run_cli(store, "retire", str(trait_id), "--reason", "Not me")
        self.assertEqual(exit.exception.code, "No such trait.")
        for args in (("retire", str(trait_id)), ("retire", "not-an-id", "--reason", "Not me")):
            store = MagicMock()
            with self.subTest(args=args), self.assertRaises(SystemExit):
                self.run_cli(store, *args)
            store.retire.assert_not_called()


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class IdentityStoreDatabaseTests(unittest.TestCase):
    setUpClass = classmethod(interest_tests.summary_tests.SummaryDatabaseTests.setUpClass.__func__)

    def setUp(self):
        self.delete_identity()  # evidence references interests and summaries
        interest_tests.InterestStoreDatabaseTests.setUp(self)
        self.identity = IdentityStore(self.engine)
        interest = self.interests.spawn(*interest_tests.spawn_args(), now=NOW)
        explorations = ExplorationStore(self.engine)
        found = explorations.finish(explorations.start_next(now=NOW).exploration.id,
                                    *exploration_tests.FINDING, now=NOW)
        with Session(self.engine) as session, session.begin():
            session.add(ConversationSummaryRecord(conversation_id=self.conversation_id, text="We talked engines"))
            interest_event = session.scalar(select(InterestEventRecord.id).where(
                InterestEventRecord.interest_id == interest.id))
        self.finding = Evidence("exploration", found.id)
        self.evidence = (self.finding, Evidence("interest_event", interest_event),
                         Evidence("conversation", self.conversation_id))

    def delete_identity(self):
        with self.engine.begin() as connection:
            for table in ("identity_event_evidence", "identity_event_traits", "identity_events",
                          "identity_traits", "identity_worker_state"):
                connection.execute(text(f"DELETE FROM public.{table}"))

    def held(self, kind="perspective", statement="Held"):
        with Session(self.engine) as session, session.begin():
            record = IdentityTraitRecord(kind=kind, statement=statement, confidence=0.6)
            session.add(record)
            session.flush()
            return record.id

    def reflect(self, changes, now):
        return self.identity.apply_reflection(
            changes, read_after=self.identity.last_reflected_at(), read_until=now, now=now)

    def test_a_reflection_forms_then_revises_with_reasons_and_evidence(self):
        self.assertEqual((self.identity.list_traits(), self.identity.last_reflected_at()), ([], None))
        read_until = NOW - timedelta(minutes=5)
        self.assertEqual(self.identity.apply_reflection(
            [form(self.evidence + (self.finding,), statement="  Old machines show their reasoning. ")],
            read_after=None, read_until=read_until, now=NOW), [None])
        [formed] = self.identity.list_traits()
        self.assertEqual((formed.statement, formed.confidence, formed.status),
                         ("Old machines show their reasoning.", 0.3, "active"))
        self.assertEqual(self.identity.last_reflected_at(), read_until)  # how far it read, not now
        later = NOW + timedelta(days=1)
        # A reflection that started before that one finished saves nothing.
        self.assertIsNone(self.identity.apply_reflection(
            [form(self.evidence, statement="Stale.")], read_after=None, read_until=later, now=later))
        self.assertEqual((len(self.identity.list_traits()), self.identity.last_reflected_at()), (1, read_until))
        revise = Change("revise", "Saw it again", trait_id=formed.id, statement="Old machines explain themselves.",
                        confidence=0.5, evidence=(self.finding,))
        self.assertEqual(self.reflect([revise], later), [None])
        [revised] = self.identity.list_traits()
        self.assertEqual((revised.statement, revised.confidence), ("Old machines explain themselves.", 0.5))
        self.assertEqual(self.identity.last_reflected_at(), later)
        events = self.identity.history(formed.id)
        self.assertEqual([(e.kind, e.statement, e.confidence_before, e.confidence_after, e.reason, e.created_at)
                          for e in events], [
            ("form", "Old machines show their reasoning.", None, 0.3, "Kept noticing it", NOW),
            ("revise", "Old machines explain themselves.", 0.3, 0.5, "Saw it again", later),
        ])
        with Session(self.engine) as session:
            evidence = session.scalars(select(IdentityEventEvidenceRecord).where(
                IdentityEventEvidenceRecord.event_id == events[0].id)).all()
        self.assertEqual(len(evidence), 3)  # repeated evidence is saved once
        self.assertIsNone(self.identity.last_value_change_at())
        with self.assertRaises(LookupError):
            self.identity.history(uuid4())

    def test_rules_are_rechecked_against_the_traits_as_they_are_now(self):
        self.assertEqual(self.reflect([form(self.evidence)], NOW), [None])
        # A second reflection made from a stale read cannot add the same trait again.
        problems = self.reflect(
            [form(self.evidence), form(self.evidence, statement="Another.", confidence=0.5)],
            NOW + timedelta(days=1))
        self.assertIn("revise it instead", problems[0])
        self.assertIn("start at", problems[1])
        self.assertEqual(len(self.identity.list_traits()), 1)
        self.assertEqual(self.identity.last_reflected_at(), NOW + timedelta(days=1))  # stamped anyway

    def test_values_cite_held_traits_and_change_at_most_once_a_week(self):
        cited = (self.held(), self.held("tendency", "Also held"))
        value = form(self.evidence, kind="value", statement="I value clarity.", cited_trait_ids=cited)
        self.assertEqual(self.reflect([value], NOW), [None])
        self.assertEqual(self.identity.last_value_change_at(), NOW)
        [formed] = [trait for trait in self.identity.list_traits() if trait.kind == "value"]
        with Session(self.engine) as session:
            self.assertEqual(set(session.scalars(select(IdentityEventTraitRecord.trait_id))), set(cited))
        retire = Change("retire", "Not me after all", trait_id=formed.id)
        problems = self.reflect([retire], NOW + timedelta(days=6))
        self.assertIn("once a week", problems[0])
        self.assertEqual(self.reflect([retire], NOW + timedelta(days=7)), [None])
        self.assertEqual(self.identity.last_value_change_at(), NOW + timedelta(days=7))

    def test_retiring_by_hand_keeps_history_and_happens_once(self):
        self.reflect([form(self.evidence)], NOW)
        [trait] = self.identity.list_traits()
        retired = self.identity.retire(trait.id, "  Does not fit  ", now=NOW - timedelta(hours=1))  # clock back
        self.assertEqual(retired.status, "retired")
        self.assertEqual(self.identity.list_traits(), [])
        self.assertEqual(self.identity.list_traits(include_retired=True), [retired])
        with Session(self.engine) as session:
            record = session.get(IdentityTraitRecord, trait.id)
            self.assertEqual(record.retired_at, record.created_at)
        self.assertEqual([(e.kind, e.reason) for e in self.identity.history(trait.id)],
                         [("form", "Kept noticing it"), ("retire", "Does not fit")])
        self.assertIsNone(self.identity.retire(trait.id, "Again", now=NOW))
        self.assertEqual(self.identity.last_reflected_at(), NOW)  # retiring by hand is not a reflection
        with self.assertRaises(LookupError):
            self.identity.retire(uuid4(), "Because", now=NOW)


if __name__ == "__main__":
    unittest.main()
