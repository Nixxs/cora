import unittest

from subconscious.relationship.relationship_context import describe_relationship
from subconscious.relationship.relationship_rules import BASELINE, RelationshipValues


class RelationshipContextTests(unittest.TestCase):
    def test_a_new_acquaintance_reads_as_friendly_and_undecided(self):
        self.assertEqual(describe_relationship(BASELINE), (
            "You have only just met the person. You feel friendly toward them, as you would toward "
            "most people. You have no particular reason yet to trust or distrust them."
        ))

    def test_each_value_picks_its_tier_at_the_boundaries(self):
        for values, expected in (
            (RelationshipValues(trust=0.55, warmth=0.55, familiarity=0.4),
             ("You know the person fairly well by now.", "You feel warm toward them", "You trust them.")),
            (RelationshipValues(trust=0.7, warmth=0.7, familiarity=0.7),
             ("share a lot of history.", "You are very fond of them.", "You trust them deeply.")),
            (RelationshipValues(trust=0.44, warmth=0.44, familiarity=0.15),
             ("still getting to know", "a little cooler toward them", "a little wary of them.")),
            (RelationshipValues(trust=0.0, warmth=0.0, familiarity=1.0),
             ("share a lot of history.", "hurt by them", "you are guarded.")),
        ):
            with self.subTest(values=values):
                text = describe_relationship(values)
                for part in expected:
                    self.assertIn(part, text)
        self.assertNotIn("0.", describe_relationship(RelationshipValues(0.55, 0.65, 0.3)))

    def test_familiarity_brings_banter_unless_shes_feeling_cool(self):
        self.assertNotIn("teas", describe_relationship(RelationshipValues(trust=0.6, warmth=0.6, familiarity=0.3)))
        self.assertIn("light teasing", describe_relationship(RelationshipValues(trust=0.6, warmth=0.6, familiarity=0.5)))
        close = describe_relationship(RelationshipValues(trust=0.6, warmth=0.6, familiarity=0.86))
        self.assertIn("cheeky", close)
        self.assertIn("drop it when they're down", close)
        self.assertNotIn("teas", describe_relationship(RelationshipValues(trust=0.6, warmth=0.44, familiarity=0.86)))


if __name__ == "__main__":
    unittest.main()
