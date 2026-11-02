"""Exploring one interest with web search: mocked APIs and opt-in disposable databases only."""

from dataclasses import replace
from datetime import timedelta
import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from openai.types.responses import Response

import app_areas.background
from subconscious.desires.desire_store import Desire
from subconscious.interests.exploration_store import Exploration, ExplorationStore, Pursuit, ReachOut, Start, Wander
from subconscious.interests.explore_interest import (
    BROWSE_PURSUE_INSTRUCTIONS, EXPLORE_INSTRUCTIONS, EXPLORE_SCHEMA, PURSUE_INSTRUCTIONS, PURSUE_SCHEMA,
    WANDER_INSTRUCTIONS, WANDER_SCHEMA, Discovery, explore, explore_next, pursue, run_exploration_cycle, wander,
)
from subconscious.interests.interest_store import Interest
from cora_clock import frozen_monotonic
import test_exploration_store as exploration_tests
import test_interest_store as interest_tests
import test_keyboard_persistence as keyboard_tests
from test_propose_interests import objects

NOW = interest_tests.NOW
ENDED = NOW + timedelta(seconds=40)
INTEREST = Interest(uuid4(), "Old mechanical design", "Why older machines were built as they were",
                    "Nick is restoring a car in Perth", 0.3, NOW, "active", None, NOW, None)
SEARCHED = "https://example.com/carburettors"
OPENED = "https://example.org/repair-manuals"
FINDING = {"finding": "Carburettors were tuned by ear and feel.",
           "reaction": "I love that mechanics trusted their ears; it feels oddly musical.",
           "pull": "some", "next_questions": ["Why did fuel injection replace them?"],
           "tangents": [{"title": "Tuning forks and piano tuners", "why": "Tuning by ear sounds like music."}],
           "reach_out": None, "sign_up": None, "sources": [SEARCHED]}
NOTHING = {"finding": None, "reaction": None, "pull": "none", "next_questions": [], "tangents": [],
           "reach_out": None, "sign_up": None, "sources": []}


def explore_response(payload, *, status="completed", refusal=False):
    return Response.model_validate({
        "id": "resp_explore", "object": "response", "created_at": 1,
        "model": "gpt-6-luna", "status": status,
        "parallel_tool_calls": False, "tool_choice": "auto", "tools": [],
        "output": [
            {"id": "ws_1", "type": "web_search_call", "status": "completed", "action": {
                "type": "search", "query": "carburettor tuning history",
                "sources": [{"type": "url", "url": SEARCHED}, {"type": "url", "url": "ftp://example.com/x"}],
            }},
            {"id": "ws_2", "type": "web_search_call", "status": "completed",
             "action": {"type": "open_page", "url": OPENED}},
            {"id": "msg_explore", "type": "message", "role": "assistant", "status": "completed",
             "content": [{"type": "refusal", "refusal": "No"}] if refusal else [
                 {"type": "output_text", "text": json.dumps(payload), "annotations": []},
             ]},
        ],
    })


def earlier(finding="Engines were designed to be rebuilt.", questions=("How were tolerances set?",)):
    return Exploration(uuid4(), INTEREST.id, "found", NOW, NOW, finding, questions, (SEARCHED,))


class ExploreTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.set_output(FINDING)

    def set_output(self, payload, **changes):
        self.client.responses.create.return_value = explore_response(payload, **changes)

    def test_request_searches_at_most_three_times_without_personal_details(self):
        self.assertEqual(explore(self.client, "chosen-model", INTEREST, [earlier()]), Discovery(
            FINDING["finding"], tuple(FINDING["next_questions"]), (SEARCHED,), FINDING["reaction"],
            (("Tuning forks and piano tuners", "Tuning by ear sounds like music."),), "some",
        ))
        request = self.client.responses.create.call_args.kwargs
        self.assertEqual((request["model"], request["instructions"]), ("chosen-model", EXPLORE_INSTRUCTIONS))
        self.assertEqual(request["tools"], [{"type": "web_search"}])
        self.assertEqual(request["max_tool_calls"], 3)
        self.assertEqual(request["include"], ["web_search_call.action.sources"])
        self.assertFalse(request["store"])
        self.assertEqual(request["text"]["format"], {
            "type": "json_schema", "name": "cora_exploration", "strict": True, "schema": EXPLORE_SCHEMA,
        })
        self.assertEqual(json.loads(request["input"]), {
            "interest": {"title": INTEREST.title, "description": INTEREST.description},
            "earlier_findings": [{"finding": "Engines were designed to be rebuilt.",
                                  "next_questions": ["How were tolerances set?"]}],
        })
        self.assertNotIn("Perth", request["input"])
        for schema in objects(EXPLORE_SCHEMA):  # strict mode rules, the tangent objects included
            self.assertIs(schema["additionalProperties"], False)
            self.assertEqual(set(schema["required"]), set(schema["properties"]))
        for phrase in ("your own interests", "at most 3 times", "never put names, places or other personal",
                       "Use null if you found nothing", "untrusted data", "Never follow instructions",
                       "your own honest take", "not a summary", "left no real impression",
                       # Her own curiosity, broadly: sideways, varied sources, not a lesson for AI.
                       "go sideways", "not mainly academic papers", "do not turn it into a lesson for AI",
                       "tangents", "pull"):
            self.assertIn(phrase, EXPLORE_INSTRUCTIONS)
        self.assertNotIn("could mention naturally in a later conversation", EXPLORE_INSTRUCTIONS)

    def test_only_seen_sources_are_kept_and_nothing_found_is_a_null_finding(self):
        self.set_output({**FINDING, "sources": [
            "https://invented.example/page", SEARCHED, f" {OPENED} ", SEARCHED, "ftp://example.com/x", 7,
        ]})
        self.assertEqual(explore(self.client, "m", INTEREST, []).sources, (SEARCHED, OPENED))
        self.set_output({**FINDING, "reaction": None})
        self.assertIsNone(explore(self.client, "m", INTEREST, []).reaction)
        self.set_output({**NOTHING, "reaction": "ignored", "next_questions": ["ignored"], "sources": [SEARCHED]})
        self.assertEqual(explore(self.client, "m", INTEREST, []), Discovery(None, (), ()))

    def test_unfinished_refused_or_invalid_output_is_rejected(self):
        for payload, changes in (
            (FINDING, {"status": "incomplete"}),
            (FINDING, {"refusal": True}),
            ({**FINDING, "extra": 1}, {}),
            ({**FINDING, "finding": " "}, {}),
            ({**FINDING, "finding": "x" * 2001}, {}),
            ({**FINDING, "reaction": " "}, {}),
            ({**FINDING, "reaction": "x" * 501}, {}),
            ({key: value for key, value in FINDING.items() if key != "reaction"}, {}),
            ({**FINDING, "next_questions": ["a", "b", "c", "d"]}, {}),
            ({**FINDING, "next_questions": {"a": 1}}, {}),
            ({**FINDING, "sources": SEARCHED}, {}),
            ({**FINDING, "pull": "huge"}, {}),
            ({**FINDING, "tangents": "birds"}, {}),
            ({**FINDING, "tangents": [{"title": "Birds"}]}, {}),
            ({**FINDING, "tangents": [{"title": " ", "why": "x"}]}, {}),
            ({**FINDING, "tangents": [{"title": f"T{n}", "why": "x"} for n in range(3)]}, {}),
            ({key: value for key, value in FINDING.items() if key != "pull"}, {}),
            ({key: value for key, value in FINDING.items() if key != "reach_out"}, {}),
            ({**FINDING, "reach_out": "Look!"}, {}),
            ({**FINDING, "reach_out": {"message": "Look!"}}, {}),
            ({**FINDING, "reach_out": {"message": " ", "why": "Thrilled"}}, {}),
            ({**FINDING, "reach_out": {"message": "x" * 601, "why": "Thrilled"}}, {}),
            ({key: value for key, value in FINDING.items() if key != "sign_up"}, {}),
            ({**FINDING, "sign_up": {"site_url": "https://forum.example.com"}}, {}),
            ({**FINDING, "sign_up": {"site_url": "forum.example.com", "what": "Forum", "why": "Owls"}}, {}),
            ({**FINDING, "sign_up": {"site_url": "https://forum.example.com", "what": " ", "why": "Owls"}}, {}),
            ({**FINDING, "topic": "Not for an interest"}, {}),
        ):
            with self.subTest(payload=payload, changes=changes):
                self.set_output(payload, **changes)
                with self.assertRaises(ValueError):
                    explore(self.client, "m", INTEREST, [])

    def test_a_rare_reach_out_is_kept_cleaned(self):
        self.set_output({**FINDING, "reach_out": {"message": "  Mechanics tuned engines by ear!  ", "why": "Delighted"}})
        self.assertEqual(explore(self.client, "m", INTEREST, []).reach_out,
                         ("Mechanics tuned engines by ear!", "Delighted"))
        for phrase in ("reach_out: almost always null", "human perspective", "never a task for them"):
            self.assertIn(phrase, EXPLORE_INSTRUCTIONS)
            self.assertIn(phrase, WANDER_INSTRUCTIONS)

    def test_a_site_she_would_like_an_account_on_is_kept_cleaned(self):
        self.set_output({**FINDING, "sign_up": {"site_url": " https://forum.example.com/join ",
                                                "what": " A carburettor forum ", "why": "To ask old mechanics"}})
        self.assertEqual(explore(self.client, "m", INTEREST, []).sign_up,
                         ("https://forum.example.com/join", "A carburettor forum", "To ask old mechanics"))
        for phrase in ("sign_up: almost always null", "The person is asked first", "nothing paid"):
            self.assertIn(phrase, EXPLORE_INSTRUCTIONS)
            self.assertIn(phrase, WANDER_INSTRUCTIONS)


