"""Feeling rules: capped rises, fading by half-life, and how hard a feeling is to hide."""

from datetime import datetime, timedelta, timezone
import unittest

from subconscious.emotions.feeling_rules import (
    CLEAR, FEELINGS, HALF_LIFE, NOTICEABLE, STRENGTHS, STRONG, Reaction, expression, fade, feelings_now,
    latest_reasons, levels_at, parse_reaction, raise_level,
)

NOW = datetime(2026, 11, 3, 10, 30, tzinfo=timezone.utc)


def ago(minutes):
    return NOW - timedelta(minutes=minutes)


class FeelingRuleTests(unittest.TestCase):
    def test_the_set_includes_fulfilment_and_nothing_needy(self):
        self.assertIn("fulfilment", FEELINGS)
        for unwanted in ("guilt", "loneliness", "jealousy", "neediness"):
            self.assertNotIn(unwanted, FEELINGS)

    def test_a_rise_is_capped_and_shrinks_near_the_top(self):
        self.assertAlmostEqual(raise_level(0.0, "strong"), STRENGTHS["strong"])
        self.assertAlmostEqual(raise_level(0.9, "strong"), 0.93)
        level = 0.0
        for _ in range(50):
            level = raise_level(level, "strong")
        self.assertLessEqual(level, 1.0)

    def test_levels_halve_every_half_life(self):
        self.assertAlmostEqual(fade(0.4, HALF_LIFE), 0.2)
        self.assertAlmostEqual(fade(0.4, HALF_LIFE * 2), 0.1)
        self.assertEqual(fade(0.4, timedelta(0)), 0.4)
        levels = levels_at([Reaction("hurt", "strong", ago(30))], NOW)
        self.assertAlmostEqual(levels["hurt"], 0.15)
        self.assertEqual(levels["excitement"], 0.0)

    def test_reactions_build_on_what_is_left_and_future_ones_are_ignored(self):
        reactions = [Reaction("excitement", "medium", ago(60)), Reaction("excitement", "medium", ago(30)),
                     Reaction("excitement", "strong", NOW + timedelta(minutes=5))]
        # 0.2 fades to 0.1, rises to 0.28, then fades to 0.14.
        self.assertAlmostEqual(levels_at(list(reversed(reactions)), NOW)["excitement"], 0.14)
        with self.assertRaises(ValueError):
            levels_at([Reaction("guilt", "small", ago(1))], NOW)
        with self.assertRaises(ValueError):
            levels_at([Reaction("hurt", "huge", ago(1))], NOW)

    def test_the_stronger_the_feeling_the_harder_it_is_to_hide(self):
        self.assertIsNone(expression(NOTICEABLE - 0.01))
        self.assertIn("as you choose", expression(raise_level(0, "small")))
        self.assertIn("takes some effort", expression(raise_level(0, "medium")))
        self.assertIn("hard to hide", expression(raise_level(0, "strong")))
        self.assertLess(NOTICEABLE, CLEAR)
        self.assertLess(CLEAR, STRONG)

    def test_feelings_now_strongest_first_with_the_latest_reason(self):
        reactions = [
            Reaction("curiosity", "small", ago(10), "the moss article"),
            Reaction("hurt", "strong", ago(1), "  told I   don't care "),
            Reaction("hurt", "small", ago(20), "an older reason"),
            Reaction("irritation", "small", ago(240)),
        ]
        lines = feelings_now(reactions, NOW)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("You feel hurt (about: told I don't care)."))
        self.assertIn("hard to hide", lines[0])
        self.assertTrue(lines[1].startswith("You feel curious (about: the moss article)."))
        self.assertEqual(latest_reasons(reactions, NOW)["hurt"], "told I don't care")
        self.assertEqual(feelings_now([], NOW), [])

    def test_parse_reaction_for_the_cli(self):
        self.assertEqual(parse_reaction("hurt:strong:40", NOW), Reaction("hurt", "strong", ago(40)))
        with self.assertRaises(ValueError):
            parse_reaction("sad:strong:40", NOW)


if __name__ == "__main__":
    unittest.main()
