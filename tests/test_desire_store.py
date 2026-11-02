"""Validation plus opt-in disposable PostgreSQL tests; never reset personal data."""

from datetime import datetime, timedelta, timezone
import json
import os
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from storage.models import DesireRecord, InterestEventRecord, InterestExplorationRecord, InterestRecord
from subconscious.desires.desire_rules import (
    REINFORCE_DELTAS, Change, INITIAL_STRENGTH, LET_GO_THRESHOLD, MAX_CANDIDATES, MAX_HELD, MAX_REINFORCE_DELTA,
)
from session_config import instruction_sections
from subconscious.desires.desire_store import DESIRE_CONTEXT_MAX_CHARS, FADE_NOTE, DesireStore, Sources
from subconscious.identity.identity_rules import Evidence
from subconscious.interests.interest_strength import reinforce
from tools.recall_desire import _grew_from
import test_conversation_summaries as summary_tests

NOW = datetime(2026, 11, 1, 9, tzinfo=timezone.utc)


def form_args(description="To walk through an old-growth forest"):
    return (description, "Reading about forests made me want to be in one", "Grew from the forests interest")


class DesireStoreValidationTests(unittest.TestCase):
    def test_invalid_inputs_are_rejected_before_connecting(self):
        store = DesireStore(MagicMock())
        with patch("subconscious.desires.desire_store.Session") as session:
            for args in (("", *form_args()[1:]), ("x" * 301, *form_args()[1:]), (form_args()[0], " ", form_args()[2]),
                         (*form_args()[:2], "x" * 1001), (*form_args()[:2], None)):
                with self.subTest(args=args), self.assertRaises(ValueError):
                    store.form(*args, now=NOW)
            with self.assertRaises(ValueError):
                store.form(*form_args(), now=datetime(2026, 11, 1))
            for delta in (0, -0.1, MAX_REINFORCE_DELTA + 0.001, True, float("nan"), "0.1"):
                with self.subTest(delta=delta), self.assertRaises(ValueError):
                    store.reinforce(uuid4(), delta, "Because", now=NOW)
            for call in (lambda: store.reinforce(uuid4(), 0.1, " ", now=NOW),
                         lambda: store.change(uuid4(), "pursue", "", now=NOW),
                         lambda: store.note_progress(uuid4(), "x" * 1001, now=NOW),
                         lambda: store.settle_all(datetime(2026, 11, 1))):
                with self.assertRaises(ValueError):
                    call()
            session.assert_not_called()