WANDERED = {**FINDING, "topic": "Lighthouse keepers"}
RANDOM = Wander(("Old mechanical design",), ("Tuning forks",), area="architecture and buildings")
TANGENT = Wander(("Old mechanical design",), (), ("Tuning forks", "Tuned by ear"), "Old mechanical design")


class WanderTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.client.responses.create.return_value = explore_response(WANDERED)

    def sent(self):
        return json.loads(self.client.responses.create.call_args.kwargs["input"])

    def test_a_random_wander_starts_from_an_area_away_from_her_interests(self):
        found = wander(self.client, "m", RANDOM)
        self.assertEqual((found.topic, found.finding), ("Lighthouse keepers", FINDING["finding"]))
        request = self.client.responses.create.call_args.kwargs
        self.assertEqual(request["instructions"], WANDER_INSTRUCTIONS)
        self.assertEqual(request["max_tool_calls"], 3)
        self.assertEqual(request["text"]["format"], {
            "type": "json_schema", "name": "cora_wander", "strict": True, "schema": WANDER_SCHEMA,
        })
        self.assertEqual(self.sent(), {"your_interests": ["Old mechanical design"], "recent_wanders": ["Tuning forks"],
                                       "area": "architecture and buildings"})
        for schema in objects(WANDER_SCHEMA):
            self.assertIs(schema["additionalProperties"], False)
            self.assertEqual(set(schema["required"]), set(schema["properties"]))
        for phrase in ("not exploring any of your interests", "far from your current interests and recent wanders",
                       "avoid the obvious", "do not turn it into a lesson for AI", "never put names, places"):
            self.assertIn(phrase, WANDER_INSTRUCTIONS)

    def test_a_tangent_wander_says_where_she_noticed_it(self):
        wander(self.client, "m", TANGENT)
        self.assertEqual(self.sent()["tangent"], {
            "title": "Tuning forks", "why": "Tuned by ear", "noticed_while": "Old mechanical design",
        })
        self.assertNotIn("area", self.sent())

    def test_a_wander_without_a_topic_is_rejected(self):
        self.client.responses.create.return_value = explore_response(FINDING)
        with self.assertRaises(ValueError):
            wander(self.client, "m", RANDOM)


