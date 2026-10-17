"""Appraisal validation: exact fields, allowed values and the amends rules."""

import unittest

from subconscious.relationship.appraisal import REASON_MAX_CHARS, Appraisal, validate_appraisal


def reply(**changes):
    values = {"setting": "everyday", "tone": "warm", "friction": "none", "cora_mistake": False,
              "repair": "none", "reason": "An easy chat about engines"}
    return {**values, **changes}


class ValidateAppraisalTests(unittest.TestCase):
    def test_valid_replies_become_appraisals_with_a_trimmed_reason(self):
        self.assertEqual(validate_appraisal(reply(reason="  An easy chat  ")),
                         Appraisal("everyday", "warm", "none", False, "none", "An easy chat"))
        for changes in (
            {"setting": "development", "tone": "curt", "friction": "at_situation"},
            {"friction": "at_cora", "tone": "hostile", "repair": "person"},
            {"cora_mistake": True, "repair": "cora"},
            {"friction": "at_cora", "cora_mistake": True, "repair": "both"},
            {"reason": "x" * REASON_MAX_CHARS},
        ):
            with self.subTest(changes=changes):
                appraisal = validate_appraisal(reply(**changes))
                for name, value in changes.items():
                    self.assertEqual(getattr(appraisal, name), value)

    def test_malformed_replies_are_rejected_whole(self):
        extra = {**reply(), "trust": 0.9}
        missing = reply()
        del missing["reason"]
        for payload, message in (
            (None, "fields"), ([], "fields"), (extra, "fields"), (missing, "fields"),
            (reply(setting="test"), "setting"), (reply(tone="cold"), "tone"), (reply(tone=["warm"]), "tone"),
            (reply(friction=None), "friction"), (reply(repair="apology"), "repair"),
            (reply(cora_mistake="false"), "cora_mistake"), (reply(cora_mistake=0), "cora_mistake"),
            (reply(reason=None), "reason"), (reply(reason="  "), "reason"),
            (reply(reason="x" * (REASON_MAX_CHARS + 1)), "longer"),
        ):
            with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, message):
                validate_appraisal(payload)

    def test_amends_must_match_what_needed_repairing(self):
        for changes, message in (
            ({"friction": "at_situation", "repair": "person"}, "aimed at Cora"),
            ({"friction": "none", "cora_mistake": True, "repair": "both"}, "aimed at Cora"),
            ({"repair": "cora"}, "her own mistake"),
            ({"friction": "at_cora", "repair": "both"}, "her own mistake"),
        ):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, message):
                validate_appraisal(reply(**changes))


if __name__ == "__main__":
    unittest.main()
