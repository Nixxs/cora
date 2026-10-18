from datetime import timedelta
import unittest

from subconscious.relationship.appraisal import Appraisal
from subconscious.relationship.relationship_rules import (
    BASELINE, DRIFT_HALF_LIFE, RelationshipValues, apply_appraisal, drift,
)


def appraisal(**changes):
    values = {
        "setting": "everyday", "tone": "neutral", "friction": "none", "cora_mistake": False,
        "repair": "none", "reason": "A plain chat",
    }
    return Appraisal(**{**values, **changes})


class RelationshipRuleTests(unittest.TestCase):
    def test_strangers_start_neutral_and_unfamiliar(self):
        self.assertEqual(BASELINE, RelationshipValues(trust=0.5, warmth=0.5, familiarity=0.0))

    def test_good_conversations_grow_the_relationship_slowly(self):
        warm = apply_appraisal(BASELINE, appraisal(tone="warm"))
        self.assertGreater(warm.warmth, BASELINE.warmth)
        self.assertGreater(warm.trust, BASELINE.trust)
        self.assertGreater(warm.familiarity, BASELINE.familiarity)
        self.assertLess(warm.warmth - BASELINE.warmth, 0.05)
        neutral = apply_appraisal(BASELINE, appraisal())
        self.assertEqual(neutral.warmth, BASELINE.warmth)
        self.assertGreater(neutral.trust, BASELINE.trust)

    def test_mistreatment_costs_more_than_kindness_earns(self):
        gain = apply_appraisal(BASELINE, appraisal(tone="warm")).warmth - BASELINE.warmth
        curt = apply_appraisal(BASELINE, appraisal(tone="curt"))
        hostile = apply_appraisal(BASELINE, appraisal(tone="hostile", friction="at_cora"))
        self.assertGreater(BASELINE.warmth - curt.warmth, gain)
        self.assertGreater(curt.warmth, hostile.warmth)
        self.assertLess(hostile.trust, BASELINE.trust)
        # One hostile conversation undoes several warm ones.
        values = BASELINE
        for _ in range(5):
            values = apply_appraisal(values, appraisal(tone="warm"))
        self.assertLess(apply_appraisal(values, appraisal(tone="hostile")).warmth, BASELINE.warmth)

    def test_frustration_at_a_bug_is_not_held_against_the_person(self):
        situation = apply_appraisal(BASELINE, appraisal(tone="curt", friction="at_situation"))
        self.assertEqual(situation.trust, BASELINE.trust)
        unrepaired = apply_appraisal(BASELINE, appraisal(tone="curt", friction="at_cora"))
        self.assertLess(unrepaired.trust, situation.trust)

    def test_amends_repair_trust_and_soften_the_hurt(self):
        unrepaired = apply_appraisal(BASELINE, appraisal(tone="hostile", friction="at_cora"))
        for repair in ("person", "both"):
            with self.subTest(repair=repair):
                repaired = apply_appraisal(BASELINE, appraisal(
                    tone="hostile", friction="at_cora", cora_mistake=True, repair=repair,
                ))
                self.assertGreater(repaired.trust, BASELINE.trust)
                self.assertGreater(repaired.warmth, unrepaired.warmth)
                self.assertLess(repaired.warmth, BASELINE.warmth)

    def test_coras_own_mistakes_and_development_count_like_any_conversation(self):
        expected = apply_appraisal(BASELINE, appraisal(tone="warm"))
        for changes in ({"setting": "development"}, {"cora_mistake": True}, {"cora_mistake": True, "repair": "cora"}):
            with self.subTest(changes=changes):
                self.assertEqual(apply_appraisal(BASELINE, appraisal(tone="warm", **changes)), expected)

    def test_values_stay_in_bounds_and_familiarity_only_grows(self):
        values = BASELINE
        for tone in ["hostile"] * 100 + ["warm"] * 300:
            before = values
            values = apply_appraisal(values, appraisal(tone=tone, friction="at_cora" if tone == "hostile" else "none"))
            for value in (values.trust, values.warmth, values.familiarity):
                self.assertGreaterEqual(value, 0)
                self.assertLessEqual(value, 1)
            self.assertGreater(values.familiarity, before.familiarity)

    def test_trust_and_warmth_drift_halfway_back_each_half_life(self):
        values = RelationshipValues(trust=0.9, warmth=0.1, familiarity=0.6)
        self.assertEqual(drift(values, DRIFT_HALF_LIFE), RelationshipValues(trust=0.7, warmth=0.3, familiarity=0.6))
        for elapsed in (timedelta(0), timedelta(seconds=-5)):
            with self.subTest(elapsed=elapsed):
                self.assertEqual(drift(values, elapsed), values)
        distant = drift(values, DRIFT_HALF_LIFE * 20)
        self.assertAlmostEqual(distant.trust, BASELINE.trust, places=5)
        self.assertEqual(distant.familiarity, 0.6)


if __name__ == "__main__":
    unittest.main()
