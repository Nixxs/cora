"""Feelings from outside a conversation: a fulfilled wish, a finding that stirred her, and thinking back
on a recent conversation. No real API."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from subconscious.desires.reflect_desires import feel_fulfilled
from subconscious.emotions.feeling_rules import APPRAISAL_RECENT, appraisal_feeling
from subconscious.interests.exploration_store import Start
from subconscious.interests.explore_interest import (
    EXPLORE_INSTRUCTIONS, EXPLORE_SCHEMA, PURSUE_INSTRUCTIONS, PURSUE_SCHEMA, WANDER_SCHEMA, explore, explore_next,
)
from subconscious.relationship.appraisal import Appraisal
from subconscious.relationship.appraise_conversation import feel_after_appraisal
import test_explore_interest as explore_tests

NOW = datetime(2026, 11, 3, 10, 30, tzinfo=timezone.utc)


def appraisal(tone="warm", friction="none", repair="none", mistake=False):
    return Appraisal("everyday", tone, friction, mistake, repair, "They thanked me for the help.")


class AppraisalRuleTests(unittest.TestCase):
    def test_at_most_one_small_or_medium_feeling_from_what_was_found(self):
        self.assertEqual(appraisal_feeling("neutral", "at_cora", "none"), ("hurt", "medium"))
        self.assertEqual(appraisal_feeling("neutral", "at_cora", "cora"), ("hurt", "medium"))
        self.assertIsNone(appraisal_feeling("neutral", "at_cora", "person"))
        self.assertIsNone(appraisal_feeling("warm", "at_cora", "both"))
        self.assertEqual(appraisal_feeling("hostile", "none", "none"), ("hurt", "medium"))
        self.assertEqual(appraisal_feeling("curt", "at_situation", "none"), ("irritation", "small"))
        self.assertEqual(appraisal_feeling("warm", "none", "none"), ("contentment", "small"))
        self.assertIsNone(appraisal_feeling("warm", "at_situation", "none"))
        self.assertIsNone(appraisal_feeling("neutral", "none", "none"))


class AppraisalFeelingTests(unittest.TestCase):
    def setUp(self):
        self.conversations = MagicMock()
        self.conversations.get_conversation.return_value.ended_at = NOW - timedelta(minutes=20)

    def feel(self, value, now=NOW):
        with patch("subconscious.relationship.appraise_conversation.FeelingStore") as store:
            outcome = feel_after_appraisal("engine", self.conversations, uuid4(), value, now)
        return outcome, store.return_value.record

    def test_a_recent_conversation_leaves_a_feeling(self):
        outcome, record = self.feel(appraisal())
        record.assert_called_once_with("contentment", "small",
                                       "thinking back on our conversation: They thanked me for the help.", now=NOW)
        self.assertEqual(outcome, "Cora feels content (small), thinking back on your conversation.")

    def test_an_old_one_nothing_stirring_or_her_own_mistake_leave_none(self):
        self.assertEqual(self.feel(appraisal(), now=NOW + APPRAISAL_RECENT)[0], None)
        self.assertEqual(self.feel(appraisal("neutral", mistake=True))[0], None)
        self.conversations.get_conversation.return_value.ended_at = None
        outcome, record = self.feel(appraisal())
        self.assertIsNone(outcome)
        record.assert_not_called()

    def test_a_failure_is_reported_not_raised(self):
        self.conversations.get_conversation.side_effect = RuntimeError("private")
        self.assertEqual(self.feel(appraisal())[0], "A feeling from thinking back wasn't saved (RuntimeError).")


class FulfilledTests(unittest.TestCase):
    def test_a_fulfilled_wish_leaves_her_strongly_fulfilled(self):
        store = MagicMock()
        desires = [MagicMock(description="To walk through an old-growth forest"), MagicMock(description="To sing")]
        with patch("subconscious.desires.reflect_desires.FeelingStore") as feelings:
            feelings.return_value.record.side_effect = [None, RuntimeError("down")]
            self.assertEqual(feel_fulfilled(store, desires, NOW), "; felt fulfilled")
        feelings.return_value.record.assert_any_call(
            "fulfilment", "strong", "fulfilled a wish: To walk through an old-growth forest", now=NOW)
        self.assertEqual(feel_fulfilled(store, [], NOW), "")


class FindingFeelingTests(unittest.TestCase):
    def test_every_kind_of_exploration_asks_for_her_own_feeling(self):
        for schema in (EXPLORE_SCHEMA, WANDER_SCHEMA, PURSUE_SCHEMA):
            self.assertIn("feeling", schema["required"])
        for instructions in (EXPLORE_INSTRUCTIONS, PURSUE_INSTRUCTIONS):
            self.assertIn("null if it didn't, which is often. Never inflate it.", instructions)
        self.assertIn("Fulfilment fits a step that brings the wish really closer", PURSUE_INSTRUCTIONS)

    def test_a_bad_feeling_rejects_the_attempt(self):
        client = MagicMock()
        for feeling in ({"feeling": "guilt", "strength": "small"}, {"feeling": "hurt", "strength": "huge"},
                        {"feeling": "hurt"}):
            with self.subTest(feeling=feeling):
                client.responses.create.return_value = explore_tests.explore_response(
                    {**explore_tests.FINDING, "feeling": feeling})
                with self.assertRaises(ValueError):
                    explore(client, "m", explore_tests.INTEREST, [])

    def test_a_finding_that_stirred_her_is_felt_with_her_reaction(self):
        store, client = MagicMock(), MagicMock()
        attempt = replace(explore_tests.earlier(), status="running")
        store.start_next.return_value = Start("started", attempt, explore_tests.INTEREST)
        store.list_recent.return_value = []
        store.finish.return_value = replace(explore_tests.earlier(), reaction="It feels oddly musical.")
        client.responses.create.return_value = explore_tests.explore_response(
            {**explore_tests.FINDING, "feeling": {"feeling": "excitement", "strength": "medium"}})
        with patch("subconscious.interests.explore_interest.FeelingStore") as feelings:
            outcome = explore_next(store, client, now=NOW, clock=lambda: NOW, interests=MagicMock())
        feelings.return_value.record.assert_called_once_with(
            "excitement", "medium", "Old mechanical design: It feels oddly musical.", now=NOW)
        self.assertIn("felt excited (medium)", outcome)
        # None stirred: nothing recorded.
        client.responses.create.return_value = explore_tests.explore_response(explore_tests.FINDING)
        with patch("subconscious.interests.explore_interest.FeelingStore") as feelings:
            self.assertNotIn("felt", explore_next(store, client, now=NOW, clock=lambda: NOW, interests=MagicMock()))
        feelings.return_value.record.assert_not_called()


if __name__ == "__main__":
    unittest.main()