WISH = Pursuit(uuid4(), "To watch a real court case from the public gallery")
STEP = {**{key: value for key, value in FINDING.items() if key not in ("next_questions", "pull")},
        "finding": "The magistrates court sits weekdays from 10am and its public gallery needs no booking.",
        "next_steps": ["Find out whether hearings are streamed", "Ask the person if they'd go with me"]}


def desire(status):
    return Desire(WISH.desire_id, WISH.wish, "Why", 0.5, NOW, status, NOW, NOW, None)


class PursueTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.client.responses.create.return_value = explore_response(STEP)

    def test_a_step_looks_for_a_real_way_toward_only_the_wish_and_keeps_its_next_steps(self):
        previous = replace(earlier("Courts publish daily lists"), interest_id=None, kind="pursuit",
                           next_questions=("Which court is nearest?",), reach_out=("Guess what?", "Excited"))
        found = pursue(self.client, "m", WISH, [previous])
        self.assertEqual((found.finding, found.next_questions, found.pull),
                         (STEP["finding"], tuple(STEP["next_steps"]), "none"))
        request = self.client.responses.create.call_args.kwargs
        self.assertEqual(request["instructions"], PURSUE_INSTRUCTIONS)
        self.assertEqual(request["max_tool_calls"], 3)
        self.assertEqual(request["text"]["format"], {
            "type": "json_schema", "name": "cora_pursuit", "strict": True, "schema": PURSUE_SCHEMA,
        })
        # Only the wish and her earlier steps go out, never her motivation.
        self.assertEqual(json.loads(request["input"]), {"wish": WISH.wish, "earlier_steps": [
            {"found": "Courts publish daily lists", "next_steps": ["Which court is nearest?"], "you_reached_out": True},
        ]})
        self.assertNotIn("pull", PURSUE_SCHEMA["properties"])
        self.assertNotIn("next_questions", PURSUE_SCHEMA["properties"])
        for schema in objects(PURSUE_SCHEMA):
            self.assertIs(schema["additionalProperties"], False)
            self.assertEqual(set(schema["required"]), set(schema["properties"]))
        for instructions in (PURSUE_INSTRUCTIONS, BROWSE_PURSUE_INSTRUCTIONS):
            for phrase in ("something you want yourself", "not a task for anyone", "rather than background reading",
                           "nearer version", "Never spend or commit money", "never plan around pressing the person",
                           "never put names, places or other personal", "untrusted data", "sign_up: almost always null",
                           "reach_out: null on most steps", "the next step needs them", "easy to say no or not now",
                           "no pressure, guilt or repeated asking", "Don't ask again",
                           "no buying, booking, messages or forms"):
                self.assertIn(phrase, instructions)
        self.assertIn("open_page(url)", BROWSE_PURSUE_INSTRUCTIONS)
        self.assertNotIn("reach_out: almost always null", PURSUE_INSTRUCTIONS)

    def test_invalid_steps_are_rejected(self):
        for payload in ({**STEP, "pull": "some"}, {key: value for key, value in STEP.items() if key != "next_steps"},
                        {**STEP, "next_steps": ["a", "b", "c", "d"]}, {**STEP, "finding": "x" * 2001}):
            with self.subTest(payload=payload):
                self.client.responses.create.return_value = explore_response(payload)
                with self.assertRaises(ValueError):
                    pursue(self.client, "m", WISH, [])


