"""Emailing from Cora's own address (E2): always approved first, optionally starting or joining a task."""

import json
import unittest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from mail.account import EmailNotReady, Mailbox, SentMail
from services.confirmation import APPROVED, DECLINED
from storage.task_store import EmailParty
from tools.send_email import SCHEMA, SendEmailTool, validate_arguments
import test_keyboard_persistence as keyboard_tests
from test_email import FakeClient, FakeResponse

EMAIL = {"to": "Info@School.example", "name": "Bright Driving School", "subject": "Lesson times",
         "message": "Hi, could you tell me your free lesson times next week? Cora", "task": None}
NEW_TASK = {"goal": "Book Sarah a driving lesson", "task_id": None,
            "brief": "Free lesson times next week and the price. You may say it's for a learner driver."}


class SendTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_draft_is_made_so_its_thread_is_known_then_sent(self):
        client = FakeClient([FakeResponse(201, {"id": "m1", "conversationId": "c1"}), FakeResponse(202, None)])
        sent = await Mailbox(lambda: "tok", client).send("info@school.example", "School", "Times?", "Hi")
        self.assertEqual(sent, SentMail("m1", "c1"))
        (method, url, body), (send_method, send_url, _) = client.sent
        self.assertEqual((method, send_method), ("POST", "POST"))
        self.assertTrue(url.endswith("/me/messages"))
        self.assertTrue(send_url.endswith("/me/messages/m1/send"))
        self.assertEqual(body, {
            "subject": "Times?", "body": {"contentType": "Text", "content": "Hi"},
            "toRecipients": [{"emailAddress": {"address": "info@school.example", "name": "School"}}],
        })


class ToolTests(unittest.IsolatedAsyncioTestCase):
    def tool(self, outcome=APPROVED, goal="An open task", mailbox=None, record=None):
        self.connection = MagicMock()
        self.connection.response.item.create = AsyncMock()
        self.connection.response.create = AsyncMock()
        self.confirm = AsyncMock(return_value=outcome)
        if mailbox is None:
            mailbox = MagicMock()
            mailbox.address = AsyncMock(return_value="cora@intelligis.io")
            mailbox.send = AsyncMock(return_value=SentMail("m1", "thread-1"))
        self.mailbox = mailbox
        self.task_goal = AsyncMock(return_value=goal)
        self.record = record or AsyncMock(return_value=uuid4())
        return SendEmailTool(lambda: self.connection, lambda: False, MagicMock(), lambda: mailbox, self.confirm,
                             self.task_goal, self.record)

    def output(self):
        return json.loads(self.connection.response.item.create.call_args.kwargs["item"]["output"])

    def test_schema_is_strict_and_takes_one_plain_address_and_a_one_line_subject(self):
        self.assertTrue(SCHEMA["strict"])
        validate_arguments(EMAIL)
        validate_arguments({**EMAIL, "name": None, "task": NEW_TASK})
        for bad in ({**EMAIL, "to": "a@b.c, d@e.f"}, {**EMAIL, "to": "Bob <a@b.c>"}, {**EMAIL, "to": "nobody"},
                    {**EMAIL, "subject": "Hi\nBcc: x@y.z"}, {**EMAIL, "message": " "}, {**EMAIL, "name": " "},
                    {**EMAIL, "task": {**NEW_TASK, "task_id": str(uuid4())}}, {k: v for k, v in EMAIL.items() if k != "name"}):
            with self.subTest(arguments=bad), self.assertRaises(ValueError):
                validate_arguments(bad)

    async def test_the_person_approves_exactly_what_goes_then_it_is_sent(self):
        tool = self.tool()
        await tool.handle("c1", EMAIL)
        self.confirm.assert_awaited_once_with("email", "send_email", {
            "to": "Bright Driving School <info@school.example>", "subject": "Lesson times",
            "message": EMAIL["message"],
        }, title="Email")
        self.mailbox.send.assert_awaited_once_with(
            "info@school.example", "Bright Driving School", "Lesson times", EMAIL["message"])
        self.assertEqual(self.output()["status"], "ok")
        self.record.assert_not_awaited()

    async def test_declined_means_nothing_sent(self):
        tool = self.tool(outcome=DECLINED)
        await tool.handle("c1", EMAIL)
        self.mailbox.send.assert_not_awaited()
        self.assertEqual(self.output()["status"], "not_done")

    async def test_a_task_shows_its_brief_and_is_recorded_with_the_thread(self):
        task_id = uuid4()
        tool = self.tool(record=AsyncMock(return_value=task_id))
        await tool.handle("c1", {**EMAIL, "task": NEW_TASK})
        shown = self.confirm.await_args.args[2]
        self.assertEqual((shown["task"], shown["brief"]), (NEW_TASK["goal"], NEW_TASK["brief"]))
        self.record.assert_awaited_once_with(
            "info@school.example", "Bright Driving School", NEW_TASK["goal"], None, NEW_TASK["brief"],
            EMAIL["message"], "thread-1")
        self.assertIn(str(task_id), self.output()["message"])
        tool = self.tool(goal=None)
        await tool.handle("c1", {**EMAIL, "task": {"goal": None, "task_id": str(task_id), "brief": "b"}})
        self.confirm.assert_not_awaited()
        self.assertEqual(self.output()["status"], "error")

    async def test_without_email_or_signed_out_nobody_is_asked(self):
        tool = SendEmailTool(lambda: self.connection, lambda: False, MagicMock(), lambda: None, AsyncMock())
        self.connection = MagicMock()
        self.connection.response.item.create = AsyncMock()
        self.connection.response.create = AsyncMock()
        await tool.handle("c1", EMAIL)
        self.assertIn("set up", self.output()["message"])
        lapsed = MagicMock()
        lapsed.address = AsyncMock(side_effect=EmailNotReady("Cora's email sign-in has lapsed"))
        tool = self.tool(mailbox=lapsed)
        await tool.handle("c1", EMAIL)
        self.confirm.assert_not_awaited()
        self.assertEqual(self.output()["message"], "Cora's email sign-in has lapsed")

    async def test_a_failed_send_never_claims_it_arrived(self):
        mailbox = MagicMock()
        mailbox.address = AsyncMock(return_value="cora@intelligis.io")
        mailbox.send = AsyncMock(side_effect=RuntimeError("private"))
        tool = self.tool(mailbox=mailbox)
        await tool.handle("c1", EMAIL)
        self.assertIn("don't assume it arrived", self.output()["message"])


class AppTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_emailed_party_is_saved_with_its_thread(self):
        async with keyboard_tests.running_app() as (app, pilot):
            task_id = uuid4()
            app.persistence.start_task.return_value = task_id
            self.assertEqual(await app.record_email_task(
                "info@school.example", "School", "Goal", None, "Brief", "Hi", "thread-1"), task_id)
            app.persistence.start_task.assert_called_once_with(
                "Goal", EmailParty("info@school.example", "School", "thread-1"), "Brief", "Emailed School: Hi")
            await app.record_email_task("x@y.example", None, None, task_id, "Brief", "Hi", "thread-2")
            app.persistence.add_task_party.assert_called_once_with(
                task_id, EmailParty("x@y.example", None, "thread-2"), "Brief", "Emailed x@y.example: Hi")

    async def test_offered_where_you_talk_with_her_and_to_the_coordinator_only_with_signal_approval(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.assertIn("send_email", [schema["name"] for schema in app.live.tool_schemas()])
            import app as app_module
            self.assertNotIn("send_email", app_module.COORDINATOR_TOOLS)
            self.assertIn("send_email", app_module.COORDINATOR_SEND_TOOLS)


if __name__ == "__main__":
    unittest.main()
