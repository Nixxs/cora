"""Tasks with other people: started with an approved message and a brief, answered through pass_on."""

from datetime import datetime, timedelta, timezone
import json
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import psycopg
from psycopg import sql
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

import app as app_module
from backend_chat import BackendRoute
from messaging.setup import LINKED, SignalStatus
from migrate import apply_migrations, database_settings
from services.confirmation import APPROVED, DECLINED
from session_config import instruction_sections
from storage.client_store import Client, ClientStore
from storage.database import build_engine
from storage.models import TaskPartyRecord, TaskRecord
from storage.signal_settings_store import SignalSettings
from storage.task_store import EmailParty, QuietParty, TaskStore, TASKS_CONTEXT_MAX_CHARS
from tools.close_task import validate_arguments as validate_close_arguments
from tools.send_signal_message import SendSignalMessageTool, validate_arguments
import test_keyboard_persistence as keyboard_tests

CORA = "+61400000001"
NICK = "+61400000002"
SARAH = Client(uuid4(), "Sarah", "+61400000003")
NEW_TASK = {"goal": "Find out when Sarah can do a driving lesson", "task_id": None,
            "brief": "Which weekday afternoons next week suit her for an hour's lesson. You may say Nick asked."}


class TaskToolTests(unittest.IsolatedAsyncioTestCase):
    def tool(self, outcome=APPROVED, goal="An open task", record=None):
        self.connection = MagicMock()
        self.connection.response.item.create = AsyncMock()
        self.connection.response.create = AsyncMock()
        self.confirm = AsyncMock(return_value=outcome)
        self.send = AsyncMock()
        self.task_goal = AsyncMock(return_value=goal)
        self.record = record or AsyncMock(return_value=uuid4())
        return SendSignalMessageTool(
            lambda: self.connection, lambda: False, MagicMock(), lambda name: (SARAH, None),
            self.confirm, self.send, task_goal=self.task_goal, record_task=self.record,
        )

    def output(self):
        return json.loads(self.connection.response.item.create.call_args.kwargs["item"]["output"])

    def test_a_task_has_a_new_goal_or_an_open_task_and_always_a_brief(self):
        validate_arguments({"to": "Sarah", "message": "Hi", "task": NEW_TASK})
        validate_arguments({"to": "Sarah", "message": "Hi", "task": {"goal": None, "task_id": str(uuid4()), "brief": "b"}})
        for task in ({**NEW_TASK, "task_id": str(uuid4())}, {**NEW_TASK, "goal": None},
                     {**NEW_TASK, "brief": " "}, {**NEW_TASK, "goal": None, "task_id": "nope"}, {"goal": "g"}):
            with self.subTest(task=task), self.assertRaises(ValueError):
                validate_arguments({"to": "Sarah", "message": "Hi", "task": task})

    async def test_the_person_approves_the_brief_with_the_message_then_the_task_is_recorded(self):
        task_id = uuid4()
        tool = self.tool(record=AsyncMock(return_value=task_id))
        await tool.handle("c1", {"to": "Sarah", "message": "Hi Sarah, when suits you for a lesson?", "task": NEW_TASK})
        self.confirm.assert_awaited_once_with("signal", "send_signal_message", {
            "to": f"Sarah ({SARAH.number})", "message": "Hi Sarah, when suits you for a lesson?",
            "task": NEW_TASK["goal"], "brief": NEW_TASK["brief"],
        }, title="Signal")
        self.send.assert_awaited_once_with(SARAH, "Hi Sarah, when suits you for a lesson?")
        self.record.assert_awaited_once_with(
            SARAH, NEW_TASK["goal"], None, NEW_TASK["brief"], "Hi Sarah, when suits you for a lesson?")
        self.assertIn(str(task_id), self.output()["message"])

    async def test_declined_means_no_message_and_no_task(self):
        tool = self.tool(outcome=DECLINED)
        await tool.handle("c1", {"to": "Sarah", "message": "Hi", "task": NEW_TASK})
        self.send.assert_not_awaited()
        self.record.assert_not_awaited()

    async def test_adding_someone_to_an_open_task_shows_its_goal_and_an_unknown_one_asks_nothing(self):
        task_id = uuid4()
        tool = self.tool(goal="Book Sarah a driving lesson")
        await tool.handle("c1", {"to": "Sarah", "message": "Hi", "task": {"goal": None, "task_id": str(task_id), "brief": "b"}})
        self.assertEqual(self.confirm.await_args.args[2]["task"], "Book Sarah a driving lesson")
        self.record.assert_awaited_once_with(SARAH, None, task_id, "b", "Hi")
        tool = self.tool(goal=None)
        await tool.handle("c1", {"to": "Sarah", "message": "Hi", "task": {"goal": None, "task_id": str(task_id), "brief": "b"}})
        self.confirm.assert_not_awaited()
        self.assertEqual(self.output()["status"], "error")

    async def test_a_task_that_cant_be_saved_is_still_sent_and_said(self):
        tool = self.tool(record=AsyncMock(side_effect=RuntimeError("private")))
        await tool.handle("c1", {"to": "Sarah", "message": "Hi", "task": NEW_TASK})
        self.send.assert_awaited_once()
        self.assertIn("couldn't be saved", self.output()["message"])


