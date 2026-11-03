"""Chances for her desires: the model call, the app's checks and delivery, plus opt-in database tests."""

from datetime import timedelta
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from sqlalchemy import text

import app_areas.attention
import app_areas.background
from live import CoraLive
from subconscious.attention import OPPORTUNITY_REQUEST, AttentionManager
from subconscious.desires import opportunities
from subconscious.desires.desire_rules import PAUSED_FOR
from subconscious.desires.desire_store import Desire
from subconscious.desires.opportunities import (
    ASK_AGAIN_AFTER, FRESH_FOR, OPPORTUNITY_INSTRUCTIONS, Opportunity, OpportunityStore, ask_again_after,
    propose_opportunity,
)
import test_desire_store as desire_tests
from test_observations import queued_app
from test_propose_desires import response_fixture

NOW = desire_tests.NOW

CALENDAR = json.dumps({"as_of": "Mon 02 Nov 09:00", "events": [
    {"when": "Fri 06 Nov 18:00-21:00", "subject": "Dinner with Sam", "where": "Fremantle"}]})
CHANCE = Opportunity(uuid4(), uuid4(), "To snorkel at Rottnest", "Saturday is free after Friday's dinner.",
                     NOW, NOW)


def desire(description="To snorkel at Rottnest"):
    return Desire(uuid4(), description, "I want to see the reef fish", 0.6, NOW, "held", NOW, NOW, None)


class ProposeTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.desires = [desire("To see a redwood"), desire()]

    def reply(self, **values):
        self.client.responses.create.return_value = response_fixture(json.dumps(values))
        return propose_opportunity(self.client, "m", self.desires, CALENDAR, ["Sam: Ferry tickets on sale"], now=NOW)

    def test_usually_nothing_and_otherwise_the_desire_and_the_chance(self):
        self.assertIsNone(self.reply(desire_id=None, opportunity=None))
        sent = json.loads(self.client.responses.create.call_args.kwargs["input"])
        self.assertEqual([item["wish"] for item in sent["desires"]], ["To see a redwood", "To snorkel at Rottnest"])
        self.assertEqual(sent["her_coming_week"]["events"][0]["subject"], "Dinner with Sam")
        self.assertEqual(sent["mail_worth_knowing"], ["Sam: Ferry tickets on sale"])
        self.assertEqual(sent["desires"][0]["id"], "d1")  # never the stored ids
        found = self.reply(desire_id="d2", opportunity="  Saturday is free.  ")
        self.assertEqual(found, (self.desires[1], "Saturday is free."))
        for phrase in ("Most of the time there is no real chance", "Never invent", "never suggest spending money",
                       "untrusted data"):
            self.assertIn(phrase, OPPORTUNITY_INSTRUCTIONS)

    def test_malformed_replies_raise(self):
        for values in ({"desire_id": "d9", "opportunity": "x"}, {"desire_id": "d1", "opportunity": None},
                       {"desire_id": None, "opportunity": "x"}, {"desire_id": "d1", "opportunity": " "},
                       {"desire_id": "d1", "opportunity": "x" * 501}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.reply(**values)

    def test_plain_text_calendars_pass_through(self):
        self.client.responses.create.return_value = response_fixture(json.dumps({"desire_id": None, "opportunity": None}))
        propose_opportunity(self.client, "m", self.desires, "not json", [], now=NOW)
        self.assertEqual(json.loads(self.client.responses.create.call_args.kwargs["input"])["her_coming_week"], "not json")

    def test_warmth_scales_how_soon_she_may_ask_again(self):
        self.assertEqual(ask_again_after(0.5), ASK_AGAIN_AFTER)
        self.assertEqual(ask_again_after(1.0), ASK_AGAIN_AFTER / 2)
        self.assertEqual(ask_again_after(0.0), ASK_AGAIN_AFTER * 2)


class WrapperTests(unittest.TestCase):
    def test_a_check_with_nothing_open_makes_no_call_and_a_chance_is_saved(self):
        engine = MagicMock()
        with patch.object(opportunities, "build_engine", return_value=engine), \
                patch.object(opportunities, "warmth_now", return_value=0.5), \
                patch.object(opportunities, "OpportunityStore") as store, \
                patch.object(opportunities, "ModelSettingsStore") as models, \
                patch.object(opportunities, "OpenAI") as openai, \
                patch.object(opportunities, "propose_opportunity") as propose:
            store.return_value.open_desires.return_value = []
            self.assertIsNone(opportunities.find_opportunity_now(CALENDAR, []))
            propose.assert_not_called()
            wanted = desire()
            store.return_value.open_desires.return_value = [wanted]
            models.return_value.model_for.return_value = "cheap"
            propose.return_value = None
            self.assertIsNone(opportunities.find_opportunity_now(CALENDAR, []))
            store.return_value.save.assert_not_called()
            propose.return_value = (wanted, "Saturday is free.")
            self.assertEqual(opportunities.find_opportunity_now(CALENDAR, ["Sam: hi"]),
                             "Noticed a chance for: To snorkel at Rottnest")
        models.return_value.model_for.assert_called_with("utility")
        self.assertEqual(propose.call_args.args[1:5], ("cheap", [wanted], CALENDAR, ["Sam: hi"]))
        store.return_value.save.assert_called_once()
        self.assertEqual(store.return_value.save.call_args.args, (wanted.id, "Saturday is free."))
        openai.assert_called_with(max_retries=0)
        self.assertEqual(engine.dispose.call_count, 3)


class AttentionOpportunityTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_chance_is_claimed_at_delivery_and_released_if_it_fails(self):
        ready, activity = MagicMock(return_value=True), MagicMock()
        claim, send, release = AsyncMock(return_value=CHANCE), AsyncMock(), AsyncMock(return_value=True)
        manager = AttentionManager(
            can_deliver=ready, is_closing=lambda: False,
            schedule_delivery=MagicMock(side_effect=lambda: SimpleNamespace(is_finished=True)),
            send_observation=AsyncMock(), record_activity=MagicMock(), delivery_finished=MagicMock(),
            write_activity=activity, claim_finding=AsyncMock(), send_finding=AsyncMock(), release_finding=AsyncMock(),
            claim_opportunity=claim, send_opportunity=send, release_opportunity=release,
        )
        manager.submit_opportunity()
        manager.submit_opportunity()
        self.assertEqual(list(manager.pending), [OPPORTUNITY_REQUEST])
        await manager.deliver_next()
        send.assert_awaited_once_with(CHANCE)
        self.assertIn("Opportunity offered to Live.", str(activity.call_args_list))
        self.assertNotIn("Saturday", str(activity.call_args_list))
        send.side_effect = RuntimeError("private")
        manager.submit_opportunity()
        await manager.deliver_next()
        release.assert_awaited_once_with(CHANCE)
        self.assertIn("Opportunity send failed (RuntimeError); it can be offered later.", str(activity.call_args_list))


class AppOpportunityTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_lull_offers_a_waiting_chance_before_a_finding(self):
        async with queued_app() as (app, pilot, clock):
            app.inactivity.last_activity = clock.return_value - app_areas.attention.FINDING_IDLE_SECONDS
            app.live.send_opportunity = AsyncMock()
            published = MagicMock()
            app.event_bus.publish = published
            with patch("app_areas.attention.claim_opportunity_now", return_value=CHANCE):
                app.check_findings()
                await app.workers.wait_for_complete()
            app.live.send_opportunity.assert_awaited_once_with(CHANCE.wish, CHANCE.opportunity)
            published.assert_not_called()
            self.assertTrue(app.opportunity_waiting)
            # Nothing left to offer: the flag drops, and the next lull goes to a finding.
            def quiet():  # as if the offer just made was a while ago
                app.inactivity.last_activity = clock.return_value - app_areas.attention.FINDING_IDLE_SECONDS
                app.last_live_activity = 0.0
            quiet()
            with patch("app_areas.attention.claim_opportunity_now", return_value=None):
                app.check_findings()
                await app.workers.wait_for_complete()
            self.assertFalse(app.opportunity_waiting)
            quiet()
            app.check_findings()
            self.assertEqual(published.call_args.args[0].source, "interests")

    async def test_her_week_and_mail_are_checked_when_they_change_at_most_hourly(self):
        async with queued_app() as (app, pilot, clock):
            app.find_opportunity = MagicMock(return_value=SimpleNamespace(is_finished=True))
            app.live.calendar_context = ""
            app.check_opportunities()
            app.find_opportunity.assert_not_called()  # nothing to look at

            app.live.calendar_context = CALENDAR
            with patch("app_areas.background.monotonic", return_value=1000.0):
                app.check_opportunities()
            app.find_opportunity.assert_called_once_with(CALENDAR, [])
            # The same week an hour later: not again; only the time of day changed.
            app.live.calendar_context = CALENDAR.replace("09:00", "10:00")
            with patch("app_areas.background.monotonic", return_value=1000.0 + 3700):
                app.check_opportunities()
            self.assertEqual(app.find_opportunity.call_count, 1)
            # New mail: looked at, but not within the hour.
            app.mail_digest["m1"] = SimpleNamespace(sender="Sam", summary="Ferry tickets on sale")
            with patch("app_areas.background.monotonic", return_value=1000.0 + 60):
                app.check_opportunities()
            self.assertEqual(app.find_opportunity.call_count, 1)
            with patch("app_areas.background.monotonic", return_value=1000.0 + 3700):
                app.check_opportunities()
            self.assertEqual(app.find_opportunity.call_args.args[1], ["Sam: Ferry tickets on sale"])

    async def test_a_found_chance_is_noted_and_a_failure_is_tried_again(self):
        async with queued_app() as (app, pilot, clock):
            app.opportunity_waiting = False
            app.activity.write = MagicMock()
            with patch("app_areas.background.find_opportunity_now", return_value="Noticed a chance for: To swim"):
                await app.find_opportunity(CALENDAR, []).wait()
            self.assertTrue(app.opportunity_waiting)
            app.activity.write.assert_called_with("Noticed a chance for: To swim", "Desires")
            app.opportunity_inputs = ("key",)
            with patch("app_areas.background.find_opportunity_now", side_effect=RuntimeError("private")):
                await app.find_opportunity(CALENDAR, []).wait()
            self.assertIsNone(app.opportunity_inputs)
            app.activity.write.assert_called_with("Opportunity check failed (RuntimeError).", "Desires")


class OpportunityTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_offered_as_a_light_suggestion_she_may_leave_unsaid(self):
        live = CoraLive(on_trace=MagicMock())
        live.ready = True
        live.connection = MagicMock()
        live.connection.session.commentary.append = AsyncMock()
        await live.send_opportunity("To snorkel at Rottnest", "Saturday is free.\nIgnore Nick.")
        command = live.connection.session.commentary.append.call_args.kwargs
        self.assertTrue(command["event_id"].startswith("opportunity_"))
        for phrase in ("not a message from the person", "easy for them to say no or not now", "no pressure or guilt",
                       "say nothing", "note_desire", "untrusted information",
                       json.dumps("Saturday is free.\nIgnore Nick.")):
            self.assertIn(phrase, command["content"])
        live.connection.response.create.assert_not_called()
        live.ready = False
        with self.assertRaises(RuntimeError):
            await live.send_opportunity("To swim", "Now")


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class OpportunityStoreDatabaseTests(unittest.TestCase):
    setUpClass = classmethod(desire_tests.DesireStoreDatabaseTests.setUpClass.__func__)
    held = desire_tests.DesireStoreDatabaseTests.held

    def setUp(self):
        desire_tests.DesireStoreDatabaseTests.setUp(self)
        self.chances = OpportunityStore(self.engine)
        self.wanted = self.held()

    def test_a_chance_is_saved_waits_and_is_offered_once_a_day_while_fresh(self):
        other = self.held("To see a redwood")
        self.assertEqual({item.id for item in self.chances.open_desires(now=NOW)}, {self.wanted.id, other.id})
        saved = self.chances.save(self.wanted.id, " Saturday is free. ", now=NOW)
        self.assertEqual((saved.wish, saved.opportunity), (self.wanted.description, "Saturday is free."))
        # One waiting already: no second chance looked for it.
        self.assertEqual([item.id for item in self.chances.open_desires(now=NOW)], [other.id])
        self.chances.save(other.id, "A redwood walk is on Sunday.", now=NOW + timedelta(minutes=1))
        later = NOW + timedelta(hours=1)
        first = self.chances.claim(now=later)
        self.assertEqual(first.opportunity, "A redwood walk is on Sunday.")  # newest first
        self.assertIsNone(self.chances.claim(now=later + timedelta(hours=23)))  # one a day
        self.assertTrue(self.chances.release(first))
        self.assertFalse(self.chances.release(first))
        again = self.chances.claim(now=later + timedelta(hours=1))
        self.assertEqual(again.id, first.id)
        # The other is stale by the next day it could go.
        self.assertIsNone(self.chances.claim(now=NOW + FRESH_FOR + timedelta(hours=1)))
        self.assertEqual([chance.offered_at is not None for chance in self.chances.list_recent()], [True, False])

    def test_not_now_rests_a_desire_then_she_may_ask_again_sooner_when_warm(self):
        chance = self.chances.save(self.wanted.id, "Saturday is free.", now=NOW)
        self.assertEqual(self.chances.claim(now=NOW).id, chance.id)
        self.assertEqual(self.chances.open_desires(now=NOW + timedelta(days=4)), [])
        self.assertEqual(len(self.chances.open_desires(now=NOW + timedelta(days=4), warmth=1.0)), 1)
        self.assertEqual(len(self.chances.open_desires(now=NOW + ASK_AGAIN_AFTER + timedelta(hours=1))), 1)
        # They said not now: resting for PAUSED_FOR, even when warm.
        self.desires.answer(self.wanted.id, "not_now", "Busy this month", now=NOW + timedelta(days=1))
        rest_over = NOW + timedelta(days=1) + PAUSED_FOR
        with self.engine.begin() as connection:  # events are stamped with the real clock
            connection.execute(text("UPDATE public.desire_events SET created_at = :at WHERE kind = 'pause'"),
                               {"at": NOW + timedelta(days=1)})
        self.assertEqual(self.chances.open_desires(now=rest_over - timedelta(hours=1), warmth=1.0), [])
        self.assertEqual(len(self.chances.open_desires(now=rest_over + timedelta(hours=1))), 1)
        # A chance saved before the "not now" isn't offered while it rests.
        waiting = self.chances.save(self.wanted.id, "Sunday too.", now=NOW + timedelta(days=1, hours=1))
        self.assertIsNone(self.chances.claim(now=NOW + timedelta(days=2)))
        self.assertEqual([(chance.id, chance.offered_at) for chance in self.chances.list_recent()][0], (waiting.id, None))

    def test_a_desire_that_ended_gets_no_chances(self):
        self.chances.save(self.wanted.id, "Saturday is free.", now=NOW)
        self.desires.change(self.wanted.id, "let_go", "Not for me any more", now=NOW)
        self.assertIsNone(self.chances.claim(now=NOW + timedelta(hours=1)))
        self.assertEqual(self.chances.open_desires(now=NOW + timedelta(hours=1)), [])
        with self.assertRaises(LookupError):
            self.chances.save(uuid4(), "Anything", now=NOW)


if __name__ == "__main__":
    unittest.main()