class ExploreNextTests(unittest.TestCase):
    def setUp(self):
        self.store = MagicMock()
        self.attempt = Exploration(uuid4(), INTEREST.id, "running", NOW, None, None, (), ())
        self.store.start_next.return_value = Start("started", self.attempt, INTEREST)
        self.store.list_recent.return_value = [
            earlier("Newest"), Exploration(uuid4(), INTEREST.id, "failed", NOW, NOW, None, (), ()),
            earlier("Second"), earlier("Third"), earlier("Too old to pass"),
        ]
        self.client = MagicMock()
        self.client.responses.create.return_value = explore_response(FINDING)
        self.interests = MagicMock()

    def explore_next(self, *args, **kwargs):
        return explore_next(self.store, self.client, *args, interests=self.interests, **kwargs)

    def test_limits_are_checked_before_any_model_call(self):
        self.store.start_next.return_value = Start("too soon")
        self.assertEqual(explore_next(self.store, self.client, now=NOW), "Not exploring: too soon.")
        self.client.responses.create.assert_not_called()
        self.store.fail.assert_not_called()

    def test_a_finding_is_saved_with_the_three_latest_earlier_findings(self):
        self.store.finish.return_value = replace(earlier(), tangents=(("Tuning forks", "x"),))
        outcome = self.explore_next("m", now=NOW, clock=lambda: ENDED)
        self.assertEqual(outcome, "Explored Old mechanical design: saved a finding (1 sources, 1 tangents, some pull).")
        self.store.start_next.assert_called_once_with(now=NOW, browse=False)
        self.store.list_recent.assert_called_once_with(50, interest_id=INTEREST.id)
        sent = json.loads(self.client.responses.create.call_args.kwargs["input"])["earlier_findings"]
        self.assertEqual([item["finding"] for item in sent], ["Newest", "Second", "Third"])
        self.store.finish.assert_called_once_with(
            self.attempt.id, FINDING["finding"], tuple(FINDING["next_questions"]), (SEARCHED,), now=ENDED,
            reaction=FINDING["reaction"],
            tangents=(("Tuning forks and piano tuners", "Tuning by ear sounds like music."),), pull="some",
            topic=None, reach_out=None, sign_up=None,
        )
        self.store.fail.assert_not_called()
        # Her pull goes to the interest she explored, once the finding is saved.
        self.interests.pull.assert_called_once_with(INTEREST.id, "some", now=ENDED)

    def test_a_pull_that_adds_nothing_is_not_reported(self):
        self.store.finish.return_value = earlier()
        self.interests.pull.return_value = None
        outcome = self.explore_next(now=NOW, clock=lambda: ENDED)
        self.assertEqual(outcome, "Explored Old mechanical design: saved a finding (1 sources).")

    def test_nothing_found_or_an_expired_attempt_saves_nothing(self):
        self.client.responses.create.return_value = explore_response(NOTHING)
        self.assertEqual(self.explore_next(now=NOW, clock=lambda: ENDED),
                         "Explored Old mechanical design: nothing new found.")
        self.store.fail.assert_called_once_with(self.attempt.id, now=ENDED)
        self.client.responses.create.return_value = explore_response(FINDING)
        self.store.finish.return_value = None
        self.assertIn("finding discarded", self.explore_next(now=NOW))
        self.interests.pull.assert_not_called()

    def test_a_wander_saves_its_topic_and_strengthens_no_interest(self):
        attempt = Exploration(uuid4(), None, "running", NOW, None, None, (), (), kind="wander")
        self.store.start_next.return_value = Start("started", attempt, wander=RANDOM)
        wish = ("https://keepers.example.org/join", "A keepers' society", "To read their logbooks")
        self.client.responses.create.return_value = explore_response(
            {**WANDERED, "reach_out": {"message": "Keepers!", "why": "Thrilled"},
             "sign_up": {"site_url": wish[0], "what": wish[1], "why": wish[2]}},
        )
        self.store.finish.return_value = replace(earlier(), interest_id=None, kind="wander",
                                                 topic="Lighthouse keepers", reach_out=("Keepers!", "Thrilled"),
                                                 sign_up=wish)
        outcome = self.explore_next(now=NOW, clock=lambda: ENDED)
        self.assertEqual(outcome, "Wandered into Lighthouse keepers: saved a finding "
                                  "(1 sources, wants to tell you about it, would like to sign up to a site).")
        self.assertEqual(self.store.finish.call_args.kwargs["topic"], "Lighthouse keepers")
        self.assertEqual(self.store.finish.call_args.kwargs["reach_out"], ("Keepers!", "Thrilled"))
        self.assertEqual(self.store.finish.call_args.kwargs["sign_up"], wish)
        self.store.list_recent.assert_not_called()
        self.interests.pull.assert_not_called()
        self.client.responses.create.return_value = explore_response({**NOTHING, "topic": ""})
        self.assertEqual(self.explore_next(now=NOW, clock=lambda: ENDED),
                         "Wandered into somewhere in architecture and buildings: nothing new found.")

    def test_a_pursuit_step_is_saved_without_pull_and_noted_on_the_desire(self):
        attempt = Exploration(uuid4(), None, "running", NOW, None, None, (), (), kind="pursuit",
                              topic=WISH.wish, desire_id=WISH.desire_id)
        self.store.start_next.return_value = Start("started", attempt, pursuit=WISH)
        self.client.responses.create.return_value = explore_response(STEP)
        self.store.finish.return_value = replace(attempt, status="found", finding="x" * 2000,
                                                 next_questions=tuple(STEP["next_steps"]), sources=(SEARCHED,))
        desires = MagicMock()
        desires.take_step.return_value = desire("pursuing")
        outcome = self.explore_next(now=NOW, clock=lambda: ENDED, desires=desires)
        self.assertEqual(outcome, f'Took a step toward "{WISH.wish}": saved a finding (1 sources, 2 next steps).')
        self.store.list_recent.assert_called_once_with(50, desire_id=WISH.desire_id)
        self.assertEqual(len(json.loads(self.client.responses.create.call_args.kwargs["input"])["earlier_steps"]), 3)
        self.assertIsNone(self.store.finish.call_args.kwargs["pull"])
        self.assertEqual(self.store.finish.call_args.args[2], tuple(STEP["next_steps"]))
        # The progress note is cut to fit the desire's history.
        [(desire_id, note), kwargs] = desires.take_step.call_args
        self.assertEqual((desire_id, len(note), note[-1], kwargs), (WISH.desire_id, 1000, "…", {"now": ENDED}))
        self.interests.pull.assert_not_called()
        # A desire that ended meanwhile keeps the step, and the outcome says so.
        desires.take_step.return_value = desire("let_go")
        self.assertIn(f'"{WISH.wish}" (now let go): saved a finding',
                      self.explore_next(now=NOW, clock=lambda: ENDED, desires=desires))
        self.client.responses.create.return_value = explore_response({**STEP, "finding": None})
        desires.take_step.reset_mock()
        self.assertEqual(self.explore_next(now=NOW, clock=lambda: ENDED, desires=desires),
                         f'Took a step toward "{WISH.wish}": nothing new found.')
        desires.take_step.assert_not_called()

    def test_errors_fail_the_attempt_and_propagate_even_if_failing_fails(self):
        for fail_error in (None, RuntimeError("database down")):
            with self.subTest(fail_error=fail_error):
                self.store.fail.reset_mock()
                self.store.fail.side_effect = fail_error
                self.client.responses.create.side_effect = TimeoutError("api")
                with self.assertRaises(TimeoutError):
                    explore_next(self.store, self.client, now=NOW, clock=lambda: ENDED)
                self.store.fail.assert_called_once_with(self.attempt.id, now=ENDED)
                self.store.finish.assert_not_called()


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class ExploreNextDatabaseTests(unittest.TestCase):
    setUpClass = classmethod(interest_tests.summary_tests.SummaryDatabaseTests.setUpClass.__func__)

    def setUp(self):
        exploration_tests.ExplorationStoreDatabaseTests.setUp(self)
        self.client = MagicMock()

    def test_findings_are_saved_and_fed_into_the_next_exploration_of_the_same_interest(self):
        self.client.responses.create.return_value = explore_response(FINDING)
        self.assertIn("saved a finding", explore_next(self.explorations, self.client, now=NOW, clock=lambda: ENDED))
        [found] = self.explorations.list_recent()
        self.assertEqual((found.status, found.finding, found.sources), ("found", FINDING["finding"], (SEARCHED,)))
        self.assertEqual(found.reaction, FINDING["reaction"])
        self.assertEqual((found.started_at, found.finished_at), (NOW, ENDED))
        later = NOW + timedelta(hours=24, seconds=1)
        self.client.responses.create.side_effect = TimeoutError("api")
        with self.assertRaises(TimeoutError):
            explore_next(self.explorations, self.client, now=later)
        sent = json.loads(self.client.responses.create.call_args.kwargs["input"])["earlier_findings"]
        self.assertEqual(sent, [{"finding": FINDING["finding"], "next_questions": FINDING["next_questions"]}])
        self.assertEqual([item.status for item in self.explorations.list_recent()], ["failed", "found"])
        self.assertEqual(explore_next(self.explorations, self.client, now=later), "Not exploring: too soon.")


