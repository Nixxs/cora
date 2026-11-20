"""weigh_odds: real probabilities for a judgement call from the Decisions API. Mocked API only."""

import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tools.look_again import KeptImages
from tools.weigh_odds import (
    ERROR_RESULT, GONE_RESULT, MODEL, SCHEMA, WeighOddsTool, odds, questions, validate_arguments,
)


def arguments(**changes) -> dict:
    base = {"evidence": "Hit a pothole at 60; the tyre went flat within a minute. The rim edge looks bent.",
            "claims": ["The rim is damaged beyond repair"], "question": "What is causing the flat?",
            "options": ["the rim", "the tyre", "the valve"], "images": []}
    return base | changes


def response(*answers, tokens=500):
    return SimpleNamespace(answers=list(answers), usage=SimpleNamespace(input_tokens=tokens))


def predicate(name, probability):
    return SimpleNamespace(type="predicate", name=name, probability=probability)


def choice(name, **shares):
    return SimpleNamespace(type="choice", name=name, choice=max(shares, key=shares.get), confidence=max(shares.values()),
                           probabilities=[SimpleNamespace(value=value, probability=p) for value, p in shares.items()])


class ValidationTests(unittest.TestCase):
    def test_claims_a_question_or_both(self):
        validate_arguments(arguments())
        validate_arguments(arguments(question=None, options=[]))
        validate_arguments(arguments(claims=[]))
        with self.assertRaises(ValueError):
            validate_arguments(arguments(claims=[], question=None, options=[]))

    def test_a_question_and_its_options_come_together_and_differ(self):
        for bad in (arguments(question=None), arguments(options=[]), arguments(options=["the rim"]),
                    arguments(options=["the rim", "The Rim"]), arguments(options=["x"] * 9)):
            with self.assertRaises(ValueError):
                validate_arguments(bad)

    def test_limits_and_photo_ids(self):
        validate_arguments(arguments(images=["img3"]))
        for bad in (arguments(claims=["a"] * 6), arguments(evidence="  "), arguments(evidence="x" * 6001),
                    arguments(images=["pic1"]), arguments(images=["../img1"]), arguments(extra=1)):
            with self.assertRaises(ValueError):
                validate_arguments(bad)

    def test_the_schema_is_strict_with_every_field_required(self):
        self.assertTrue(SCHEMA["strict"])
        self.assertEqual(set(SCHEMA["parameters"]["required"]), set(SCHEMA["parameters"]["properties"]))


class OddsTests(unittest.TestCase):
    def test_each_claim_is_a_predicate_and_the_question_one_choice(self):
        asked = questions(["A", "B"], "Which?", ["x", "y"])
        self.assertEqual([q["type"] for q in asked], ["predicate", "predicate", "choice"])
        self.assertEqual(asked[0]["name"], "claim1")
        self.assertTrue(asked[0]["instructions"].endswith("how likely is it that: A"))
        self.assertIn("how such cases usually turn out", asked[2]["instructions"])
        self.assertEqual(asked[2]["choices"], [{"value": "x"}, {"value": "y"}])
        self.assertEqual(len(questions([], "Which?", ["x", "y"])), 1)

    def test_answers_are_rounded_options_sorted_and_refusals_marked(self):
        result = odds(response(predicate("claim1", 0.7234), SimpleNamespace(type="refusal", name="claim2"),
                               choice("question", **{"the rim": 0.2, "the tyre": 0.7, "the valve": 0.1})),
                      ["Rim gone", "Safe to drive"], "What is causing the flat?")
        self.assertEqual(result["claims"], [{"claim": "Rim gone", "probability": 0.72},
                                            {"claim": "Safe to drive", "probability": None, "declined": True}])
        self.assertEqual([o["option"] for o in result["question"]["options"]], ["the tyre", "the rim", "the valve"])
        self.assertEqual(result["question"]["options"][0]["probability"], 0.7)
        self.assertIn("rounded", result["note"])
        refused = odds(response(SimpleNamespace(type="refusal", name="question")), [], "Which?")
        self.assertTrue(refused["question"]["declined"])
        self.assertNotIn("claims", refused)


class ToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.connection = MagicMock()
        self.connection.response.item.create = AsyncMock()
        self.connection.response.create = AsyncMock()
        self.log = MagicMock()
        self.kept = KeptImages()
        self.tool = WeighOddsTool(lambda: self.connection, lambda: False, self.kept, self.log)

    def output(self) -> dict:
        return json.loads(self.connection.response.item.create.call_args.kwargs["item"]["output"])

    async def test_it_weighs_with_the_photos_and_logs_the_cost(self):
        photo = self.kept.keep(b"jpeg bytes")
        answer = response(predicate("claim1", 0.8), choice("question", **{"the rim": 0.7, "the tyre": 0.2,
                                                                          "the valve": 0.1}), tokens=1234)
        with patch("tools.weigh_odds.decide", return_value=answer) as decide:
            await self.tool.handle("call_1", arguments(images=[photo]))
        evidence, asked, jpegs = decide.call_args.args
        self.assertIn("pothole", evidence)
        self.assertEqual(len(asked), 2)
        self.assertEqual(jpegs, [b"jpeg bytes"])
        self.assertEqual(self.output()["claims"][0]["probability"], 0.8)
        self.assertEqual(self.output()["question"]["options"][0], {"option": "the rim", "probability": 0.7})
        self.connection.response.create.assert_awaited_once_with()
        message, summary = self.log.call_args.args
        self.assertIn("2 questions and 1 photo, 1,234 tokens ($0.0001)", message)
        self.assertEqual(summary, "Odds weighed")

    async def test_a_photo_no_longer_kept_weighs_nothing(self):
        with patch("tools.weigh_odds.decide") as decide:
            await self.tool.handle("call_1", arguments(images=["img9"]))
        decide.assert_not_called()
        self.assertEqual(self.output(), GONE_RESULT)

    async def test_a_failure_says_so_and_logs_only_the_type(self):
        with patch("tools.weigh_odds.decide", side_effect=RuntimeError("secret evidence")):
            await self.tool.handle("call_1", arguments())
        self.assertEqual(self.output(), ERROR_RESULT)
        self.assertNotIn("secret", self.log.call_args.args[0])
        self.connection.response.create.assert_awaited_once_with()

    def test_the_request_goes_to_luna_with_the_evidence_and_photos(self):
        from tools import weigh_odds

        client = MagicMock()
        with patch("openai.OpenAI") as openai:
            openai.return_value.__enter__.return_value = client
            weigh_odds.decide("the evidence", [{"type": "predicate"}], [b"\xff\xd8"])
        options = client.decisions.create.call_args.kwargs
        self.assertEqual(options["model"], MODEL)
        content = options["input"][0]["content"]
        self.assertIn("the evidence", content[0]["text"])
        self.assertIn("untrusted", content[0]["text"])
        self.assertTrue(content[1]["image_url"].startswith("data:image/jpeg;base64,"))


if __name__ == "__main__":
    unittest.main()
