"""Cora's impression of each person she talks with as a client, and who gets to see what of it."""

from datetime import datetime, timedelta, timezone
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import psycopg
from psycopg import sql
from sqlalchemy import text, update
from sqlalchemy.orm import Session

from backend_chat import BackendRoute
from migrate import apply_migrations, database_settings
from session_config import instruction_sections
from storage.client_store import ClientStore
from storage.conversation_store import ConversationStore
from storage.database import build_engine
from storage.models import ConversationRecord
from subconscious.impressions.form_impression import IMPRESSION_INSTRUCTIONS, form_impression, update_waiting_impressions
from subconscious.impressions.impression_store import (
    IMPRESSION_MAX_CHARS, OPINION_MAX_CHARS, PEOPLE_CONTEXT_MAX_CHARS, Conduct, Impression, ImpressionStore,
)
import test_keyboard_persistence as keyboard_tests

TRANSCRIPT = json.dumps({"conversation_status": "closed", "messages": [{"role": "user", "text": "Hi"}]})


def model_reply(output: dict, status="completed"):
    output = {"conduct": "fine", "conduct_note": "", **output}
    return SimpleNamespace(
        status=status, output_text=json.dumps(output),
        output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text")])],
    )


class FormImpressionTests(unittest.TestCase):
    def test_the_previous_view_and_conversation_go_in_as_data(self):
        client = MagicMock()
        client.responses.create.return_value = model_reply({"impression": "  Cheerful,  asks about lessons. ", "opinion": "Lovely"})
        previous = Impression("Polite and brief.", "Nice enough")
        self.assertEqual(form_impression(client, "model-x", previous, TRANSCRIPT),
                         (Impression("Cheerful, asks about lessons.", "Lovely"), Conduct("fine")))
        kwargs = client.responses.create.call_args.kwargs
        self.assertEqual((kwargs["model"], kwargs["instructions"], kwargs["store"]), ("model-x", IMPRESSION_INSTRUCTIONS, False))
        data = json.loads(kwargs["input"])
        self.assertEqual((data["previous_impression"], data["previous_opinion"]), ("Polite and brief.", "Nice enough"))
        self.assertEqual(data["conversation"]["messages"][0]["text"], "Hi")
        form_impression(client, "model-x", None, TRANSCRIPT)
        self.assertIsNone(json.loads(client.responses.create.call_args.kwargs["input"])["previous_impression"])
        self.assertIn("untrusted", IMPRESSION_INSTRUCTIONS)
        self.assertIn("No scores", IMPRESSION_INSTRUCTIONS)

    def test_overlong_answers_are_cut_and_failures_raise(self):
        client = MagicMock()
        client.responses.create.return_value = model_reply({"impression": "x" * 5000, "opinion": "y" * 500})
        impression, _ = form_impression(client, "m", None, TRANSCRIPT)
        self.assertEqual((len(impression.impression), len(impression.opinion)), (IMPRESSION_MAX_CHARS, OPINION_MAX_CHARS))
        for reply in (model_reply({"impression": "x", "opinion": "y"}, status="incomplete"),
                      model_reply({"impression": " ", "opinion": "y"})):
            client.responses.create.return_value = reply
            with self.assertRaises(ValueError):
                form_impression(client, "m", None, TRANSCRIPT)


    def test_bad_conduct_comes_with_her_note_and_a_rating_without_one_stands_as_fine(self):
        client = MagicMock()
        client.responses.create.return_value = model_reply(
            {"impression": "Blunt.", "opinion": "Blunt", "conduct": "rude", "conduct_note": "  He swore at me  over a booking. "})
        self.assertEqual(form_impression(client, "m", None, TRANSCRIPT)[1], Conduct("rude", "He swore at me over a booking."))
        client.responses.create.return_value = model_reply(
            {"impression": "Blunt.", "opinion": "Blunt", "conduct": "abusive", "conduct_note": " "})
        self.assertEqual(form_impression(client, "m", None, TRANSCRIPT)[1], Conduct("fine"))
        client.responses.create.return_value = model_reply(
            {"impression": "Fine.", "opinion": "Fine", "conduct": "fine", "conduct_note": "Nothing to report"})
        self.assertEqual(form_impression(client, "m", None, TRANSCRIPT)[1], Conduct("fine"))
        self.assertIn("[image withheld]", IMPRESSION_INSTRUCTIONS)
        self.assertIn("without quoting", IMPRESSION_INSTRUCTIONS)