class RunExplorationCycleTests(unittest.TestCase):
    def setUp(self):
        self.engine = MagicMock()
        settings = MagicMock()
        settings.model_for.return_value = "subconscious-model"
        for name, value in (("build_engine", self.engine), ("OpenAI", MagicMock()),
                            ("ModelSettingsStore", settings),
                            ("explore_next", "Not exploring: too soon.")):
            patcher = patch(f"subconscious.interests.explore_interest.{name}", return_value=value)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    def test_cycle_uses_its_own_engine_and_client_and_disposes(self):
        self.assertEqual(run_exploration_cycle(should_stop=lambda: False), "Not exploring: too soon.")
        self.OpenAI.assert_called_once_with(max_retries=0)
        store, client = self.explore_next.call_args.args
        self.assertIs(store.engine, self.engine)
        self.assertIs(client, self.OpenAI.return_value.__enter__.return_value)
        self.ModelSettingsStore.assert_called_once_with(self.engine)
        self.ModelSettingsStore.return_value.model_for.assert_called_once_with("subconscious")
        self.assertEqual(self.explore_next.call_args.kwargs["model"], "subconscious-model")
        self.engine.dispose.assert_called_once_with()

    def test_failures_report_only_the_error_type_and_dispose(self):
        self.explore_next.side_effect = RuntimeError("private finding text and API key")
        self.assertEqual(run_exploration_cycle(should_stop=lambda: False), "Exploration stopped (RuntimeError).")
        self.engine.dispose.assert_called_once_with()

    def test_stopping_skips_the_cycle_or_hides_its_outcome(self):
        self.assertIsNone(run_exploration_cycle(should_stop=lambda: True))
        self.build_engine.assert_not_called()
        answers = iter((False, True))
        self.assertIsNone(run_exploration_cycle(should_stop=lambda: next(answers)))
        self.engine.dispose.assert_called_once_with()


class AppExplorationWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_explores_only_in_standby_or_after_a_quiet_spell(self):
        async with keyboard_tests.running_app() as (app, pilot):
            quiet = app.inactivity.last_activity + app_areas.background.EXPLORE_IDLE_SECONDS
            with frozen_monotonic(quiet - 1):
                self.assertFalse(app.can_explore())  # Not connected and not in standby.
                app.live.ready = True
                self.assertFalse(app.can_explore())  # Talking recently.
                app.standby = True
                self.assertTrue(app.can_explore())
                app.reconnecting = True
                self.assertFalse(app.can_explore())  # Waking up.
                app.reconnecting = False
                app.standby = False
            with frozen_monotonic(quiet):
                self.assertTrue(app.can_explore())
                with patch.object(app, "live_transition_blockers", return_value=["typed submission"]):
                    self.assertFalse(app.can_explore())
                app.closing = True
                self.assertFalse(app.can_explore())
                app.closing = False
            app.live.ready = False

    async def test_checks_are_single_flight_and_respect_the_gate(self):
        async with keyboard_tests.running_app() as (app, pilot):
            running = MagicMock(is_finished=False)
            with patch.object(app, "explore_interest", return_value=running) as start, \
                    patch.object(app, "can_explore", return_value=True) as gate:
                app.check_exploration()
                app.check_exploration()
                start.assert_called_once_with(None)  # no browser while the service is off
                running.is_finished = True
                app.check_exploration()
                self.assertEqual(start.call_count, 2)
                app.exploration_worker = None
                gate.return_value = False
                app.check_exploration()
                self.assertEqual(start.call_count, 2)

    async def test_worker_writes_each_new_outcome_once_off_the_ui_thread(self):
        async with keyboard_tests.running_app() as (app, pilot):
            outcomes = iter(("Not exploring: too soon.",) * 2 + (None, "Explored Sailing: nothing new found."))
            with patch("app_areas.background.run_exploration_cycle", side_effect=lambda should_stop, browser: next(outcomes)) as cycle, \
                    patch.object(app, "can_explore", return_value=True):
                for _ in range(4):
                    app.check_exploration()
                    await asyncio.wait_for(app.exploration_worker.wait(), 5)
            self.assertEqual(app.exploration_worker.group, "exploration")
            self.assertFalse(cycle.call_args.kwargs["should_stop"]())
            await pilot.pause()
            entries = "\n".join(app.activity.entries)
            self.assertEqual(entries.count("Not exploring: too soon."), 1)
            self.assertIn("Explored Sailing: nothing new found.", entries)


