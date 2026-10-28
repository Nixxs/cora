"""Clients passing things on to the person through Cora: only ever to the person, marked, and limited."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from backend_chat import SIGNAL_EXCLUDED, STANDBY_EXCLUDED, BackendRoute
from messaging.setup import LINKED, SignalStatus
from storage.signal_settings_store import SignalSettings
from tools.pass_on import CLIENT_ONLY, SCHEMA, PassOnTool, validate_arguments
import test_keyboard_persistence as keyboard_tests

CORA = "+61400000001"
NICK = "+61400000002"


class ToolTests(unittest.IsolatedAsyncioTestCase):
    def tool(self, connection, problem=None):
        connection.response.item.create = AsyncMock()
        connection.response.create = AsyncMock()
        self.relay = AsyncMock(return_value=problem)
        return PassOnTool(lambda: connection, lambda: False, MagicMock(), self.relay)

    def output(self, connection):
        return json.loads(connection.response.item.create.call_args.kwargs["item"]["output"])

    def test_schema_is_strict_with_a_message_and_an_optional_task(self):
        self.assertTrue(SCHEMA["strict"])
        self.assertEqual(SCHEMA["name"], "pass_on")
        validate_arguments({"message": "The plan", "task_id": None})
        validate_arguments({"message": "Friday at 10", "task_id": str(uuid4())})
        for bad in ({}, {"message": "hi"}, {"message": " ", "task_id": None}, {"message": "x" * 3001, "task_id": None},
                    {"message": "hi", "task_id": None, "to": "Bob"}, {"message": "hi", "task_id": "not-an-id"},
                    {"message": "hi", "task_id": 3}):
            with self.subTest(arguments=bad), self.assertRaises(ValueError):
                validate_arguments(bad)

    async def test_from_a_clients_chat_it_is_passed_on_as_theirs(self):
        client_id = uuid4()
        route = BackendRoute(memory_scope=(client_id, "Royce"))
        tool = self.tool(route)
        await tool.handle("c1", {"message": " Here's the plan we worked out. ", "task_id": None})
        self.relay.assert_awaited_once_with(client_id, "Royce", "Here's the plan we worked out.", None)
        self.assertEqual(self.output(route)["status"], "ok")
        task_id = uuid4()
        await tool.handle("c2", {"message": "Friday at 10", "task_id": str(task_id)})
        self.relay.assert_awaited_with(client_id, "Royce", "Friday at 10", task_id)

    async def test_anywhere_else_it_refuses(self):
        for connection in (BackendRoute(), MagicMock()):
            with self.subTest(connection=type(connection).__name__):
                tool = self.tool(connection)
                await tool.handle("c1", {"message": "hi", "task_id": None})
                self.relay.assert_not_called()
                self.assertEqual(self.output(connection)["status"], "error")

    async def test_a_limit_or_problem_is_reported_not_hidden(self):
        route = BackendRoute(memory_scope=(uuid4(), "Royce"))
        tool = self.tool(route, problem="You've passed on as much as you can for Royce today; it wasn't sent.")
        await tool.handle("c1", {"message": "hi", "task_id": None})
        self.assertEqual(self.output(route)["status"], "not_done")

    def test_only_clients_chats_are_offered_it(self):
        self.assertTrue(CLIENT_ONLY <= STANDBY_EXCLUDED)
        self.assertTrue(CLIENT_ONLY <= SIGNAL_EXCLUDED)


class RelayAppTests(unittest.IsolatedAsyncioTestCase):
    async def linked(self, app):
        app.signal.status = SignalStatus(LINKED, account=CORA)
        app.signal_settings = SignalSettings(True, CORA, NICK, "uuid-nick")
        app.signal.client = MagicMock()
        app.signal.client.send = AsyncMock()

    async def test_it_reaches_the_person_marked_as_from_the_client_and_shows_in_their_chat(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.linked(app)
            records = []
            app.conversation.record_message = lambda role, text, channel: records.append((role, text, channel))
            client_id = uuid4()
            self.assertIsNone(await app.relay_to_person(client_id, "Royce", "Plan: meet Friday at 10."))
            app.signal.client.send.assert_awaited_once_with(CORA, "uuid-nick", "From Royce, via Cora:\nPlan: meet Friday at 10.")
            self.assertEqual(records, [("assistant", "From Royce, via Cora:\nPlan: meet Friday at 10.", "signal")])
            self.assertIn("Passed something on from Royce to you.", "\n".join(app.activity.entries))

    async def test_with_live_active_she_says_it_instead(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.linked(app)
            app.can_reach_by_voice = lambda: True
            self.assertIsNone(await app.relay_to_person(uuid4(), "Royce", "Running late."))
            [message] = app.attention.pending
            self.assertEqual(message.text, "From Royce, via Cora:\nRunning late.")
            app.signal.client.send.assert_not_awaited()

    async def test_a_task_answer_is_recorded_then_passed_on_naming_the_task(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.linked(app)
            client_id, task_id = uuid4(), uuid4()
            app.persistence.report_task.return_value = ("Find a lesson time for Sarah", True)
            self.assertIsNone(await app.relay_to_person(client_id, "Sarah", "Tuesdays after 4.", task_id))
            app.persistence.report_task.assert_called_once_with(task_id, client_id, "Tuesdays after 4.")
            app.signal.client.send.assert_awaited_once_with(CORA, "uuid-nick", (
                'From Sarah, via Cora, for your task "Find a lesson time for Sarah":\n'
                "Tuesdays after 4. That's everything for this task."
            ))
            app.persistence.report_task.side_effect = LookupError("no")
            self.assertIn("isn't an open task", await app.relay_to_person(client_id, "Sarah", "Hi", task_id))
            self.assertEqual(app.signal.client.send.await_count, 1)

    async def test_a_task_answer_is_kept_even_when_the_person_cant_be_reached(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.persistence.report_task.return_value = ("Goal", False)
            problem = await app.relay_to_person(uuid4(), "Sarah", "Tuesdays.", uuid4())
            self.assertIn("recorded on the task", problem)
            app.persistence.report_task.assert_called_once()

    async def test_each_client_can_pass_on_only_a_few_a_day(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.linked(app)
            royce, terry = uuid4(), uuid4()
            from app import SIGNAL_RELAYS_PER_DAY
            for _ in range(SIGNAL_RELAYS_PER_DAY):
                self.assertIsNone(await app.relay_to_person(royce, "Royce", "again"))
            self.assertIn("as much as you can for Royce today", await app.relay_to_person(royce, "Royce", "again"))
            self.assertIsNone(await app.relay_to_person(terry, "Terry", "hi"))
            self.assertEqual(app.signal.client.send.await_count, SIGNAL_RELAYS_PER_DAY + 1)

    async def test_without_signal_it_says_so(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.assertIn("can't be reached", await app.relay_to_person(uuid4(), "Royce", "hi"))

    async def test_live_never_sees_it(self):
        async with keyboard_tests.running_app() as (app, pilot):
            names = [schema["name"] for schema in app.live.tool_schemas()]
            self.assertNotIn("pass_on", names)
            self.assertIn("search_memory", names)
