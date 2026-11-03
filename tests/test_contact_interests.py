"""Contacts get only the interests their message touches on: no real API, plus opt-in database tests."""

from datetime import timedelta
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from session_config import instruction_sections
from storage.models import MEMORY_EMBEDDING_DIMENSIONS
from subconscious.interests import relevance
from subconscious.interests.propose_interests import INTEREST_INSTRUCTIONS
from subconscious.interests.interest_store import INTEREST_CONTEXT_MAX_CHARS, InterestStore
from test_relevance import embedding_response
import test_exploration_store as exploration_tests
import test_interest_store as interest_tests
import test_keyboard_persistence as keyboard_tests

NOW = interest_tests.NOW
axis, blend = exploration_tests.axis, exploration_tests.blend
ENGINES, MOSS, CHESS = uuid4(), uuid4(), uuid4()


def shared():
    return [
        {"id": ENGINES, "title": "Old engines", "description": "Why old machines were built to be repaired",
         "latest_finding": "Carburettors were tuned by ear.",
         "found_on": "2026-10-30", "text": "Old engines: Carburettors were tuned by ear."},
        {"id": MOSS, "title": "Moss gardens", "description": "Slow gardens", "text": "Moss gardens"},
        {"id": CHESS, "title": "Chess openings", "description": "Old lines", "text": "Chess openings"},
    ]


class SparkedTests(unittest.TestCase):
    def setUp(self):
        self.store = MagicMock()
        self.store.shareable.return_value = shared()
        self.client = MagicMock()

    def test_only_close_interests_go_and_only_changed_ones_are_re_embedded(self):
        # Engines' finding changed since it was embedded; moss's title didn't; chess has none yet.
        self.store.embeddings.return_value = {ENGINES: ("Old engines", axis(0)), MOSS: ("Moss gardens", blend(0.6))}
        self.client.embeddings.create.return_value = embedding_response(axis(0), blend(0.9), axis(3))
        sparked = relevance.sparked_interests(self.store, self.client, "My carburettor is gummed up", now=NOW)
        self.assertEqual(self.client.embeddings.create.call_args.kwargs["input"], [
            "My carburettor is gummed up", "Old engines: Carburettors were tuned by ear.", "Chess openings"])
        saved = self.store.save_embeddings.call_args.args[0]
        self.assertEqual({key: value[0] for key, value in saved.items()},
                         {ENGINES: "Old engines: Carburettors were tuned by ear.", CHESS: "Chess openings"})
        self.assertEqual(sparked, [
            {"title": "Old engines", "description": "Why old machines were built to be repaired",
             "latest_finding": "Carburettors were tuned by ear.", "found_on": "2026-10-30"},
            {"title": "Moss gardens", "description": "Slow gardens"},
        ])

    def test_at_most_two_and_nothing_when_unrelated_or_empty(self):
        self.store.embeddings.return_value = {item["id"]: (item["text"], axis(0)) for item in shared()}
        self.client.embeddings.create.return_value = embedding_response(axis(0))
        self.assertEqual(len(relevance.sparked_interests(self.store, self.client, "anything", now=NOW)), 2)
        self.store.save_embeddings.assert_not_called()
        self.client.embeddings.create.return_value = embedding_response(axis(7))
        self.assertEqual(relevance.sparked_interests(self.store, self.client, "the weather", now=NOW), [])
        self.client.reset_mock()
        self.assertEqual(relevance.sparked_interests(self.store, self.client, "  ", now=NOW), [])
        self.store.shareable.return_value = []
        self.assertEqual(relevance.sparked_interests(self.store, self.client, "engines", now=NOW), [])
        self.client.embeddings.create.assert_not_called()

    def test_the_app_wrapper_gives_json_or_nothing(self):
        engine, client = MagicMock(), MagicMock()
        client.__enter__.return_value = client
        with patch.object(relevance, "build_engine", return_value=engine), \
                patch.object(relevance, "OpenAI", return_value=client), \
                patch.object(relevance, "sparked_interests", return_value=[{"title": "Old engines"}]):
            self.assertEqual(relevance.sparked_interests_now("engines"), '[{"title": "Old engines"}]')
        with patch.object(relevance, "build_engine", return_value=engine), \
                patch.object(relevance, "OpenAI", return_value=client), \
                patch.object(relevance, "sparked_interests", return_value=[]):
            self.assertEqual(relevance.sparked_interests_now("weather"), "")
        big = [{"title": "x" * 200, "latest_finding": "y" * (INTEREST_CONTEXT_MAX_CHARS - 300)}] * 2
        with patch.object(relevance, "build_engine", return_value=engine), \
                patch.object(relevance, "OpenAI", return_value=client), \
                patch.object(relevance, "sparked_interests", return_value=list(big)):
            self.assertEqual(len(json.loads(relevance.sparked_interests_now("x"))), 1)
        self.assertEqual(engine.dispose.call_count, 3)