class TaskSectionTests(unittest.TestCase):
    def test_the_person_sees_tasks_and_a_party_sees_only_their_brief(self):
        mine = instruction_sections(tasks_context='[{"goal": "g"}]')["tasks"]
        self.assertIn("Your tasks with other people", mine)
        theirs = instruction_sections(task_briefs='[{"task_id": "t", "brief": "b"}]')["tasks"]
        self.assertIn("pass_on with that task_id, settled true", theirs)
        self.assertIn("settled false", theirs)
        self.assertIn("never mention other tasks", theirs)
        self.assertIn("untrusted", theirs)


class TaskCheckAppTests(unittest.IsolatedAsyncioTestCase):
    def linked(self, app):
        app.signal.status = SignalStatus(LINKED, account=CORA)
        app.signal_settings = SignalSettings(True, CORA, NICK)
        app.signal_clients = {SARAH.number: SARAH}
        app.signal.client = MagicMock()
        app.signal.client.send = AsyncMock()
        app.backend_chat.reply = AsyncMock(return_value="Hi Sarah, any thoughts on a lesson time?")
        app.persistence.client_conversation.return_value = self.conversation_id = uuid4()
        app.persistence.load_client_history.return_value = []

    def party(self, nudges=0, last_message_at=None, person_id=SARAH.id, name="Sarah"):
        return QuietParty(uuid4(), uuid4(), person_id, name, "Lesson for Sarah", "When suits her", nudges, last_message_at)

    async def check(self, app, pilot, quiet=(), stale=(), hours=range(0, 24), unsettled=()):
        app.persistence.load_task_checks.return_value = (list(quiet), list(stale), list(unsettled))
        with patch.object(app_module, "NUDGE_HOURS", hours):
            app.check_tasks()
            for _ in range(3):
                await pilot.pause()
                await app.workers.wait_for_complete()

    async def test_a_quiet_party_gets_one_short_nudge_in_their_own_chat(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.linked(app)
            party = self.party()
            await self.check(app, pilot, quiet=[party])
            kwargs = app.backend_chat.reply.await_args.kwargs
            self.assertIn(str(party.task_id), kwargs["content"][0]["text"])
            self.assertEqual(kwargs["route"].excluded, frozenset(app.tools.names()))
            app.signal.client.send.assert_awaited_once_with(CORA, SARAH.number, "Hi Sarah, any thoughts on a lesson time?")
            app.persistence.save_client_message.assert_called_once_with(
                self.conversation_id, "assistant", "Hi Sarah, any thoughts on a lesson time?")
            app.persistence.record_task_nudge.assert_called_once_with(
                party.party_id, "Nudged Sarah: Hi Sarah, any thoughts on a lesson time?")

    async def test_no_nudge_at_night_or_mid_conversation(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.linked(app)
            await self.check(app, pilot, quiet=[self.party()], hours=range(0))
            recent = datetime.now(timezone.utc) - timedelta(minutes=5)
            await self.check(app, pilot, quiet=[self.party(last_message_at=recent)])
            app.signal.client.send.assert_not_awaited()
            app.persistence.record_task_nudge.assert_not_called()

    async def test_after_two_nudges_or_without_the_contact_or_a_week_stuck_it_comes_back_to_you(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.linked(app)
            ignored, removed = self.party(nudges=2), self.party(person_id=uuid4(), name="Royce")
            stale = (uuid4(), "Car service")
            await self.check(app, pilot, quiet=[ignored, removed], stale=[stale])
            sent = [call.args[2] for call in app.signal.client.send.await_args_list]
            self.assertEqual(sent, [
                'About your task "Lesson for Sarah": Sarah hasn\'t answered, even after 2 reminders. What would you like me to do?',
                'About your task "Lesson for Sarah": Royce isn\'t one of my contacts any more, so I can\'t ask them. '
                "What would you like me to do?",
                'About your task "Car service": it\'s been a week without it moving on. What would you like me to do?',
            ])
            self.assertEqual([call.args[0] for call in app.persistence.task_needs_person.call_args_list],
                             [ignored.task_id, removed.task_id, stale[0]])
            app.backend_chat.reply.assert_not_awaited()

    async def test_a_stuck_task_that_cant_reach_you_is_left_for_next_round(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.check(app, pilot, stale=[(uuid4(), "Car service")])
            app.persistence.task_needs_person.assert_not_called()

    async def test_closing_a_task_at_your_word(self):
        validate_close_arguments({"task_id": str(uuid4()), "status": "cancelled", "note": "No longer needed"})
        for bad in ({"task_id": "x", "status": "done", "note": "n"}, {"task_id": str(uuid4()), "status": "open", "note": "n"},
                    {"task_id": str(uuid4()), "status": "done", "note": " "}):
            with self.subTest(arguments=bad), self.assertRaises(ValueError):
                validate_close_arguments(bad)
        async with keyboard_tests.running_app() as (app, pilot):
            task_id = uuid4()
            self.assertIsNone(await app.close_task(task_id, "done", "Booked it myself"))
            app.persistence.close_task.assert_called_once_with(task_id, "done", "Booked it myself")
            app.persistence.close_task.side_effect = LookupError("no")
            self.assertIn("no open task", await app.close_task(task_id, "done", "x"))


class CoordinatorAppTests(unittest.IsolatedAsyncioTestCase):
    TASK = {"task_id": "t", "goal": "Lesson for Sarah", "status": "waiting", "started": "2026-10-30",
            "parties": [{"name": "Sarah", "status": "done", "brief": "When suits her", "outcome": "Tuesdays"}],
            "steps": ["Asked Sarah", "Sarah reported: Tuesdays"]}

    def linked(self, app, reply="Sarah can do Tuesdays after 4, so that's sorted."):
        app.signal.status = SignalStatus(LINKED, account=CORA)
        app.signal_settings = SignalSettings(True, CORA, NICK)
        app.signal.client = MagicMock()
        app.signal.client.send = AsyncMock()
        app.persistence.task_coordination.return_value = self.TASK
        app.persistence.task_needs_person.return_value = True
        app.backend_chat.reply = AsyncMock(return_value=reply) if isinstance(reply, str) else reply

    async def settle(self, app, pilot):
        for _ in range(3):
            await pilot.pause()
            await app.workers.wait_for_complete()

    async def test_an_answer_goes_through_a_turn_in_your_context_that_can_only_search_and_close(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.linked(app)
            app.persistence.load_tasks_context.return_value = '[{"goal": "Lesson for Sarah"}]'
            task_id = uuid4()
            app.coordinate_task(task_id, "Sarah answered: Tuesdays", "From Sarah: Tuesdays")
            await self.settle(app, pilot)
            kwargs = app.backend_chat.reply.await_args.kwargs
            note = kwargs["content"][0]["text"]
            self.assertIn("Never message anyone yourself", note)
            self.assertEqual(json.loads(note[note.index("{"):]), {"news": "Sarah answered: Tuesdays", "task": self.TASK})
            self.assertIn("Lesson for Sarah", kwargs["sections"]["tasks"])
            self.assertEqual(frozenset(app.tools.names()) - kwargs["route"].excluded, {"search_memory", "close_task"})
            app.signal.client.send.assert_awaited_once_with(CORA, NICK, "Sarah can do Tuesdays after 4, so that's sorted.")
            # Once told, it waits on the person unless someone is still to answer.
            app.persistence.task_needs_person.assert_called_once_with(
                task_id, "Told them the news and asked what next.", True)
            self.assertEqual(app.tasks_coordinating, set())

    async def test_a_question_always_waits_on_you(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.linked(app, reply="Sarah needs the address before she can say.")
            task_id = uuid4()
            app.coordinate_task(task_id, "Sarah needs something from the person: address?", "From Sarah: address?", False)
            await self.settle(app, pilot)
            self.assertFalse(app.persistence.task_needs_person.call_args.args[2])

    async def test_if_the_turn_fails_the_answer_is_passed_on_as_it_came(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.linked(app, reply=AsyncMock(side_effect=RuntimeError("private")))
            app.coordinate_task(uuid4(), "Sarah answered: Tuesdays", "From Sarah, via Cora:\nTuesdays")
            await self.settle(app, pilot)
            app.signal.client.send.assert_awaited_once_with(CORA, NICK, "From Sarah, via Cora:\nTuesdays")
            app.persistence.task_needs_person.assert_called_once()

    async def test_a_closed_task_or_one_that_cant_reach_you_is_left_alone(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.linked(app)
            app.persistence.task_coordination.return_value = None
            app.coordinate_task(uuid4(), "news", "fallback")
            await self.settle(app, pilot)
            app.backend_chat.reply.assert_not_awaited()
            app.persistence.task_coordination.return_value = self.TASK
            app.signal_settings = SignalSettings(True, CORA, "")
            app.coordinate_task(uuid4(), "news", "fallback")
            await self.settle(app, pilot)
            app.persistence.task_needs_person.assert_not_called()

    async def test_the_hourly_round_retries_answers_that_never_reached_you(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.linked(app)
            busy, waiting = uuid4(), uuid4()
            app.tasks_coordinating.add(busy)
            app.persistence.load_task_checks.return_value = ([], [], [(busy, "Busy"), (waiting, "Lesson for Sarah")])
            app.check_tasks()
            await self.settle(app, pilot)
            self.assertEqual(app.persistence.task_coordination.call_args_list, [((waiting,),)])
            self.assertEqual(app.tasks_coordinating, {busy})


class TaskAppTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_new_task_or_a_further_party_is_saved_with_what_she_sent(self):
        async with keyboard_tests.running_app() as (app, pilot):
            task_id = uuid4()
            app.persistence.start_task.return_value = task_id
            self.assertEqual(await app.record_task(SARAH, "Goal", None, "Brief", "Hi Sarah"), task_id)
            app.persistence.start_task.assert_called_once_with("Goal", SARAH.id, "Brief", "Asked Sarah: Hi Sarah")
            self.assertEqual(await app.record_task(SARAH, None, task_id, "Brief 2", "And?"), task_id)
            app.persistence.add_task_party.assert_called_once_with(task_id, SARAH.id, "Brief 2", "Asked Sarah: And?")

    async def test_writing_as_herself_carries_her_tasks(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.persistence.load_tasks_context.return_value = '[{"goal": "Find a lesson time"}]'
            app.backend_chat.reply = AsyncMock(return_value="Sure.")
            await app.reply_in_writing([{"type": "input_text", "text": "Hi"}], [], BackendRoute())
            self.assertIn("Find a lesson time", app.backend_chat.reply.await_args.kwargs["sections"]["tasks"])


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class TaskStoreDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        settings = database_settings()
        admin = psycopg.connect(**settings, autocommit=True)
        cls.addClassCleanup(admin.close)
        cls.database = "cora_test_tasks_" + uuid4().hex
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.database)))
        cls.addClassCleanup(admin.execute, sql.SQL("DROP DATABASE {}").format(sql.Identifier(cls.database)))
        with psycopg.connect(**{**settings, "dbname": cls.database}, autocommit=True) as connection:
            apply_migrations(connection)
        with patch("storage.database.database_settings", return_value={**settings, "dbname": cls.database}):
            cls.engine = build_engine()
        cls.addClassCleanup(cls.engine.dispose)

    def setUp(self):
        with self.engine.begin() as connection:
            for table in ("task_steps", "task_parties", "tasks", "messages", "conversations", "people"):
                connection.execute(text(f"DELETE FROM public.{table}"))
        self.tasks = TaskStore(self.engine)
        clients = ClientStore(self.engine)
        self.sarah = clients.add("Sarah", "+61400000003")
        self.royce = clients.add("Royce", "+61400000004")

    def steps(self, task_id):
        with self.engine.connect() as connection:
            return [row[0] for row in connection.execute(
                text("SELECT text FROM public.task_steps WHERE task_id = :t ORDER BY id"), {"t": task_id})]

    def test_a_task_runs_from_first_contact_to_done_at_the_coordinators_word(self):
        task_id = self.tasks.start("Lesson for Sarah", self.sarah.id, "When suits her", "Asked Sarah: when?")
        self.assertEqual(self.tasks.goal(task_id), "Lesson for Sarah")
        self.tasks.add_party(task_id, self.royce.id, "Can he drive her", "Asked Royce: can you?")
        self.assertEqual(self.tasks.report(task_id, self.sarah.id, "What time of day?", settled=False), "Lesson for Sarah")
        self.assertEqual(self.tasks.unsettled_tasks(), [])
        self.assertEqual(self.tasks.report(task_id, self.sarah.id, "Tuesdays after 4"), "Lesson for Sarah")
        self.assertEqual(self.tasks.unsettled_tasks(), [])  # Royce is still to answer
        self.tasks.report(task_id, self.royce.id, "Yes, Tuesdays")
        # Everyone has answered, but only the coordinator decides it's done.
        self.assertEqual(self.tasks.unsettled_tasks(), [(task_id, "Lesson for Sarah")])
        self.tasks.close(task_id, "done", "Tuesdays after 4, Royce driving")
        with Session(self.engine) as session:
            task = session.get(TaskRecord, task_id)
            self.assertEqual((task.status, task.result), ("done", "Tuesdays after 4, Royce driving"))
            self.assertIsNotNone(task.closed_at)
        self.assertEqual(self.steps(task_id), [
            "Asked Sarah: when?", "Asked Royce: can you?", "Sarah asked: What time of day?",
            "Sarah reported: Tuesdays after 4", "Royce reported: Yes, Tuesdays", "Done: Tuesdays after 4, Royce driving",
        ])
        self.assertEqual(self.tasks.unsettled_tasks(), [])
        # Closed: no more answers, no goal for new contacts, no briefs in their chats.
        self.assertIsNone(self.tasks.goal(task_id))
        with self.assertRaises(LookupError):
            self.tasks.report(task_id, self.sarah.id, "Actually Wednesday")
        with self.assertRaises(LookupError):
            self.tasks.add_party(task_id, self.sarah.id, "b", "s")
        self.assertEqual(self.tasks.briefs_for(self.sarah.id), "")

    def test_only_a_party_can_answer_and_each_sees_only_their_own_brief(self):
        hers = self.tasks.start("Lesson for Sarah", self.sarah.id, "When suits her", "Asked Sarah")
        his = self.tasks.start("Car service", self.royce.id, "Which garage", "Asked Royce")
        with self.assertRaises(LookupError):
            self.tasks.report(hers, self.royce.id, "Not mine")
        self.assertEqual(json.loads(self.tasks.briefs_for(self.sarah.id)), [{"task_id": str(hers), "brief": "When suits her"}])
        self.assertNotIn("garage", self.tasks.briefs_for(self.sarah.id))
        self.tasks.report(hers, self.sarah.id, "Tuesdays")
        self.assertEqual(self.tasks.briefs_for(self.sarah.id), "")
        self.assertEqual(json.loads(self.tasks.briefs_for(self.royce.id))[0]["task_id"], str(his))

    def test_the_person_sees_open_and_lately_finished_tasks_with_each_party(self):
        old = self.tasks.start("Old errand", self.sarah.id, "b", "s")
        self.tasks.report(old, self.sarah.id, "Done long ago")
        self.tasks.close(old, "done", "Done long ago")
        with Session(self.engine) as session, session.begin():
            session.execute(update(TaskRecord).where(TaskRecord.id == old).values(
                closed_at=datetime.now(timezone.utc) - timedelta(days=10)))
        recent = self.tasks.start("Lesson for Sarah", self.sarah.id, "b", "s")
        self.tasks.report(recent, self.sarah.id, "Tuesdays")
        self.tasks.close(recent, "done", "Tuesdays")
        waiting = self.tasks.start("Car service", self.royce.id, "b", "s")
        context = json.loads(self.tasks.tasks_context(datetime.now(timezone.utc)))
        self.assertEqual([(task["goal"], task["status"]) for task in context],
                         [("Lesson for Sarah", "done"), ("Car service", "waiting")])
        self.assertEqual(context[0]["parties"], [{"name": "Sarah", "status": "done", "outcome": "Tuesdays"}])
        self.assertEqual(context[1]["task_id"], str(waiting))

    def age(self, task_id, **times):
        """Move a task's party times back, as if it were asked days ago."""
        with Session(self.engine) as session, session.begin():
            session.execute(update(TaskPartyRecord).where(TaskPartyRecord.task_id == task_id).values(**times))

    def test_parties_quiet_for_a_day_are_due_a_nudge_counted_from_their_last_word_or_nudge(self):
        now = datetime.now(timezone.utc)
        task_id = self.tasks.start("Lesson for Sarah", self.sarah.id, "When suits her", "Asked Sarah")
        self.assertEqual(self.tasks.quiet_parties(now), [])
        self.age(task_id, contacted_at=now - timedelta(days=2))
        [party] = self.tasks.quiet_parties(now)
        self.assertEqual((party.task_id, party.name, party.brief, party.nudges, party.last_message_at),
                         (task_id, "Sarah", "When suits her", 0, None))
        self.tasks.record_nudge(party.party_id, "Nudged Sarah: any thoughts?")
        self.assertEqual(self.tasks.quiet_parties(now), [])
        self.age(task_id, nudged_at=now - timedelta(days=2))
        self.assertEqual(self.tasks.quiet_parties(now)[0].nudges, 1)
        self.age(task_id, last_heard_at=now - timedelta(hours=2))
        self.assertEqual(self.tasks.quiet_parties(now), [])
        self.assertEqual(self.steps(task_id)[-1], "Nudged Sarah: any thoughts?")

    def test_a_task_comes_back_to_the_person_once_and_resumes_when_someone_is_asked_again(self):
        now = datetime.now(timezone.utc)
        task_id = self.tasks.start("Lesson for Sarah", self.sarah.id, "When suits her", "Asked Sarah")
        with Session(self.engine) as session, session.begin():
            session.execute(update(TaskRecord).where(TaskRecord.id == task_id).values(updated_at=now - timedelta(days=8)))
        self.assertEqual(self.tasks.stale_tasks(now), [(task_id, "Lesson for Sarah")])
        self.assertTrue(self.tasks.needs_person(task_id, "No answer"))
        self.assertFalse(self.tasks.needs_person(task_id, "Again"))
        self.assertEqual(self.tasks.stale_tasks(now), [])
        self.age(task_id, contacted_at=now - timedelta(days=2))
        self.assertEqual(self.tasks.quiet_parties(now), [])  # waits on the person, not on Sarah
        context = json.loads(self.tasks.tasks_context(now))[0]
        self.assertEqual((context["status"], context["latest_step"]), ("needs_person", "Back to the person: No answer"))
        self.tasks.add_party(task_id, self.sarah.id, "When suits her, any day", "Asked Sarah again")
        self.assertEqual(json.loads(self.tasks.tasks_context(now))[0]["status"], "waiting")

    def test_closing_at_the_persons_word_ends_the_briefs(self):
        task_id = self.tasks.start("Lesson for Sarah", self.sarah.id, "When suits her", "Asked Sarah")
        self.assertEqual(self.tasks.close(task_id, "cancelled", "Not needed now"), "Lesson for Sarah")
        self.assertEqual(self.tasks.briefs_for(self.sarah.id), "")
        self.assertEqual(self.steps(task_id)[-1], "Cancelled: Not needed now")
        with self.assertRaises(LookupError):
            self.tasks.close(task_id, "done", "x")
        other = self.tasks.start("Car service", self.royce.id, "b", "s")
        self.tasks.close(other, "done", "Booked it myself")
        with Session(self.engine) as session:
            self.assertEqual(session.get(TaskRecord, other).result, "Booked it myself")

    def test_the_coordinator_sees_the_whole_open_task_with_its_latest_steps(self):
        task_id = self.tasks.start("Lesson for Sarah", self.sarah.id, "When suits her", "Asked Sarah")
        self.tasks.add_party(task_id, self.royce.id, "Can he drive her", "Asked Royce")
        self.tasks.report(task_id, self.sarah.id, "Tuesdays")
        view = self.tasks.coordination(task_id)
        self.assertEqual((view["task_id"], view["goal"], view["status"]), (str(task_id), "Lesson for Sarah", "waiting"))
        self.assertEqual(view["parties"], [
            {"name": "Sarah", "status": "done", "brief": "When suits her", "outcome": "Tuesdays"},
            {"name": "Royce", "status": "waiting", "brief": "Can he drive her", "outcome": None},
        ])
        self.assertEqual(view["steps"], ["Asked Sarah", "Asked Royce", "Sarah reported: Tuesdays"])
        for n in range(20):
            self.tasks.report(task_id, self.royce.id, f"Thinking {n} " + "x" * 2000, settled=False)
        view = self.tasks.coordination(task_id)
        self.assertLessEqual(len(json.dumps(view, ensure_ascii=False)), TASKS_CONTEXT_MAX_CHARS)
        self.assertTrue(view["steps"][-1].startswith("Royce asked: Thinking 19"))
        self.tasks.close(task_id, "cancelled", "Not now")
        self.assertIsNone(self.tasks.coordination(task_id))

    def test_the_dashboard_sees_open_tasks_with_each_partys_next_check(self):
        now = datetime.now(timezone.utc)
        task_id = self.tasks.start("Lesson for Sarah", self.sarah.id, "When suits her", "Asked Sarah")
        self.tasks.add_party(task_id, self.royce.id, "Can he drive her", "Asked Royce")
        self.age(task_id, contacted_at=now - timedelta(hours=20))
        self.tasks.report(task_id, self.royce.id, "Yes")
        stuck = self.tasks.start("Car service", self.royce.id, "Which garage", "Asked Royce")
        self.tasks.needs_person(stuck, "No answer")
        closed = self.tasks.start("Old", self.sarah.id, "b", "s")
        self.tasks.close(closed, "done", "x")
        lesson, car = self.tasks.open_tasks()
        self.assertEqual((lesson.goal, lesson.status, lesson.latest_step), ("Lesson for Sarah", "waiting", "Royce reported: Yes"))
        sarah, royce = lesson.parties
        self.assertEqual((sarah.name, sarah.status, sarah.nudges), ("Sarah", "waiting", 0))
        self.assertAlmostEqual(sarah.due_at, now + timedelta(hours=4), delta=timedelta(minutes=1))
        self.assertEqual((royce.status, royce.due_at), ("done", None))
        self.assertGreater(lesson.stale_at, now + timedelta(days=6))
        self.assertEqual((car.status, car.stale_at, car.parties[0].due_at), ("needs_person", None, None))

    def test_an_email_party_is_named_by_who_they_are_or_their_address_with_its_thread(self):
        task_id = self.tasks.start("Book a lesson", EmailParty("Info@School.example", "Bright School", "thread-1"),
                                   "Times and price", "Emailed Bright School: Hi")
        self.tasks.add_party(task_id, EmailParty("bob@garage.example", None, "thread-2"), "Can he drive", "Emailed bob")
        self.tasks.add_party(task_id, self.sarah.id, "When suits her", "Asked Sarah")
        # Emailing the same address again for this task asks them again, in the new thread.
        self.tasks.add_party(task_id, EmailParty("info@school.example", "Bright School", "thread-3"), "Just times", "Again")
        with Session(self.engine) as session:
            school = session.scalar(select(TaskPartyRecord).where(TaskPartyRecord.address == "info@school.example"))
            self.assertEqual((school.channel, school.name, school.thread_id, school.brief, school.person_id),
                             ("email", "Bright School", "thread-3", "Just times", None))
        [context] = json.loads(self.tasks.tasks_context(datetime.now(timezone.utc)))
        # In the order they were (last) asked.
        self.assertEqual(context["parties"], [
            {"name": "bob@garage.example", "status": "waiting", "outcome": None, "email": "bob@garage.example"},
            {"name": "Sarah", "status": "waiting", "outcome": None},
            {"name": "Bright School", "status": "waiting", "outcome": None, "email": "info@school.example"},
        ])
        self.assertEqual([party["name"] for party in self.tasks.coordination(task_id)["parties"]],
                         ["bob@garage.example", "Sarah", "Bright School"])
        [view] = self.tasks.open_tasks()
        self.assertEqual([party.name for party in view.parties], ["bob@garage.example", "Sarah", "Bright School"])
        # Email parties wait for E3's mail check, not for Signal nudges.
        self.age(task_id, contacted_at=datetime.now(timezone.utc) - timedelta(days=2))
        self.assertEqual([party.name for party in self.tasks.quiet_parties(datetime.now(timezone.utc))], ["Sarah"])

    def test_the_mail_check_reads_each_email_thread_once_and_their_word_reaches_the_task(self):
        task_id = self.tasks.start("Book a lesson", EmailParty("info@school.example", "Bright School", "thread-1"),
                                   "Times and price", "Emailed Bright School: Hi")
        self.tasks.add_party(task_id, self.sarah.id, "When suits her", "Asked Sarah")
        self.tasks.add_party(task_id, EmailParty("no-thread@x.example", None, None), "b", "s")
        [thread] = self.tasks.email_threads()
        self.assertEqual((thread.task_id, thread.goal, thread.address, thread.name, thread.brief, thread.thread_id),
                         (task_id, "Book a lesson", "info@school.example", "Bright School", "Times and price", "thread-1"))
        self.assertEqual(self.tasks.email_thread(task_id, "Info@School.example "), "thread-1")
        # Read up to Microsoft's time for their latest; it never moves back.
        later = thread.since + timedelta(hours=2)
        self.tasks.email_heard(thread.party_id, later)
        self.tasks.email_heard(thread.party_id, later - timedelta(hours=1))
        [thread] = self.tasks.email_threads()
        self.assertEqual(thread.since, later)
        # Their word, through pass_on, is the task's; it leaves what the mail check has read alone.
        self.assertEqual(self.tasks.report_email(task_id, thread.party_id, "Tuesdays, $70"), "Book a lesson")
        self.assertEqual(self.tasks.email_threads()[0].since, later)
        self.assertEqual(self.steps(task_id)[-1], "Bright School reported: Tuesdays, $70")
        with self.assertRaises(LookupError):
            self.tasks.report_email(uuid4(), thread.party_id, "x")
        self.tasks.close(task_id, "done", "Booked")
        self.assertEqual((self.tasks.email_threads(), self.tasks.email_thread(task_id, "info@school.example")), ([], None))

    def test_a_task_waits_on_the_person_unless_someone_is_still_to_answer(self):
        task_id = self.tasks.start("Lesson for Sarah", self.sarah.id, "When suits her", "Asked Sarah")
        self.assertFalse(self.tasks.needs_person(task_id, "Told them", unless_waiting=True))
        self.assertTrue(self.tasks.needs_person(task_id, "Told them"))

    def test_hearing_from_a_party_is_noted_only_while_their_part_is_open(self):
        task_id = self.tasks.start("Lesson for Sarah", self.sarah.id, "b", "s")
        self.tasks.heard_from(self.sarah.id)
        with self.engine.connect() as connection:
            heard = connection.execute(text("SELECT last_heard_at FROM public.task_parties WHERE task_id = :t"),
                                       {"t": task_id}).scalar()
        self.assertIsNotNone(heard)


if __name__ == "__main__":
    unittest.main()
