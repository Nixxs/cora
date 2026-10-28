"""Cora messaging the people the person added, such as introducing herself. Approval always first. Mocked."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from backend_chat import SIGNAL_EXCLUDED
from messaging.setup import LINKED, NOT_LINKED, SignalStatus
from services.confirmation import APPROVED, DECLINED, TIMED_OUT
from storage.client_store import Client
from storage.signal_settings_store import SignalSettings
from tools.send_signal_message import SCHEMA, SendSignalMessageTool, validate_arguments
from ui.confirm_view import ConfirmScreen
import test_keyboard_persistence as keyboard_tests

CORA = "+61400000001"
NICK = "+61400000002"
SARAH = Client(uuid4(), "Sarah Jones", "+61400000003")
SAM = Client(uuid4(), "Sam Lee", "+61400000004")
SAMUEL = Client(uuid4(), "Sam Green", "+61400000005")


class ToolTests(unittest.IsolatedAsyncioTestCase):
    def tool(self, outcome=APPROVED, contact=SARAH, problem=None):
        self.connection = MagicMock()
        self.connection.response.item.create = AsyncMock()
        self.connection.response.create = AsyncMock()
        self.confirm = AsyncMock(return_value=outcome)
        self.send = AsyncMock()
        return SendSignalMessageTool(
            lambda: self.connection, lambda: False, MagicMock(),
            lambda name: (contact, problem), self.confirm, self.send,
        )

    def output(self):
        return json.loads(self.connection.response.item.create.call_args.kwargs["item"]["output"])

    def test_schema_is_strict_and_arguments_are_checked(self):
        self.assertTrue(SCHEMA["strict"])
        validate_arguments({"to": "Sarah", "message": "Hi", "task": None})
        for bad in ({"to": "Sarah"}, {"to": " ", "message": "Hi"}, {"to": "Sarah", "message": "x" * 2001},
                    {"to": "Sarah", "message": "Hi", "extra": 1}):
            with self.subTest(arguments=bad), self.assertRaises(ValueError):
                validate_arguments(bad)

    async def test_approved_messages_are_sent_exactly_as_shown(self):
        tool = self.tool()
        await tool.handle("c1", {"to": "Sarah", "message": " Hi Sarah, I'm Cora. ", "task": None})
        self.confirm.assert_awaited_once_with(
            "signal", "send_signal_message", {"to": f"Sarah Jones ({SARAH.number})", "message": "Hi Sarah, I'm Cora."},
            title="Signal",
        )
        self.send.assert_awaited_once_with(SARAH, "Hi Sarah, I'm Cora.")
        self.assertEqual(self.output()["status"], "ok")
        self.connection.response.create.assert_awaited_once()

    async def test_nothing_is_sent_without_approval(self):
        for outcome in (DECLINED, TIMED_OUT):
            with self.subTest(outcome=outcome):
                tool = self.tool(outcome)
                await tool.handle("c1", {"to": "Sarah", "message": "Hi", "task": None})
                self.send.assert_not_called()
                self.assertEqual(self.output()["status"], "not_done")

    async def test_an_unknown_contact_is_explained_without_asking(self):
        tool = self.tool(contact=None, problem="There's no contact by that name. You can message: Sarah Jones.")
        await tool.handle("c1", {"to": "Bob", "message": "Hi", "task": None})
        self.confirm.assert_not_called()
        self.assertIn("You can message: Sarah Jones", self.output()["message"])

    async def test_a_failed_send_says_so_without_echoing_the_text(self):
        tool = self.tool()
        self.send.side_effect = RuntimeError("private words")
        log = MagicMock()
        tool.write_debug = log
        await tool.handle("c1", {"to": "Sarah", "message": "private words", "task": None})
        self.assertEqual(self.output()["status"], "error")
        self.assertNotIn("private words", repr(log.call_args_list))


class AppTests(unittest.IsolatedAsyncioTestCase):
    async def linked(self, app):
        app.signal.status = SignalStatus(LINKED, account=CORA)
        app.signal_settings = SignalSettings(True, CORA, NICK)
        app.signal_clients = {client.number: client for client in (SARAH, SAM, SAMUEL)}
        app.signal.client = MagicMock()
        app.signal.client.send = AsyncMock()

    async def test_contacts_are_found_by_name_or_a_unique_first_name(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.linked(app)
            self.assertEqual(app.find_signal_contact("sarah  jones"), (SARAH, None))
            self.assertEqual(app.find_signal_contact("Sarah"), (SARAH, None))
            contact, problem = app.find_signal_contact("Sam")
            self.assertIsNone(contact)
            self.assertIn("More than one", problem)
            contact, problem = app.find_signal_contact("Bob")
            self.assertIn("You can message: Sam Green, Sam Lee, Sarah Jones", problem)
            app.signal_clients = {}
            self.assertIn("Nobody has been added", app.find_signal_contact("Sarah")[1])
            app.signal.status = SignalStatus(NOT_LINKED)
            self.assertIn("isn't set up", app.find_signal_contact("Sarah")[1])

    async def test_introducing_herself_asks_first_then_sends_and_saves_it_in_their_chat(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.linked(app)
            conversation_id = uuid4()
            app.persistence.client_conversation.return_value = conversation_id
            app.live.connection = MagicMock()
            app.live.connection.response.item.create = AsyncMock()
            app.live.connection.response.create = AsyncMock()
            app.live.send_approval_waiting = AsyncMock()
            app.tools.handle_function_call({
                "name": "send_signal_message", "call_id": "c1", "status": "completed",
                "arguments": json.dumps({"to": "Sarah", "message": "Hi Sarah, I'm Cora.", "task": None}),
            })
            for _ in range(30):
                if isinstance(app.screen, ConfirmScreen):
                    break
                await pilot.pause(0.1)
            self.assertIsInstance(app.screen, ConfirmScreen)
            self.assertEqual(app.screen.request.title, "Signal")
            app.signal.client.send.assert_not_called()
            app.live.send_approval_waiting.assert_awaited_once_with("Signal", "send_signal_message", reminder=False)
            await pilot.click("#confirm-approve")
            for _ in range(30):
                if app.signal.client.send.await_count:
                    break
                await pilot.pause(0.1)
            await pilot.pause()
            app.signal.client.send.assert_awaited_once_with(CORA, SARAH.number, "Hi Sarah, I'm Cora.")
            app.persistence.save_client_message.assert_called_once_with(conversation_id, "assistant", "Hi Sarah, I'm Cora.")

    def test_offered_to_the_person_on_signal_where_they_approve_in_the_chat(self):
        self.assertNotIn("send_signal_message", SIGNAL_EXCLUDED)


class FailureTextTests(unittest.TestCase):
    def test_signal_clis_reason_is_shown_and_other_errors_only_by_type(self):
        from messaging.signal_cli import SignalError, signal_failure
        self.assertEqual(signal_failure(SignalError("Failed to send message due to IO exception")),
                         "SignalError: Failed to send message due to IO exception")
        self.assertEqual(signal_failure(SignalError("")), "SignalError")
        self.assertEqual(signal_failure(TimeoutError("private words")), "TimeoutError")


class UnreachableTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_unreachable_contact_is_explained_before_any_approval(self):
        connection = MagicMock()
        connection.response.item.create = AsyncMock()
        connection.response.create = AsyncMock()
        confirm, send = AsyncMock(return_value=APPROVED), AsyncMock()
        tool = SendSignalMessageTool(
            lambda: connection, lambda: False, MagicMock(), lambda name: (SARAH, None), confirm, send,
            AsyncMock(return_value="Sarah Jones's number isn't on Signal"),
        )
        await tool.handle("c1", {"to": "Sarah", "message": "Hi", "task": None})
        confirm.assert_not_called()
        send.assert_not_called()
        output = json.loads(connection.response.item.create.call_args.kwargs["item"]["output"])
        self.assertEqual((output["status"], output["message"]), ("error", "Sarah Jones's number isn't on Signal"))

    async def test_the_app_asks_signal_whether_the_number_can_be_reached(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.signal.status = SignalStatus(LINKED, account=CORA)
            app.signal.client = MagicMock()
            app.signal.client.is_registered = AsyncMock(return_value=True)
            self.assertIsNone(await app.signal_contact_unreachable(SARAH))
            app.signal.client.is_registered.assert_awaited_once_with(CORA, SARAH.number)
            app.signal.client.is_registered = AsyncMock(return_value=False)
            problem = await app.signal_contact_unreachable(SARAH)
            self.assertIn("Sarah Jones's number isn't on Signal", problem)
            self.assertIn("hide their number", problem)
            # A check that fails doesn't block; the send itself will say.
            app.signal.client.is_registered = AsyncMock(side_effect=RuntimeError("down"))
            self.assertIsNone(await app.signal_contact_unreachable(SARAH))

    async def test_adding_someone_not_on_signal_still_adds_them_but_warns(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.signal.status = SignalStatus(LINKED, account=CORA)
            app.signal_settings = SignalSettings(True, CORA, NICK)
            app.signal.client = MagicMock()
            app.signal.client.is_registered = AsyncMock(return_value=False)
            app.persistence.add_client.return_value = SARAH
            from unittest.mock import patch
            with patch.object(app, "notify") as notify:
                await app.add_signal_client("Sarah Jones", SARAH.number).wait()
            self.assertIn(SARAH.number, app.signal_clients)
            self.assertIn("isn't on Signal", notify.call_args.args[0])