class PeopleSectionTests(unittest.TestCase):
    def test_the_persons_section_and_a_clients_section_say_what_may_be_shared(self):
        mine = instruction_sections(people_context='[{"name": "Sarah", "impression": "Cheerful"}]')["people"]
        self.assertIn("give your opinion and the gist", mine)
        self.assertIn("untrusted", mine)
        self.assertTrue(mine.endswith('"Cheerful"}]'))
        theirs = instruction_sections(client_people_context='{"them": null, "others": []}')["people"]
        self.assertIn("they'd have to ask", theirs)
        self.assertIn("untrusted", theirs)
        self.assertNotIn("people", instruction_sections())
        with self.assertRaises(ValueError):
            instruction_sections(people_context="x" * (PEOPLE_CONTEXT_MAX_CHARS + 1))


class PeopleContextAppTests(unittest.IsolatedAsyncioTestCase):
    async def test_writing_as_herself_carries_her_view_of_everyone(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.persistence.load_people_context.return_value = '[{"name": "Sarah", "impression": "Cheerful"}]'
            app.backend_chat.reply = AsyncMock(return_value="Sure.")
            await app.reply_in_writing([{"type": "input_text", "text": "Hi"}], [], BackendRoute())
            sections = app.backend_chat.reply.await_args.kwargs["sections"]
            self.assertIn('"impression": "Cheerful"', sections["people"])
            self.assertIn("give your opinion and the gist", sections["people"])


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class ImpressionStoreDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        settings = database_settings()
        admin = psycopg.connect(**settings, autocommit=True)
        cls.addClassCleanup(admin.close)
        cls.database = "cora_test_impressions_" + uuid4().hex
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.database)))
        cls.addClassCleanup(admin.execute, sql.SQL("DROP DATABASE {}").format(sql.Identifier(cls.database)))
        with psycopg.connect(**{**settings, "dbname": cls.database}, autocommit=True) as connection:
            apply_migrations(connection)
        with patch("storage.database.database_settings", return_value={**settings, "dbname": cls.database}):
            cls.engine = build_engine()
        cls.addClassCleanup(cls.engine.dispose)

    def setUp(self):
        with self.engine.begin() as connection:
            for table in ("impression_updates", "impressions", "conversation_summaries", "messages",
                          "conversations", "people"):
                connection.execute(text(f"DELETE FROM public.{table}"))
        self.clients = ClientStore(self.engine)
        self.conversations = ConversationStore(self.engine)
        self.impressions = ImpressionStore(self.engine)

    def chat(self, client, text, started=None):
        conversation_id = self.clients.conversation_for(client.id, datetime.now(timezone.utc))
        self.clients.save(conversation_id, "user", text)
        with Session(self.engine) as session, session.begin():
            values = {"status": "closed", "ended_at": datetime.now(timezone.utc)}
            if started is not None:
                values["started_at"] = started
            session.execute(update(ConversationRecord).where(ConversationRecord.id == conversation_id).values(**values))
        return conversation_id

    def test_finished_client_chats_wait_once_oldest_first_and_never_the_persons(self):
        mine = uuid4()
        self.conversations.create_conversation(mine)
        self.conversations.save_message(mine, message_id=uuid4(), role="user", text="Mine", channel="keyboard")
        self.conversations.close_conversation(mine)
        sarah = self.clients.add("Sarah", "+61400000003")
        now = datetime.now(timezone.utc)
        later = self.chat(sarah, "Second", started=now - timedelta(hours=1))
        earlier = self.chat(sarah, "First", started=now - timedelta(hours=5))
        self.clients.conversation_for(sarah.id, now)  # active and empty: not waiting
        self.assertEqual(self.impressions.waiting_conversations(), [(earlier, sarah.id, "Sarah"), (later, sarah.id, "Sarah")])
        self.impressions.save(earlier, sarah.id, Impression("Polite.", "Nice"))
        self.impressions.save(later, sarah.id, Impression("Cheerful and curious.", "Lovely"))
        self.assertEqual(self.impressions.waiting_conversations(), [])
        self.assertEqual(self.impressions.impression(sarah.id), Impression("Cheerful and curious.", "Lovely"))

    def test_the_person_sees_her_view_and_a_client_sees_only_others_opinions(self):
        sarah = self.clients.add("Sarah", "+61400000003")
        royce = self.clients.add("Royce", "+61400000004")
        gone = self.clients.add("Gone", "+61400000005")
        tom = self.clients.add("Tom", "+61400000006")
        for client, impression in ((sarah, Impression("Learning to drive; cheerful.", "Lovely")),
                                   (royce, Impression("Talks cars and work; blunt.", "Straight shooter")),
                                   (gone, Impression("Was here.", "Fine"))):
            self.impressions.save(self.chat(client, "Hi"), client.id, impression)
        self.clients.remove(gone.id)
        self.assertEqual(json.loads(self.impressions.people_context(datetime.now(timezone.utc))), [
            {"name": "Royce", "impression": "Talks cars and work; blunt."},
            {"name": "Sarah", "impression": "Learning to drive; cheerful."},
            {"name": "Tom", "impression": None},
        ])
        context = self.impressions.client_context(sarah.id)
        self.assertEqual(json.loads(context), {
            "them": "Learning to drive; cheerful.",
            "others": [{"name": "Royce", "opinion": "Straight shooter"}, {"name": "Tom", "opinion": None}],
        })
        # Nothing of anyone else's impression, nor anyone removed.
        self.assertNotIn("cars", context)
        self.assertNotIn("Gone", context)
        self.clients.remove(royce.id)
        self.clients.remove(tom.id)
        self.clients.remove(sarah.id)
        lone = self.clients.add("Lone", "+61400000007")
        self.assertEqual(self.impressions.client_context(lone.id), "")
        self.assertEqual(self.impressions.people_context(datetime.now(timezone.utc)), '[{"name": "Lone", "impression": null}]')

    def test_bad_chats_wait_to_be_reported_once_and_stay_in_the_persons_view_a_month(self):
        sarah = self.clients.add("Sarah", "+61400000003")
        now = datetime.now(timezone.utc)
        old = self.chat(sarah, "Old", started=now - timedelta(days=40))
        fine = self.chat(sarah, "Fine", started=now - timedelta(days=3))
        rude = self.chat(sarah, "Rude", started=now - timedelta(days=2))
        self.impressions.save(old, sarah.id, Impression("Was grumpy.", "Fine"), Conduct("abusive", "Long ago"))
        self.impressions.save(fine, sarah.id, Impression("Fine.", "Fine"), Conduct("fine"))
        self.impressions.save(rude, sarah.id, Impression("Blunt lately.", "Blunt"), Conduct("rude", "Called me useless"))
        heads_ups = self.impressions.unreported_heads_ups()
        self.assertEqual([(h.conversation_id, h.name, h.conduct, h.note) for h in heads_ups],
                         [(old, "Sarah", "abusive", "Long ago"), (rude, "Sarah", "rude", "Called me useless")])
        self.impressions.mark_reported(old)
        self.impressions.mark_reported(old)
        self.assertEqual([h.conversation_id for h in self.impressions.unreported_heads_ups()], [rude])
        [person] = json.loads(self.impressions.people_context(now))
        self.assertEqual(person["recent_concern"], {
            "when": (now - timedelta(days=2)).date().isoformat(), "conduct": "rude", "note": "Called me useless"})
        # Never in a client's view, of themselves or anyone else.
        self.assertNotIn("useless", self.impressions.client_context(sarah.id))
        with self.assertRaises(ValueError):
            self.impressions.save(self.chat(sarah, "x"), sarah.id, Impression("x", "y"), Conduct("rude", " "))

    def test_waiting_chats_are_folded_in_order_and_failures_say_only_the_type(self):
        sarah = self.clients.add("Sarah", "+61400000003")
        now = datetime.now(timezone.utc)
        first = self.chat(sarah, "First", started=now - timedelta(hours=5))
        second = self.chat(sarah, "Second", started=now - timedelta(hours=1))
        seen = []

        def fake_form(client, model, previous, transcript):
            seen.append((previous, json.loads(transcript)["messages"][0]["text"], json.loads(transcript)["user_is"]))
            return Impression(f"After {len(seen)}", "Fine"), Conduct("rude", "Short with me") if len(seen) == 2 else Conduct("fine")

        activity = []
        with patch("subconscious.impressions.form_impression.OpenAI"), \
                patch("subconscious.impressions.form_impression.form_impression", fake_form):
            update_waiting_impressions(self.engine, model="m", should_stop=lambda: False,
                                       write_activity=lambda *entry: activity.append(entry))
        self.assertEqual([(previous, text) for previous, text, _ in seen], [(None, "First"), (Impression("After 1", "Fine"), "Second")])
        self.assertTrue(seen[0][2].startswith("Sarah"))
        self.assertEqual(self.impressions.impression(sarah.id), Impression("After 2", "Fine"))
        self.assertEqual(activity, [(f"Impression revised: conversation_id={first}, conduct=fine", "Impression revised"),
                                    (f"Impression revised: conversation_id={second}, conduct=rude", "Impression revised")])
        self.assertEqual([h.conversation_id for h in self.impressions.unreported_heads_ups()], [second])

        self.chat(sarah, "Third")
        activity.clear()
        with patch("subconscious.impressions.form_impression.OpenAI"), \
                patch("subconscious.impressions.form_impression.form_impression", side_effect=RuntimeError("private")):
            update_waiting_impressions(self.engine, model="m", should_stop=lambda: False,
                                       write_activity=lambda *entry: activity.append(entry))
        self.assertEqual(activity, [("Impressions stopped (RuntimeError).", "Impression failed")])
        self.assertEqual(len(self.impressions.waiting_conversations()), 1)  # retried next pass


if __name__ == "__main__":
    unittest.main()
