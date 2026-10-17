"""Identity proposal validation: shape and known ids only; the rules decide the rest."""

from datetime import datetime, timezone
import unittest
from uuid import uuid4

from subconscious.identity.identity_evidence import Experience
from subconscious.identity.identity_proposals import MAX_CHANGES, validate_reflection
from subconscious.identity.identity_rules import Change, Evidence, Trait

AT = datetime(2026, 10, 17, 9, tzinfo=timezone.utc)


def trait(status="active"):
    return Trait(uuid4(), "perspective", f"Statement {uuid4()}", None, None, 0.6, status)


def experience(source="exploration"):
    return Experience(source, uuid4(), AT, {"finding": "Carburettors were tuned by ear"})


def row(**changes):
    values = {"action": "form", "trait_id": None, "kind": "perspective",
              "statement": "Old machines show their reasoning.", "polarity": None, "subject": None,
              "confidence": 0.3, "reason": "Kept noticing it", "cited_trait_ids": [], "evidence_ids": []}
    return {**values, **changes}


class ValidateReflectionTests(unittest.TestCase):
    def setUp(self):
        self.held, self.other, self.gone = trait(), trait(), trait("retired")
        self.traits = [self.held, self.other, self.gone]
        self.found, self.talk = experience(), experience("conversation")
        self.experiences = [self.found, self.talk]

    def validate(self, *rows):
        return validate_reflection({"changes": list(rows)}, self.traits, self.experiences)

    def test_valid_changes_become_cleaned_changes_with_typed_citations(self):
        self.assertEqual(self.validate(), [])
        changes = self.validate(
            row(statement="  Old machines show their reasoning. ", confidence=0,
                cited_trait_ids=[str(self.held.id), str(self.held.id).upper()],
                evidence_ids=[str(self.found.id), str(self.talk.id), str(self.found.id)]),
            row(action="revise", trait_id=str(self.held.id), kind=None, confidence=0.7,
                evidence_ids=[str(self.found.id)]),
            row(action="retire", trait_id=str(self.other.id), kind=None, statement=None, confidence=None,
                reason=" Faded "),
        )
        self.assertEqual(changes, [
            Change("form", "Kept noticing it", kind="perspective", statement="Old machines show their reasoning.",
                   confidence=0.0, cited_trait_ids=(self.held.id,),
                   evidence=(Evidence("exploration", self.found.id), Evidence("conversation", self.talk.id))),
            Change("revise", "Kept noticing it", trait_id=self.held.id,
                   statement="Old machines show their reasoning.", confidence=0.7,
                   evidence=(Evidence("exploration", self.found.id),)),
            Change("retire", "Faded", trait_id=self.other.id),
        ])
        self.assertIsInstance(changes[0].confidence, float)

    def test_rule_breaking_but_well_formed_changes_are_left_to_the_rules(self):
        [change] = self.validate(row(kind="belief", confidence=5, polarity="love"))
        self.assertEqual((change.kind, change.confidence, change.polarity), ("belief", 5.0, "love"))

    def test_malformed_replies_are_rejected_whole(self):
        unknown = str(uuid4())
        for payload in (None, [], {}, {"changes": None}, {"changes": [], "extra": 1},
                        {"changes": [row()] * (MAX_CHANGES + 1)}, {"changes": ["form"]}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                validate_reflection(payload, self.traits, self.experiences)
        retire = {"action": "retire", "kind": None, "statement": None, "confidence": None}
        for changes in (
            {"action": "grow"}, {"trait_id": str(self.held.id)}, {"action": "revise"},
            {"action": "revise", "trait_id": unknown}, {"action": "revise", "trait_id": str(self.gone.id)},
            {"action": "revise", "trait_id": "not-an-id"},
            {**retire, "trait_id": str(self.held.id), "statement": "Still here"},
            {**retire, "trait_id": str(self.held.id), "confidence": 0.1},
            {"confidence": True}, {"confidence": "0.3"}, {"statement": 3}, {"kind": ["value"]},
            {"reason": None}, {"reason": " "}, {"statement": ""}, {"statement": "x" * 301},
            {"cited_trait_ids": str(self.held.id)}, {"cited_trait_ids": [unknown]},
            {"cited_trait_ids": [str(self.gone.id)]}, {"cited_trait_ids": [5]},
            {"evidence_ids": [unknown]}, {"evidence_ids": None},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.validate(row(**changes))
        extra = {**row(), "note": "x"}
        missing = {key: value for key, value in row().items() if key != "subject"}
        for bad in (extra, missing):
            with self.subTest(fields=set(bad)), self.assertRaises(ValueError):
                self.validate(bad)


if __name__ == "__main__":
    unittest.main()
