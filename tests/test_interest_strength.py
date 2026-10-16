from datetime import datetime, timedelta, timezone
import unittest

from subconscious.interests.interest_strength import (
    ACTIVE_THRESHOLD, DORMANT_THRESHOLD, HALF_LIFE, INITIAL_STRENGTH,
    MAX_COUNTED_ELAPSED, MAX_LIVE_INTERESTS, effective_strength, has_room,
    needs_room, next_status, reinforce, settle,
)

START = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)


class EffectiveStrengthTests(unittest.TestCase):
    def test_decay_by_elapsed_time(self):
        cases = (
            ("no time", 0.8, timedelta(0), {}, 0.8),
            ("one half-life", 0.8, HALF_LIFE, {}, 0.4),
            ("two half-lives", 0.8, 2 * HALF_LIFE, {}, 0.2),
            ("clock backwards", 0.5, timedelta(days=-3), {}, 0.5),
            ("custom half-life", 1.0, timedelta(days=1), {"half_life": timedelta(days=1)}, 0.5),
        )
        for name, strength, elapsed, options, expected in cases:
            with self.subTest(name):
                self.assertAlmostEqual(
                    effective_strength(strength, START, START + elapsed, **options), expected,
                )

    def test_long_absence_is_capped(self):
        capped = effective_strength(0.8, START, START + MAX_COUNTED_ELAPSED)
        self.assertAlmostEqual(capped, 0.2)
        self.assertEqual(effective_strength(0.8, START, START + timedelta(days=365)), capped)

    def test_rejects_naive_timestamps_and_invalid_inputs(self):
        cases = (
            (0.5, datetime(2026, 10, 1), START, {}),
            (0.5, START, datetime(2026, 10, 2), {}),
            (1.5, START, START, {}),
            (float("nan"), START, START, {}),
            (0.5, START, START, {"half_life": timedelta(0)}),
        )
        for strength, strength_at, now, options in cases:
            with self.subTest(strength=strength, strength_at=strength_at, now=now, options=options):
                with self.assertRaises(ValueError):
                    effective_strength(strength, strength_at, now, **options)


class ReinforceTests(unittest.TestCase):
    def test_reinforcement_moves_a_fraction_of_remaining_room(self):
        self.assertAlmostEqual(reinforce(0.2, 0.15), 0.2 + 0.15 * 0.8)
        self.assertEqual(reinforce(0.4, 0.0), 0.4)
        # Strong interests gain less than weak ones.
        self.assertGreater(reinforce(0.2, 0.15) - 0.2, reinforce(0.8, 0.15) - 0.8)

    def test_never_exceeds_one(self):
        strength = 0.9
        for _ in range(100):
            strength = reinforce(strength, 0.15)
        self.assertLessEqual(strength, 1.0)
        self.assertEqual(reinforce(1.0, 0.5), 1.0)

    def test_rejects_invalid_values(self):
        for strength, delta in ((-0.1, 0.1), (0.5, -0.1), (0.5, 1.1), (True, 0.1), (0.5, float("inf"))):
            with self.subTest(strength=strength, delta=delta):
                with self.assertRaises(ValueError):
                    reinforce(strength, delta)


class StatusTests(unittest.TestCase):
    def test_status_transitions_with_hysteresis(self):
        self.assertGreater(INITIAL_STRENGTH, DORMANT_THRESHOLD)
        self.assertLess(INITIAL_STRENGTH, ACTIVE_THRESHOLD)
        cases = (
            ("emerging", INITIAL_STRENGTH, "emerging"),
            ("emerging", ACTIVE_THRESHOLD, "active"),
            ("emerging", 0.05, "dormant"),
            ("active", 0.05, "dormant"),
            ("dormant", 0.5, "active"),
            # Between the thresholds the current status is kept.
            ("active", 0.15, "active"),
            ("dormant", 0.15, "dormant"),
            ("active", DORMANT_THRESHOLD, "active"),
            ("active", DORMANT_THRESHOLD - 0.001, "dormant"),
            ("dormant", ACTIVE_THRESHOLD - 0.001, "dormant"),
        )
        for status, strength, expected in cases:
            with self.subTest(status=status, strength=strength):
                self.assertEqual(next_status(status, strength), expected)

    def test_rejects_unknown_status_and_invalid_strength(self):
        for status, strength in (("bored", 0.5), ("active", 2)):
            with self.subTest(status=status, strength=strength), self.assertRaises(ValueError):
                next_status(status, strength)


class SettleTests(unittest.TestCase):
    def test_settle_decays_and_updates_status(self):
        strength, status = settle(0.6, START, "active", START + HALF_LIFE)
        self.assertAlmostEqual(strength, 0.3)
        self.assertEqual(status, "active")
        self.assertEqual(settle(0.15, START, "emerging", START + HALF_LIFE)[1], "dormant")
        # Settling never revives a dormant interest, even a corrupt row above the threshold.
        self.assertEqual(settle(0.9, START, "dormant", START), (0.9, "dormant"))
        self.assertEqual(settle(0.2, START, "dormant", START + HALF_LIFE)[1], "dormant")

    def test_settling_daily_fades_an_unreinforced_interest(self):
        strength, status, at = 0.6, "active", START
        for day in range(1, 31):
            now = START + timedelta(days=day)
            strength, status = settle(strength, at, status, now)
            at = now
        self.assertEqual(status, "dormant")

    def test_daily_conversation_reinforcement_keeps_an_interest_active(self):
        strength, status, at = INITIAL_STRENGTH, "emerging", START
        for day in range(1, 31):
            now = START + timedelta(days=day)
            strength, status = settle(strength, at, status, now)
            strength = reinforce(strength, 0.15)
            status, at = next_status(status, strength), now
        self.assertEqual(status, "active")
        self.assertGreater(strength, 0.6)


class LiveCapTests(unittest.TestCase):
    def test_only_spawns_and_revivals_need_room(self):
        cases = (
            (None, "emerging", True),
            ("dormant", "active", True),
            ("emerging", "active", False),
            ("active", "active", False),
            ("active", "dormant", False),
            ("dormant", "dormant", False),
        )
        for before, after, expected in cases:
            with self.subTest(before=before, after=after):
                self.assertIs(needs_room(before, after), expected)

    def test_has_room_below_the_cap(self):
        self.assertTrue(has_room(0))
        self.assertTrue(has_room(MAX_LIVE_INTERESTS - 1))
        self.assertFalse(has_room(MAX_LIVE_INTERESTS))
        self.assertFalse(has_room(MAX_LIVE_INTERESTS + 1))

    def test_rejects_invalid_inputs(self):
        for args in ((None, "bored"), ("bored", "active"), ("active", None)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                needs_room(*args)
        for count in (-1, True, 1.0):
            with self.subTest(count=count), self.assertRaises(ValueError):
                has_room(count)


if __name__ == "__main__":
    unittest.main()
