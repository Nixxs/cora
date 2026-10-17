"""Pure identity rules: no database, model or clock."""

from datetime import datetime, timedelta, timezone
import unittest
from uuid import uuid4

from subconscious.identity.identity_rules import (
    FORM_MAX_CONFIDENCE, MAX_ACTIVE, MAX_HELD, MAX_LEARNED_VALUES, VALUE_CHANGE_GAP,
    Change, Evidence, Trait, check_change, check_reflection, is_held,
)

NOW = datetime(2026, 10, 17, 9, tzinfo=timezone.utc)
FINDING = Evidence("exploration", uuid4())
TALK = Evidence("conversation", uuid4())


def trait(kind="perspective", statement=None, confidence=0.6, status="active", polarity=None, subject=None):
    return Trait(uuid4(), kind, statement or f"Statement {uuid4()}", polarity, subject, confidence, status)


def form(**changes):
    values = {"action": "form", "reason": "Because", "kind": "perspective",
              "statement": "Old machines show their reasoning.", "confidence": 0.3, "evidence": (FINDING,)}
    return Change(**{**values, **changes})


def revise(target, **changes):
    values = {"action": "revise", "reason": "Because", "trait_id": target.id, "statement": target.statement,
              "polarity": target.polarity, "confidence": target.confidence + 0.1, "evidence": (FINDING,)}
    return Change(**{**values, **changes})


class TraitRuleTests(unittest.TestCase):
    def test_held_means_active_at_half_confidence_or_more(self):
        self.assertTrue(is_held(trait(confidence=0.5)))
        self.assertFalse(is_held(trait(confidence=0.49)))
        self.assertFalse(is_held(trait(confidence=0.9, status="retired")))

    def test_new_traits_start_below_held_and_need_her_own_experience(self):
        like = {"kind": "preference", "polarity": "like", "subject": "Engines"}
        self.assertIsNone(check_change(form(confidence=FORM_MAX_CONFIDENCE), []))
        self.assertIsNone(check_change(form(**like), []))
        self.assertIsNone(check_change(form(kind="tendency", evidence=(TALK,)), []))  # how she talks
        for changes, problem in (
            ({"confidence": 0.5}, "start at"),
            ({"confidence": float("nan")}, "finite"),
            ({"confidence": None}, "finite"),
            ({"statement": None}, "statement is required"),
            ({"kind": "belief"}, "Unknown trait kind"),
            ({"kind": "preference"}, "like or dislike"),
            ({**like, "polarity": "love"}, "like or dislike"),
            ({"polarity": "like"}, "Only preferences"),
            ({"evidence": ()}, "Evidence is required"),
            ({"evidence": (TALK,)}, "her own experience"),
            ({"evidence": (Evidence("memory", uuid4()),)}, "Unknown evidence source"),
            ({"action": "grow"}, "Unknown action"),
        ):
            with self.subTest(changes=changes):
                self.assertIn(problem, check_change(form(**changes), []))

    def test_duplicates_and_contradictions_must_revise_the_existing_trait(self):
        existing = trait(statement="Old machines show their reasoning.")
        liked = trait(kind="preference", polarity="like", subject="Engines")
        self.assertIn("revise it", check_change(form(statement=" old MACHINES show their reasoning. "), [existing]))
        self.assertIn("revise it", check_change(
            form(kind="preference", polarity="dislike", subject="engines", statement="I dislike engines."),
            [liked]))
        retired = trait(statement="Old machines show their reasoning.", status="retired")
        self.assertIsNone(check_change(form(), [retired]))

    def test_active_and_value_limits(self):
        full = [trait(confidence=0.2) for _ in range(MAX_ACTIVE)]
        self.assertIn("Too many active", check_change(form(), full))
        held = [trait(), trait(kind="tendency")]
        values = [trait(kind="value") for _ in range(MAX_LEARNED_VALUES)]
        value = form(kind="value", cited_trait_ids=tuple(item.id for item in held))
        self.assertIsNone(check_change(value, held))
        self.assertIn("learned values", check_change(value, held + values))

    def test_values_rest_on_at_least_two_other_held_non_value_traits(self):
        held, other, weak = trait(), trait(kind="tendency"), trait(confidence=0.4)
        value = trait(kind="value")
        traits = [held, other, weak, value]
        for cited, problem in (
            ((held.id,), "at least 2"),
            ((held.id, weak.id), "other held traits"),
            ((held.id, uuid4()), "other held traits"),
            ((held.id, value.id), "not values"),
        ):
            with self.subTest(cited=cited):
                self.assertIn(problem, check_change(form(kind="value", cited_trait_ids=cited), traits))
        self.assertIn("other held traits", check_change(revise(held, cited_trait_ids=(held.id,)), traits))


