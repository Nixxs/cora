from datetime import datetime, timedelta, timezone
import unittest

from subconscious.desires.desire_rules import (
    HALF_LIFE, HELD_THRESHOLD, INITIAL_STRENGTH, LET_GO_THRESHOLD, MAX_CANDIDATES, MAX_COUNTED_ELAPSED,
    MAX_HELD, MAX_REINFORCE_DELTA, can_form, changed, current_strength, promoted, settle,
)
from subconscious.interests.interest_strength import reinforce

START = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)


class FadingTests(unittest.TestCase):
    def test_a_held_desire_nothing_reinforces_is_let_go_after_about_three_weeks(self):
        # Settled along the way, as the daily cycle does.
        middle = START + timedelta(days=12)
        strength, status = settle(0.5, START, "held", middle)
        self.assertEqual(settle(strength, middle, status, START + timedelta(days=20))[1], "held")
        strength, status = settle(strength, middle, status, START + timedelta(days=24))
        self.assertLess(strength, LET_GO_THRESHOLD)
        self.assertEqual(status, "let_go")

    def test_decay_uses_the_desire_half_life_and_caps_long_absences(self):
        self.assertAlmostEqual(current_strength(0.8, START, START + HALF_LIFE), 0.4)
        capped = current_strength(0.8, START, START + MAX_COUNTED_ELAPSED)
        self.assertEqual(current_strength(0.8, START, START + timedelta(days=365)), capped)

    def test_every_live_status_fades_and_ended_desires_never_change(self):
        later = START + MAX_COUNTED_ELAPSED
        for status in ("candidate", "held", "pursuing"):
            with self.subTest(status=status):
                self.assertEqual(settle(0.2, START, status, later)[1], "let_go")
        for status in ("fulfilled", "let_go"):
            with self.subTest(status=status):
                self.assertEqual(settle(0.6, START, status, later), (0.6, status))

    def test_settling_never_raises_strength_or_promotes(self):
        self.assertEqual(settle(0.9, START, "candidate", START), (0.9, "candidate"))
        self.assertEqual(settle(0.5, START, "held", START - timedelta(days=3)), (0.5, "held"))

    def test_rejects_unknown_statuses_bad_strengths_and_naive_times(self):
        for args in ((0.5, START, "wanted", START), (1.5, START, "held", START),
                     (0.5, datetime(2026, 11, 1), "held", START)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                settle(*args)


class PromotionAndCapTests(unittest.TestCase):
    def test_a_new_candidate_needs_two_full_reinforcements_to_be_held(self):
        once = reinforce(INITIAL_STRENGTH, MAX_REINFORCE_DELTA)
        self.assertEqual(promoted("candidate", once, 0), "candidate")
        self.assertEqual(promoted("candidate", reinforce(once, MAX_REINFORCE_DELTA), 0), "held")

    def test_a_candidate_waits_while_she_holds_the_most_she_can(self):
        self.assertEqual(promoted("candidate", HELD_THRESHOLD, MAX_HELD - 1), "held")
        self.assertEqual(promoted("candidate", 0.9, MAX_HELD), "candidate")

    def test_only_candidates_are_promoted(self):
        for status in ("held", "pursuing", "fulfilled", "let_go"):
            with self.subTest(status=status):
                self.assertEqual(promoted(status, 0.9, 0), status)

    def test_candidates_are_capped(self):
        self.assertTrue(can_form(MAX_CANDIDATES - 1))
        self.assertFalse(can_form(MAX_CANDIDATES))
        for count in (-1, True, 1.0):
            with self.subTest(count=count), self.assertRaises(ValueError):
                can_form(count)


class ChangeTests(unittest.TestCase):
    def test_allowed_changes(self):
        cases = (
            ("pursue", "held", "pursuing"), ("pause", "pursuing", "held"),
            ("fulfil", "held", "fulfilled"), ("fulfil", "pursuing", "fulfilled"),
            ("let_go", "candidate", "let_go"), ("let_go", "held", "let_go"), ("let_go", "pursuing", "let_go"),
        )
        for kind, before, after in cases:
            with self.subTest(kind=kind, before=before):
                self.assertEqual(changed(kind, before), after)

    def test_changes_that_do_not_apply_are_refused(self):
        cases = (("pursue", "candidate"), ("pursue", "pursuing"), ("pause", "held"), ("fulfil", "candidate"),
                 ("let_go", "fulfilled"), ("let_go", "let_go"), ("pursue", "fulfilled"), ("hold", "candidate"),
                 ("pursue", "wanted"))
        for kind, status in cases:
            with self.subTest(kind=kind, status=status), self.assertRaises(ValueError):
                changed(kind, status)


if __name__ == "__main__":
    unittest.main()
