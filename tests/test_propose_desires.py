from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import MagicMock
from uuid import uuid4

from subconscious.desires.desire_proposals import validate_reflection
from subconscious.desires.desire_rules import Change
from subconscious.desires.desire_store import Desire
from subconscious.desires.propose_desires import DESIRE_INSTRUCTIONS, DESIRE_SCHEMA, propose_desires
from subconscious.identity.identity_evidence import Experience
from subconscious.identity.identity_rules import Evidence
from subconscious.interests.interest_store import Interest
from test_summarize import response_fixture

NOW = datetime(2026, 11, 2, 9, tzinfo=timezone.utc)


def desire(status="held", strength=0.5):
    return Desire(uuid4(), "To walk through an old-growth forest", "I keep reading about them", strength,
                  NOW - timedelta(days=10), status, NOW - timedelta(days=20), None, None)


def interest():
    return Interest(uuid4(), "Forests", "Old-growth forests", "Came up", 0.4, NOW, "active", None, NOW, None, "own")


def experience(source="exploration"):
    return Experience(source, uuid4(), NOW, {"finding": "Some trees are 2,000 years old"})


def row(**changes):
    values = {"action": "form", "desire_id": None, "description": "To see a redwood",
              "motivation": "I want to stand next to something that old", "strength": None,
              "reason": "Grew from the forests interest", "interest_ids": ["i1"], "evidence_ids": ["e1"]}
    return {**values, **changes}


class ValidateReflectionTests(unittest.TestCase):
    def setUp(self):
        self.desires, self.interests, self.found = [desire(), desire("candidate")], [interest()], [experience()]

    def validate(self, *rows):
        return validate_reflection({"changes": list(rows)}, self.desires, self.interests, self.found)

    def test_short_ids_become_the_records_they_name(self):
        reinforce = row(action="reinforce", desire_id="D2", description=None, motivation=None, strength="strong",
                        interest_ids=[])
        let_go = row(action="let_go", desire_id="d1", description=None, motivation=None, interest_ids=[],
                     evidence_ids=[], reason="Not for me any more")
        self.assertEqual(self.validate(row(), reinforce, let_go), [
            Change("form", "Grew from the forests interest", description="To see a redwood",
                   motivation="I want to stand next to something that old", interest_ids=(self.interests[0].id,),
                   evidence=(Evidence("exploration", self.found[0].id),)),
            Change("reinforce", "Grew from the forests interest", desire_id=self.desires[1].id, strength="strong",
                   evidence=(Evidence("exploration", self.found[0].id),)),
            Change("let_go", "Not for me any more", desire_id=self.desires[0].id),
        ])
        self.assertEqual(self.validate(), [])

    def test_malformed_replies_are_rejected_whole(self):
        reinforce = dict(action="reinforce", desire_id="d1", description=None, motivation=None, interest_ids=[])
        cases = (
            ("extra field", [{**row(), "confidence": 0.3}]),
            ("unknown action", [row(action="pursue")]),
            ("two forms", [row(), row(description="To see a sequoia")]),
            ("four reinforcements", [row(**reinforce, strength="some")] * 4),
            ("unknown desire", [row(**{**reinforce, "desire_id": "d9"}, strength="some")]),
            ("unknown interest", [row(interest_ids=["i2"])]),
            ("unknown experience", [row(evidence_ids=["e2"])]),
            ("form from nothing", [row(interest_ids=[], evidence_ids=[])]),
            ("form with an id", [row(desire_id="d1")]),
            ("reinforce without strength", [row(**reinforce)]),
            ("reinforce without support", [row(**reinforce, strength="some", evidence_ids=[])]),
            ("let go with support", [row(action="let_go", desire_id="d1", description=None, motivation=None,
                                         interest_ids=[])]),
            ("blank reason", [row(reason=" ")]),
            ("long description", [row(description="To " + "x" * 300)]),
        )
        for name, rows in cases:
            with self.subTest(name), self.assertRaises(ValueError):
                self.validate(*rows)
        with self.assertRaises(ValueError):
            validate_reflection({"changes": [], "extra": 1}, self.desires, self.interests, self.found)


class ProposeDesiresTests(unittest.TestCase):
    def test_request_uses_strict_schema_and_shows_what_she_has(self):
        client = MagicMock()
        client.responses.create.return_value = response_fixture(json.dumps({"changes": [row()]}))
        wanted, liked, found = desire(), interest(), experience()
        changes = propose_desires(client, "chosen-model", [wanted], [liked], ["I like old machines."], [found],
                                  now=NOW)
        self.assertEqual([change.description for change in changes], ["To see a redwood"])
        request = client.responses.create.call_args.kwargs
        self.assertEqual((request["model"], request["instructions"], request["store"]),
                         ("chosen-model", DESIRE_INSTRUCTIONS, False))
        self.assertNotIn("tools", request)
        self.assertEqual(request["text"]["format"], {
            "type": "json_schema", "name": "cora_desires", "strict": True, "schema": DESIRE_SCHEMA})
        data = json.loads(request["input"])
        self.assertEqual(data["desires"], [{"id": "d1", "description": wanted.description,
                                            "motivation": wanted.motivation, "status": "held", "strength": 0.25}])
        self.assertEqual(data["interests"], [{"id": "i1", "title": "Forests", "description": "Old-growth forests",
                                              "origin": "own", "strength": 0.4}])
        self.assertEqual(data["traits"], ["I like old machines."])
        self.assertEqual(data["experiences"][0]["id"], "e1")

    def test_incomplete_or_refused_replies_and_no_experiences_raise(self):
        client = MagicMock()
        for fixture in (response_fixture("{}", status="incomplete"), response_fixture("{}", refusal=True)):
            client.responses.create.return_value = fixture
            with self.assertRaises(ValueError):
                propose_desires(client, "m", [], [], [], [experience()], now=NOW)
        with self.assertRaises(ValueError):
            propose_desires(client, "m", [], [], [], [], now=NOW)


if __name__ == "__main__":
    unittest.main()