class SectionTests(unittest.TestCase):
    def test_a_contacts_chat_frames_them_as_touched_on_never_to_start_on(self):
        section = instruction_sections(sparked_interests='[{"title": "Old engines"}]')["interests"]
        for phrase in ("Their latest message touches on", "only if it genuinely connects",
                       "don't start on your interests unprompted", "untrusted information"):
            self.assertIn(phrase, section)
        self.assertTrue(section.endswith('[{"title": "Old engines"}]'))
        # Descriptions are written to leave the person out; how it came about stays with her.
        self.assertIn("written so anyone could read it: never the user", INTEREST_INSTRUCTIONS)
        with self.assertRaises(ValueError):
            instruction_sections(interest_context="[]", sparked_interests="[]")
        with self.assertRaises(ValueError):
            instruction_sections(sparked_interests="x" * (INTEREST_CONTEXT_MAX_CHARS + 1))


class ContactChatTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_signal_contact_gets_only_what_their_message_touched_on(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.live.interest_context = '[{"title": "Old engines", "description": "From Nick\'s bike project"}]'
            app.persistence.load_client_memory_context.return_value = ""
            app.persistence.load_client_people_context.return_value = ""
            client = SimpleNamespace(id=uuid4(), name="Sarah")
            batch = [SimpleNamespace(text="My bike's carburettor"), SimpleNamespace(text=""),
                     SimpleNamespace(text="is gummed up")]
            app.activity.write = MagicMock()
            with patch("app_areas.signal_chats.sparked_interests_now", return_value='[{"title": "Old engines"}]') as spark:
                sections = await app.client_sections(client, batch)
            spark.assert_called_once_with("My bike's carburettor\nis gummed up")
            self.assertIn('[{"title": "Old engines"}]', sections["interests"])
            self.assertNotIn("Nick's bike project", json.dumps(sections))
            app.activity.write.assert_called_with("Sarah's message touched on: Old engines.", "Interests")
            # Nothing touched on, or no check possible: no interests at all.
            with patch("app_areas.signal_chats.sparked_interests_now", return_value=""):
                self.assertNotIn("interests", await app.client_sections(client, batch))
            with patch("app_areas.signal_chats.sparked_interests_now", side_effect=RuntimeError("private")):
                self.assertNotIn("interests", await app.client_sections(client, batch))
            app.activity.write.assert_called_with("Interests for Sarah's chat weren't checked (RuntimeError).",
                                                  "Interests")

    async def test_an_email_party_gets_only_what_their_email_touched_on(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.live.interest_context = '[{"title": "Old engines", "description": "From Nick\'s bike project"}]'
            thread = SimpleNamespace(task_id=uuid4(), brief="Book the service")
            new = [SimpleNamespace(subject="Your bike", text="The carburettor needs work")]
            with patch("app_areas.signal_chats.sparked_interests_now", return_value='[{"title": "Old engines"}]') as spark:
                sections = await app.email_party_sections(thread, new, "Bike shop")
            spark.assert_called_once_with("Your bike\nThe carburettor needs work")
            self.assertIn("Their latest message touches on", sections["interests"])
            self.assertNotIn("Nick's bike project", json.dumps(sections))


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class ShareableDatabaseTests(unittest.TestCase):
    setUpClass = classmethod(exploration_tests.ExplorationStoreDatabaseTests.setUpClass.__func__)
    found = exploration_tests.ExplorationStoreDatabaseTests.found
    start = exploration_tests.ExplorationStoreDatabaseTests.start

    def setUp(self):
        exploration_tests.ExplorationStoreDatabaseTests.setUp(self)

    def test_never_how_it_came_about_and_embeddings_keep_their_text(self):
        self.interests.spawn(*interest_tests.spawn_args("Moss gardens"), now=NOW)
        self.found("Carburettors were tuned by ear")
        shared = {item["title"]: item for item in self.interests.shareable(now=NOW + timedelta(hours=1))}
        engines = shared[self.interest.title]
        self.assertEqual(set(engines), {"id", "title", "description", "latest_finding", "found_on", "text"})
        self.assertEqual(engines["text"], f"{self.interest.title}: Carburettors were tuned by ear")
        self.assertEqual(shared["Moss gardens"]["text"], "Moss gardens")
        self.assertNotIn("why_emerged", json.dumps({key: dict(value, id=None) for key, value in shared.items()}))
        store = InterestStore(self.engine)
        self.assertEqual(store.embeddings([engines["id"]]), {})
        store.save_embeddings({engines["id"]: (engines["text"], axis(0))})
        [(text, vector)] = store.embeddings([engines["id"], shared["Moss gardens"]["id"]]).values()
        self.assertEqual((text, len(vector), vector[0]), (engines["text"], MEMORY_EMBEDDING_DIMENSIONS, 1.0))


if __name__ == "__main__":
    unittest.main()