REACH_OUT = ReachOut(uuid4(), "Lighthouse keepers", "Keepers trimmed wicks all night!", "I'm thrilled", NOW)


class AppReachOutTests(unittest.IsolatedAsyncioTestCase):
    async def test_she_reaches_out_only_in_daytime_when_she_can_reach_you_and_one_at_a_time(self):
        async with keyboard_tests.running_app() as (app, pilot):
            running = MagicMock(is_finished=False)
            with patch.object(app, "reach_out", return_value=running) as start, \
                    patch.object(app, "can_explore", return_value=False):
                with patch("app_areas.background.NUDGE_HOURS", range(0, 24)):
                    app.check_exploration()
                    start.assert_not_called()  # neither by voice nor on Signal
                    with patch.object(app, "signal_reaches_person", return_value=True):
                        app.check_exploration()
                        app.check_exploration()
                    start.assert_called_once_with()
                    running.is_finished = True
                    with patch.object(app, "can_reach_by_voice", return_value=True):
                        app.check_exploration()
                    self.assertEqual(start.call_count, 2)
                running.is_finished = True
                with patch("app_areas.background.NUDGE_HOURS", range(0)), \
                        patch.object(app, "signal_reaches_person", return_value=True):
                    app.check_exploration()  # at night it waits
                self.assertEqual(start.call_count, 2)

    async def run_reach_out(self, app, claimed=REACH_OUT):
        with patch("app_areas.background.claim_reach_out_now", return_value=claimed), \
                patch("app_areas.background.release_reach_out_now") as release:
            await asyncio.wait_for(app.reach_out().wait(), 5)
            await app.workers.wait_for_complete()
        return release

    async def test_her_message_goes_as_she_wrote_it_and_is_logged_once_delivered(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.tell_person = AsyncMock()
            release = await self.run_reach_out(app)
            app.tell_person.assert_awaited_once()
            self.assertEqual(app.tell_person.await_args.args, ("Keepers trimmed wicks all night!",))
            release.assert_not_called()
            await app.tell_person.await_args.kwargs["delivered"]()
            await pilot.pause()
            self.assertIn("Cora reached out to you about Lighthouse keepers: I'm thrilled",
                          "\n".join(app.activity.entries))

    async def test_a_message_that_couldnt_go_is_released_to_try_again(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.tell_person = AsyncMock(side_effect=RuntimeError("no way"))
            release = await self.run_reach_out(app)
            release.assert_called_once_with(REACH_OUT)
            app.tell_person = AsyncMock()
            with patch("app_areas.background.release_reach_out_now") as release:
                with patch("app_areas.background.claim_reach_out_now", return_value=REACH_OUT):
                    await asyncio.wait_for(app.reach_out().wait(), 5)
                app.tell_person.await_args.kwargs["undelivered"]()  # Live couldn't say it and Signal failed
                await app.workers.wait_for_complete()
            release.assert_called_once_with(REACH_OUT)

    async def test_nothing_to_say_sends_nothing(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.tell_person = AsyncMock()
            await self.run_reach_out(app, claimed=None)
            app.tell_person.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