class ReviseAndRetireTests(unittest.TestCase):
    def test_revisions_move_confidence_one_step_and_keep_kind_and_subject(self):
        target = trait(confidence=0.4)
        liked = trait(kind="preference", polarity="like", subject="Engines")
        traits = [target, liked]
        self.assertIsNone(check_change(revise(target, confidence=0.6, statement="Sharper wording."), traits))
        self.assertIsNone(check_change(revise(target, confidence=0.2), traits))
        self.assertIsNone(check_change(revise(liked, polarity="dislike"), traits))  # a change of heart
        for change, problem in (
            (revise(target, confidence=0.61), "at most 0.2"),
            (revise(target, kind="value"), "cannot change the trait's kind"),
            (revise(liked, subject="Boats"), "cannot change the preference's subject"),
            (revise(liked, polarity=None), "like or dislike"),
            (revise(target, statement=liked.statement), "Another trait"),
            (revise(target, evidence=()), "Evidence is required"),
            (revise(target, trait_id=uuid4()), "No such active trait"),
        ):
            with self.subTest(change=change):
                self.assertIn(problem, check_change(change, traits))

    def test_becoming_held_needs_room(self):
        rising = trait(confidence=0.45)
        full = [trait() for _ in range(MAX_HELD)]
        self.assertIn("Too many held", check_change(revise(rising, confidence=0.55), full + [rising]))
        self.assertIsNone(check_change(revise(full[0], confidence=0.7), full + [rising]))  # already held

    def test_retiring_needs_only_an_active_trait_and_a_reason(self):
        target, gone = trait(), trait(status="retired")
        self.assertIsNone(check_change(Change("retire", "No longer fits", trait_id=target.id), [target]))
        self.assertIn("No such active", check_change(Change("retire", "Again", trait_id=gone.id), [gone]))
        self.assertIn("No such active", check_change(revise(gone), [gone]))


class ReflectionTests(unittest.TestCase):
    def setUp(self):
        self.held = [trait(), trait(kind="tendency")]
        self.value = trait(kind="value")
        self.traits = [*self.held, self.value]

    def new_value(self):
        return form(kind="value", statement="I value clarity.", cited_trait_ids=tuple(t.id for t in self.held))

    def test_one_form_revise_and_retire_each_and_rejections_do_not_count(self):
        changes = [
            form(evidence=()), form(), form(statement="Another."),
            revise(self.held[0]), revise(self.held[1]),
            Change("retire", "Faded", trait_id=self.held[1].id), Change("retire", "Faded", trait_id=self.held[0].id),
        ]
        problems = check_reflection(changes, self.traits, last_value_change_at=None, now=NOW)
        self.assertEqual([problem is None for problem in problems], [False, True, False, True, False, True, False])
        self.assertIn("Only one form", problems[2])

    def test_values_change_at_most_once_a_week(self):
        changes = [self.new_value(), revise(self.value, cited_trait_ids=tuple(t.id for t in self.held))]
        problems = check_reflection(changes, self.traits, last_value_change_at=None, now=NOW)
        self.assertIsNone(problems[0])
        self.assertIn("once a week", problems[1])
        recent = NOW - VALUE_CHANGE_GAP + timedelta(seconds=1)
        retire = Change("retire", "No longer fits", trait_id=self.value.id)
        self.assertIn("once a week", check_reflection([retire], self.traits, last_value_change_at=recent, now=NOW)[0])
        self.assertEqual(check_reflection(
            [retire, form()], self.traits, last_value_change_at=NOW - VALUE_CHANGE_GAP, now=NOW), [None, None])

    def test_timestamps_must_be_timezone_aware(self):
        for last, now in ((None, datetime(2026, 10, 17)), (datetime(2026, 10, 1), NOW)):
            with self.subTest(last=last, now=now), self.assertRaises(ValueError):
                check_reflection([], [], last_value_change_at=last, now=now)


if __name__ == "__main__":
    unittest.main()