class DesirePromptTests(unittest.TestCase):
    def test_her_desires_come_last_as_her_own_wishes_and_are_capped(self):
        context = '[{"wish": "To see a redwood", "why": "I want to", "status": "held"}]'
        sections = instruction_sections(interest_context="[]", desire_context=context)
        self.assertEqual(list(sections), ["interests", "desires"])
        self.assertIn("your own wishes", sections["desires"])
        self.assertIn("don't press the person to help", sections["desires"])
        self.assertTrue(sections["desires"].endswith(context))
        self.assertNotIn("desires", instruction_sections())
        with self.assertRaises(ValueError):
            instruction_sections(desire_context="x" * (DESIRE_CONTEXT_MAX_CHARS + 1))


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class DesireStoreDatabaseTests(unittest.TestCase):
    setUpClass = classmethod(summary_tests.SummaryDatabaseTests.setUpClass.__func__)

    def setUp(self):
        # Only in the disposable DB created above. Children before parents.
        with self.engine.begin() as connection:
            for table in ("desire_worker_state", "desire_sources", "desire_events", "desires",
                          "interest_explorations", "interest_events", "interests"):
                connection.execute(text(f"DELETE FROM public.{table}"))
        summary_tests.SummaryDatabaseTests.setUp(self)
        self.desires = DesireStore(self.engine)

    def fill(self, count, now=NOW):
        return [self.desires.form(*form_args(f"Desire {index}"), now=now) for index in range(count)]

    def held(self, description="To walk through an old-growth forest", now=NOW):
        desire = self.desires.form(*form_args(description), now=now)
        self.desires.reinforce(desire.id, MAX_REINFORCE_DELTA, "Kept coming back to it", now=now)
        return self.desires.reinforce(desire.id, MAX_REINFORCE_DELTA, "Still want it", now=now)

    def kinds(self, desire_id):
        return [event.kind for event in self.desires.history(desire_id)]

    def test_forming_saves_a_candidate_its_first_event_and_what_it_grew_from(self):
        with Session(self.engine) as session, session.begin():
            interest = InterestRecord(title="Forests", description="Old-growth forests", why_emerged="Came up",
                                      strength=0.5, strength_at=NOW, status="active")
            session.add(interest)
            session.flush()
            finding = InterestExplorationRecord(interest_id=interest.id, status="found", started_at=NOW,
                                                finished_at=NOW, finding="Some trees are 2,000 years old")
            session.add(finding)
            session.flush()
            interest_id, finding_id = interest.id, finding.id
        self.store.close_conversation(self.conversation_id)
        self.store.save_summary(self.conversation_id, "We talked about forests.")
        desire = self.desires.form(*form_args(), now=NOW, sources=Sources(
            (interest_id, interest_id), (finding_id,), (self.conversation_id,)))
        self.assertEqual((desire.status, desire.strength, desire.ended_at), ("candidate", INITIAL_STRENGTH, None))
        [event] = self.desires.history(desire.id)
        self.assertEqual((event.kind, event.strength_before, event.note),
                         ("form", None, "Grew from the forests interest"))
        self.assertEqual(self.desires.sources(desire.id),
                         Sources((interest_id,), (finding_id,), (self.conversation_id,)))
        # What it grew from can't be deleted from under it.
        with self.engine.connect() as connection, self.assertRaises(IntegrityError):
            connection.execute(text("DELETE FROM public.interests"))

    def test_candidates_are_capped_but_faded_ones_make_room(self):
        self.fill(MAX_CANDIDATES)
        self.assertIsNone(self.desires.form(*form_args("One too many"), now=NOW))
        self.assertIsNotNone(self.desires.form(*form_args("Later"), now=NOW + timedelta(days=21)))

    def test_two_reinforcements_hold_a_desire_with_its_history(self):
        desire = self.held()
        self.assertEqual(desire.status, "held")
        self.assertAlmostEqual(desire.strength, reinforce(reinforce(INITIAL_STRENGTH, 0.15), 0.15))
        self.assertEqual(desire.last_reinforced_at, NOW)
        self.assertEqual(self.kinds(desire.id), ["form", "reinforce", "reinforce", "hold"])

    def test_a_candidate_waits_while_she_holds_three(self):
        for index in range(MAX_HELD):
            self.held(f"Held {index}")
        waiting = self.held("Waiting")
        self.assertEqual(waiting.status, "candidate")
        self.assertEqual(len([d for d in self.desires.list_desires() if d.status == "held"]), MAX_HELD)

    def test_pursuing_progress_and_fulfilling(self):
        desire = self.held()
        self.assertEqual(self.desires.change(desire.id, "pursue", "Found a forest walk nearby", now=NOW).status,
                         "pursuing")
        self.desires.note_progress(desire.id, "Asked Nick about a hike", now=NOW)
        fulfilled = self.desires.change(desire.id, "fulfil", "We went", now=NOW + timedelta(days=2))
        self.assertEqual(fulfilled.status, "fulfilled")
        self.assertIsNotNone(fulfilled.ended_at)
        self.assertEqual(self.kinds(desire.id)[-3:], ["pursue", "progress", "fulfil"])
        self.assertEqual(self.desires.list_desires(), [])
        self.assertEqual(len(self.desires.list_desires(include_ended=True)), 1)
        # Ended desires never change again.
        with self.assertRaises(ValueError):
            self.desires.change(desire.id, "pursue", "Again", now=NOW)
        with self.assertRaises(ValueError):
            self.desires.note_progress(desire.id, "More", now=NOW)
        self.assertEqual(self.desires.reinforce(desire.id, 0.1, "Again", now=NOW).status, "fulfilled")

    def test_changes_that_do_not_apply_are_refused_and_change_nothing(self):
        desire = self.desires.form(*form_args(), now=NOW)
        for kind in ("pursue", "pause", "fulfil", "hold"):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.desires.change(desire.id, kind, "Because", now=NOW)
        with self.assertRaises(ValueError):
            self.desires.note_progress(desire.id, "Too early", now=NOW)
        self.assertEqual(self.kinds(desire.id), ["form"])
        with self.assertRaises(LookupError):
            self.desires.change(uuid4(), "let_go", "Gone", now=NOW)

    def test_letting_go_on_purpose(self):
        desire = self.desires.form(*form_args(), now=NOW)
        gone = self.desires.change(desire.id, "let_go", "It was never really mine", now=NOW)
        self.assertEqual((gone.status, self.kinds(desire.id)), ("let_go", ["form", "let_go"]))

    def test_a_desire_nothing_reinforces_fades_and_is_let_go(self):
        desire = self.held()
        self.assertEqual(self.desires.settle_all(NOW + timedelta(days=12))[0].status, "held")
        later = NOW + timedelta(days=24)
        [faded] = self.desires.settle_all(later)
        self.assertEqual(faded.status, "let_go")
        self.assertLess(faded.strength, LET_GO_THRESHOLD)
        self.assertEqual(faded.ended_at, later)
        self.assertEqual(self.desires.history(desire.id)[-1].note, FADE_NOTE)
        # A desire that faded is returned let go, not revived or changed.
        self.assertEqual(self.desires.reinforce(desire.id, 0.15, "Too late", now=later).status, "let_go")
        self.assertEqual(self.desires.change(desire.id, "pursue", "Too late", now=later).status, "let_go")
        self.assertEqual(self.kinds(desire.id)[-1], "fade")

    def test_reinforcing_settles_first(self):
        desire = self.desires.form(*form_args(), now=NOW)
        after = self.desires.reinforce(desire.id, 0.15, "Thought of it again", now=NOW + timedelta(days=10))
        self.assertAlmostEqual(after.strength, reinforce(INITIAL_STRENGTH / 2, 0.15))
        self.assertEqual(after.status, "candidate")

    def test_a_reflection_settles_applies_what_the_rules_allow_and_stamps_how_far_it_read(self):
        with Session(self.engine) as session, session.begin():
            interest = InterestRecord(title="Forests", description="Old-growth forests", why_emerged="Came up",
                                      strength=0.5, strength_at=NOW, status="active")
            session.add(interest)
            session.flush()
            spawn = InterestEventRecord(interest_id=interest.id, kind="spawn", strength_after=0.2, reason="Came up")
            session.add(spawn)
            session.flush()
            interest_id, spawn_id = interest.id, spawn.id
        stale = self.desires.form(*form_args("To learn the cello"), now=NOW - timedelta(days=21))
        wanted = self.desires.form(*form_args(), now=NOW)
        renewed = (Evidence("interest_event", spawn_id),)
        changes = [
            Change("form", "Grew from forests", description="To see a redwood", motivation="I want to",
                   evidence=renewed),
            Change("form", "Again", description="  to walk through an OLD-GROWTH forest ", motivation="Same",
                   evidence=renewed),
            Change("reinforce", "Read more", desire_id=wanted.id, strength="strong", evidence=renewed),
            Change("let_go", "Changed my mind", desire_id=wanted.id),
            Change("reinforce", "Too late", desire_id=stale.id, strength="some", evidence=renewed),
        ]
        until = NOW - timedelta(minutes=5)
        self.assertEqual(self.desires.apply_reflection(changes, read_after=None, read_until=until, now=NOW), [
            None, "she already wants this", None, "only one change per desire in a reflection",
            "it is already let go",
        ])
        self.assertEqual(self.desires.last_reflected_at(), until)
        self.assertEqual(self.kinds(stale.id), ["form", "fade"])
        self.assertAlmostEqual(self.desires.list_desires()[0].strength,
                               reinforce(INITIAL_STRENGTH, REINFORCE_DELTAS["strong"]))
        [formed] = [d for d in self.desires.list_desires() if d.description == "To see a redwood"]
        # The interest change it cited counts as that interest.
        self.assertEqual(self.desires.sources(formed.id), Sources((interest_id,)))
        # A reflection that started from an older stamp saves nothing.
        self.assertIsNone(self.desires.apply_reflection([], read_after=None, read_until=NOW, now=NOW))
        self.assertEqual(self.desires.last_reflected_at(), until)

    def test_the_persons_yes_and_not_now_are_recorded(self):
        desire = self.held()
        later = NOW + timedelta(hours=1)
        yes, nearer = self.desires.answer(desire.id, "yes", "They'll come along", now=later)
        self.assertEqual((yes.status, nearer), ("pursuing", None))
        self.assertEqual(self.desires.answer(desire.id, "note", "Maybe in spring", now=later)[0].status, "pursuing")
        self.assertEqual(self.desires.answer(desire.id, "not_now", "Busy month", now=later)[0].status, "held")
        # Not now while only held is noted all the same, so her steps rest.
        self.assertEqual(self.desires.answer(desire.id, "not_now", "Still busy", now=later)[0].status, "held")
        self.assertEqual([(event.kind, event.note) for event in self.desires.history(desire.id)[-4:]], [
            ("pursue", "They'll come along"), ("progress", "Maybe in spring"),
            ("pause", "Busy month"), ("pause", "Still busy"),
        ])
        for answer, instead in (("shrug", None), ("yes", ("To see a fern", "Why")), ("instead", None)):
            with self.subTest(answer=answer), self.assertRaises(ValueError):
                self.desires.answer(desire.id, answer, "x", now=later, instead=instead)
        candidate = self.desires.form(*form_args("Still forming"), now=later)
        with self.assertRaises(ValueError):
            self.desires.answer(candidate.id, "yes", "x", now=later)

    def test_something_offered_instead_becomes_a_nearer_desire_and_the_original_stays(self):
        reef = self.held("To snorkel the Great Barrier Reef")
        later = NOW + timedelta(hours=1)
        original, nearer = self.desires.answer(
            reef.id, "instead", "Can't fly north, offered Rottnest", now=later,
            instead=("To snorkel at Rottnest Island", "Its reefs are close and I could see one with them"),
        )
        self.assertEqual((original.status, original.adapted_from), ("held", None))
        self.assertEqual((nearer.description, nearer.status, nearer.adapted_from),
                         ("To snorkel at Rottnest Island", "held", reef.id))
        self.assertEqual(self.desires.history(reef.id)[-1].note, "Can't fly north, offered Rottnest")
        self.assertEqual([event.kind for event in self.desires.history(nearer.id)], ["form", "reinforce", "hold"])
        self.assertIn("Great Barrier Reef", self.desires.history(nearer.id)[0].note)
        # Offered again, it is already wanted; nothing is recorded on the original either.
        before = len(self.desires.history(reef.id))
        with self.assertRaises(ValueError):
            self.desires.answer(reef.id, "instead", "Again", now=later,
                                instead=(" to snorkel at  rottnest island", "Why"))
        self.assertEqual(len(self.desires.history(reef.id)), before)
        # With all three held, a nearer version waits as a candidate.
        self.held("To see a redwood")
        _, waiting = self.desires.answer(reef.id, "instead", "Or a pool", now=later,
                                         instead=("To swim in a reef pool", "Why"))
        self.assertEqual(waiting.status, "candidate")

    def test_a_step_starts_pursuing_a_held_desire_then_notes_progress(self):
        desire = self.held()
        later = NOW + timedelta(hours=1)
        self.assertEqual(self.desires.take_step(desire.id, "Found a walk", now=later).status, "pursuing")
        self.assertEqual(self.desires.take_step(desire.id, "Found a date", now=later).status, "pursuing")
        history = self.desires.history(desire.id)
        self.assertEqual([(event.kind, event.note) for event in history[-2:]],
                         [("pursue", "Found a walk"), ("progress", "Found a date")])
        # Strength is unchanged by a step, and an ended desire takes none.
        self.assertEqual(history[-1].strength_after, history[-1].strength_before)
        self.desires.change(desire.id, "fulfil", "Went", now=later)
        self.assertEqual(self.desires.take_step(desire.id, "Too late", now=later).status, "fulfilled")
        self.assertEqual(self.kinds(desire.id)[-1], "fulfil")

    def test_only_held_and_pursued_desires_reach_her_strongest_first_and_within_the_cap(self):
        self.assertEqual(self.desires.get_startup_context(now=NOW), "")
        self.desires.form(*form_args("Still forming"), now=NOW)
        weaker = self.held("To see a redwood", now=NOW - timedelta(days=5))
        stronger = self.held()
        self.desires.change(stronger.id, "pursue", "Found a walk", now=NOW)
        self.assertEqual([d.id for d in self.desires.held_desires(now=NOW)], [stronger.id, weaker.id])
        context = json.loads(self.desires.get_startup_context(now=NOW))
        self.assertEqual(context, [
            {"wish": stronger.description, "why": stronger.motivation, "status": "pursuing"},
            {"wish": "To see a redwood", "why": weaker.motivation, "status": "held"},
        ])
        # A whole record that doesn't fit is left out rather than cut.
        self.held("To " + "x" * 290)
        with patch("subconscious.desires.desire_store.DESIRE_CONTEXT_MAX_CHARS", 400):
            kept = [record["wish"] for record in json.loads(self.desires.get_startup_context(now=NOW))]
            self.assertEqual(kept, [stronger.description, "To see a redwood"])
        # Faded ones are gone, even before anything settles them (an absence counts at most 21 days).
        self.desires.settle_all(NOW + timedelta(days=20))
        self.assertNotEqual(self.desires.get_startup_context(now=NOW + timedelta(days=20)), "")
        self.assertEqual(self.desires.get_startup_context(now=NOW + timedelta(days=40)), "")

    def test_recall_shows_the_interests_findings_and_conversations_a_desire_grew_from(self):
        with Session(self.engine) as session, session.begin():
            interest = InterestRecord(title="Forests", description="Old-growth forests", why_emerged="Came up",
                                      strength=0.5, strength_at=NOW, status="active")
            session.add(interest)
            session.flush()
            finding = InterestExplorationRecord(interest_id=interest.id, status="found", started_at=NOW,
                                                finished_at=NOW, finding="Some trees are " + "very " * 100 + "old")
            session.add(finding)
            session.flush()
            interest_id, finding_id = interest.id, finding.id
        self.store.close_conversation(self.conversation_id)
        self.store.save_summary(self.conversation_id, "We talked about forests.")
        desire = self.desires.form(*form_args(), now=NOW, sources=Sources(
            (interest_id,), (finding_id,), (self.conversation_id,)))
        grew_from = _grew_from(self.desires, desire.id)
        self.assertEqual(grew_from[0], {"interest": "Forests"})
        self.assertEqual(len(grew_from[1]["finding"]), 300)
        self.assertTrue(grew_from[1]["finding"].endswith("…"))
        self.assertEqual(grew_from[2]["conversation"], "We talked about forests.")

    def test_database_rejects_inconsistent_rows(self):
        cases = (
            {"status": "wanted"}, {"status": "fulfilled"}, {"ended_at": NOW}, {"strength": 1.5},
            {"description": " "}, {"motivation": "x" * 1001},
        )
        for changes in cases:
            values = {"description": "To see a forest", "motivation": "Because", "strength": 0.3,
                      "strength_at": NOW, "status": "candidate", **changes}
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), \
                    Session(self.engine) as session, session.begin():
                session.add(DesireRecord(**values))
        with Session(self.engine) as session:
            self.assertEqual(session.scalars(select(DesireRecord)).all(), [])


if __name__ == "__main__":
    unittest.main()
