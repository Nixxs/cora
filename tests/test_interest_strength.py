from datetime import datetime, timedelta, timezone
import unittest

from subconscious.interests.interest_strength import (
    ACTIVE_THRESHOLD, DORMANT_THRESHOLD, HALF_LIFE, INITIAL_STRENGTH,
    MAX_COUNTED_ELAPSED, MAX_LIVE_INTERESTS, effective_strength, has_room,
    needs_room, next_status, reinforce, settle,
)

START = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)


class EffectiveStrengthTests(unittest.TestCase):
    def test_no_elapsed_time_keeps_strength(self):
        self.assertEqual(effective_strength(0.8, START, START), 0.8)

    def test_one_half_life_halves_strength(self):
        self.assertAlmostEqual(effective_strength(0.8, START, START + HALF_LIFE), 0.4)

    def test_two_half_lives_quarter_strength(self):
        self.assertAlmostEqual(effective_strength(0.8, START, START + 2 * HALF_LIFE), 0.2)

    def test_long_absence_is_capped(self):
        capped = effective_strength(0.8, START, START + MAX_COUNTED_ELAPSED)
        self.assertAlmostEqual(capped, 0.2)
        self.assertEqual(effective_strength(0.8, START, START + timedelta(days=365)), capped)

    def test_clock_going_backwards_does_not_increase_strength(self):
        self.assertEqual(effective_strength(0.5, START, START - timedelta(days=3)), 0.5)

    def test_custom_half_life(self):
        result = effective_strength(1.0, START, START + timedelta(days=1), half_life=timedelta(days=1))
        self.assertAlmostEqual(result, 0.5)

    def test_rejects_naive_timestamps_and_invalid_inputs(self):
        with self.assertRaises(ValueError):
            effective_strength(0.5, datetime(2026, 10, 1), START)
        with self.assertRaises(ValueError):
            effective_strength(0.5, START, datetime(2026, 10, 2))
        with self.assertRaises(ValueError):
            effective_strength(1.5, START, START)
        with self.assertRaises(ValueError):
            effective_strength(float("nan"), START, START)
        with self.assertRaises(ValueError):
            effective_strength(0.5, START, START, half_life=timedelta(0))


class ReinforceTests(unittest.TestCase):
    def test_reinforcement_moves_a_fraction_of_remaining_room(self):
        self.assertAlmostEqual(reinforce(0.2, 0.15), 0.2 + 0.15 * 0.8)

    def test_strong_interests_gain_less_than_weak_ones(self):
        self.assertGreater(reinforce(0.2, 0.15) - 0.2, reinforce(0.8, 0.15) - 0.8)

    def test_never_exceeds_one(self):
        strength = 0.9
        for _ in range(100):
            strength = reinforce(strength, 0.15)
        self.assertLessEqual(strength, 1.0)
        self.assertEqual(reinforce(1.0, 0.5), 1.0)

    def test_zero_delta_changes_nothing(self):
        self.assertEqual(reinforce(0.4, 0.0), 0.4)

    def test_rejects_invalid_values(self):
        for strength, delta in ((-0.1, 0.1), (0.5, -0.1), (0.5, 1.1), (True, 0.1), (0.5, float("inf"))):
            with self.subTest(strength=strength, delta=delta):
                with self.assertRaises(ValueError):
                    reinforce(strength, delta)


class StatusTests(unittest.TestCase):
    def test_new_interest_starts_emerging_between_thresholds(self):
        self.assertGreater(INITIAL_STRENGTH, DORMANT_THRESHOLD)
        self.assertLess(INITIAL_STRENGTH, ACTIVE_THRESHOLD)
        self.assertEqual(next_status("emerging", INITIAL_STRENGTH), "emerging")

    def test_becoming_active_and_dormant(self):
        self.assertEqual(next_status("emerging", ACTIVE_THRESHOLD), "active")
        self.assertEqual(next_status("emerging", 0.05), "dormant")
        self.assertEqual(next_status("active", 0.05), "dormant")
        self.assertEqual(next_status("dormant", 0.5), "active")

    def test_hysteresis_keeps_status_between_thresholds(self):
        self.assertEqual(next_status("active", 0.15), "active")
        self.assertEqual(next_status("dormant", 0.15), "dormant")

    def test_threshold_boundaries(self):
        self.assertEqual(next_status("active", DORMANT_THRESHOLD), "active")
        self.assertEqual(next_status("active", DORMANT_THRESHOLD - 0.001), "dormant")
        self.assertEqual(next_status("dormant", ACTIVE_THRESHOLD - 0.001), "dormant")

    def test_rejects_unknown_status_and_invalid_strength(self):
        with self.assertRaises(ValueError):
            next_status("bored", 0.5)
        with self.assertRaises(ValueError):
            next_status("active", 2)

    def test_decay_then_status_fades_an_unreinforced_interest(self):
        strength = effective_strength(0.6, START, START + MAX_COUNTED_ELAPSED)
        self.assertEqual(next_status("active", strength), "active")
        strength = effective_strength(strength, START, START + MAX_COUNTED_ELAPSED)
        self.assertEqual(next_status("active", strength), "dormant")


class SettleTests(unittest.TestCase):
    def test_settle_decays_and_updates_status(self):
        strength, status = settle(0.6, START, "active", START + HALF_LIFE)
        self.assertAlmostEqual(strength, 0.3)
        self.assertEqual(status, "active")
        self.assertEqual(settle(0.15, START, "emerging", START + HALF_LIFE)[1], "dormant")

    def test_settle_never_revives_a_dormant_interest(self):
        # Even a corrupt dormant row above the active threshold stays dormant.
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
    def test_spawns_and_revivals_need_room(self):
        self.assertTrue(needs_room(None, "emerging"))
        self.assertTrue(needs_room("dormant", "active"))

    def test_live_changes_and_staying_dormant_do_not_need_room(self):
        self.assertFalse(needs_room("emerging", "active"))
        self.assertFalse(needs_room("active", "active"))
        self.assertFalse(needs_room("active", "dormant"))
        self.assertFalse(needs_room("dormant", "dormant"))

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
