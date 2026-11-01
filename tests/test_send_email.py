import app_areas.tasks
"""Emailing from Cora's own address (E2): always approved first, optionally starting or joining a task."""

import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from mail import account
from mail.account import EmailAccount, EmailNotReady, Mailbox, SentMail, signed
from mail.formatting import email_fragment, email_html
from services.confirmation import APPROVED, DECLINED
from storage.task_store import EmailParty
from tools.send_email import SCHEMA, SendEmailTool, validate_arguments
import test_keyboard_persistence as keyboard_tests
from test_email import FakeClient, FakeResponse

SIGNATURE = "Cora (AI PA)\nIntelligis.io\nE: cora@intelligis.io"
EMAIL = {"to": "Info@School.example", "name": "Bright Driving School", "subject": "Lesson times",
         "message": "Hi, could you tell me your free lesson times next week? Cora", "task": None}
NEW_TASK = {"goal": "Book Sarah a driving lesson", "task_id": None,
            "brief": "Free lesson times next week and the price. You may say it's for a learner driver.", "deadline": None}


class SendTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_draft_is_made_so_its_thread_is_known_then_sent(self):
        client = FakeClient([FakeResponse(201, {"id": "m1", "conversationId": "c1"}), FakeResponse(202, None)])
        sent = await Mailbox(lambda: "tok", client).send("info@school.example", "School", "Times?", "Hi **there**")
        self.assertEqual(sent, SentMail("m1", "c1"))
        (method, url, body), (send_method, send_url, _) = client.sent
        self.assertEqual((method, send_method), ("POST", "POST"))
        self.assertTrue(url.endswith("/me/messages"))
        self.assertTrue(send_url.endswith("/me/messages/m1/send"))
        self.assertEqual(body["body"], {"contentType": "HTML", "content": email_html("Hi **there**")})
        self.assertIn("<strong>there</strong>", body["body"]["content"])
        self.assertEqual({key: body[key] for key in ("subject", "toRecipients")}, {
            "subject": "Times?",
            "toRecipients": [{"emailAddress": {"address": "info@school.example", "name": "School"}}],
        })


class FormattingTests(unittest.TestCase):
    def test_markdown_becomes_styled_html_and_line_breaks_stay(self):
        html = email_fragment("## Plan\n\n- **3 Apr**: fly\n- 8 Apr: Da Nang\n\n| Leg | Date |\n|---|---|\n| KL | 3 Apr |"
                              "\n\nThanks,\n" + SIGNATURE)
        for part in ("<h2 style=", "<li><strong>3 Apr</strong>: fly</li>", '<table style="border-collapse',
                     '<td style="border:1px solid', "Thanks,<br />\nCora (AI PA)<br />\nIntelligis.io"):
            self.assertIn(part, html)

    def test_nothing_she_writes_or_quotes_becomes_markup_or_an_unsafe_link(self):
        html = email_fragment('<script>x()</script> <a href="https://evil.example">a</a> [f](file:///c:/x) '
                              "[j](javascript:x()) [ok](https://shangri-la.com) [m](mailto:a@b.co)")
        self.assertNotIn("<script", html)
        self.assertNotIn('<a href="https://evil', html)
        self.assertNotIn("file:", html.replace("[f](file:///c:/x)", ""))
        self.assertNotIn('href="javascript', html)
        self.assertIn('<a href="https://shangri-la.com">ok</a>', html)
        self.assertIn('<a href="mailto:a@b.co">m</a>', html)


class SignatureTests(unittest.TestCase):
    def test_the_signature_goes_below_and_signs_for_her_when_it_starts_with_her_name(self):
        self.assertEqual(signed("Hi,\n\nAny times free?\n\nThanks,\nCora", SIGNATURE),
                         "Hi,\n\nAny times free?\n\nThanks,\n\n" + SIGNATURE)
        self.assertEqual(signed("Any times free?\n\n— Cora\n", SIGNATURE), "Any times free?\n\n" + SIGNATURE)
        # A signature without her name leaves her sign-off alone; no signature changes nothing.
        self.assertEqual(signed("Thanks,\nCora", "Intelligis.io"), "Thanks,\nCora\n\nIntelligis.io")
        self.assertEqual(signed("Cora", SIGNATURE), "Cora\n\n" + SIGNATURE)
        self.assertEqual(signed("Thanks,\nCora", ""), "Thanks,\nCora")

    def test_it_is_saved_tidied_checked_and_forgotten(self):
        saved = {}
        with patch.object(account.keyring, "set_password", lambda s, a, v: saved.update({a: v})), \
                patch.object(account.keyring, "get_password", lambda s, a: saved.get(a)), \
                patch.object(account.keyring, "delete_password", lambda s, a: saved.pop(a)):
            mail_account = EmailAccount()
            self.assertEqual(mail_account.save_signature("\r\n Cora (AI PA)  \r\nIntelligis.io\r\n\r\n"),
                             "Cora (AI PA)\nIntelligis.io")
            self.assertEqual(mail_account.signature(), "Cora (AI PA)\nIntelligis.io")
            with self.assertRaises(ValueError):
                mail_account.save_signature("x" * (account.SIGNATURE_MAX_CHARS + 1))
            self.assertEqual(mail_account.save_signature("  "), "")
            self.assertEqual(mail_account.signature(), "")


class ToolTests(unittest.IsolatedAsyncioTestCase):
    def tool(self, outcome=APPROVED, goal="An open task", mailbox=None, record=None, signature=""):
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
                             self.task_goal, self.record, get_signature=lambda: signature)

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

    async def test_a_new_email_is_signed_and_the_person_sees_the_signature_in_the_approval(self):
        tool = self.tool(signature=SIGNATURE, record=AsyncMock(return_value=uuid4()))
        await tool.handle("c1", {**EMAIL, "message": "Any times free?\n\nCora", "task": NEW_TASK})
        expected = "Any times free?\n\n" + SIGNATURE
        self.assertEqual(self.confirm.await_args.args[2]["message"], expected)
        self.assertEqual(self.mailbox.send.await_args.args[3], expected)

    async def test_a_reply_in_a_thread_goes_without_it(self):
        task_id = uuid4()
        mailbox = MagicMock()
        mailbox.address = AsyncMock(return_value="cora@intelligis.io")
        mailbox.reply_in_thread = AsyncMock(return_value=SentMail("r1", "thread-1"))
        tool = self.tool(mailbox=mailbox, signature=SIGNATURE)
        tool.find_thread = AsyncMock(return_value="thread-1")
        await tool.handle("c1", {**EMAIL, "subject": None, "message": "Tuesday works.\n\nCora",
                                 "task": {"goal": None, "task_id": str(task_id), "brief": "b", "deadline": None}})
        self.assertEqual(mailbox.reply_in_thread.await_args.args[3], "Tuesday works.\n\nCora")

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
            EMAIL["message"], "thread-1", deadline=None)
        self.assertIn(str(task_id), self.output()["message"])
        tool = self.tool(goal=None)
        await tool.handle("c1", {**EMAIL, "task": {"goal": None, "task_id": str(task_id), "brief": "b", "deadline": None}})
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
                "Goal", EmailParty("info@school.example", "School", "thread-1"), "Brief", "Emailed School: Hi", None)
            await app.record_email_task("x@y.example", None, None, task_id, "Brief", "Hi", "thread-2")
            app.persistence.add_task_party.assert_called_once_with(
                task_id, EmailParty("x@y.example", None, "thread-2"), "Brief", "Emailed x@y.example: Hi", None)

    async def test_offered_where_you_talk_with_her_and_to_the_coordinator_only_with_signal_approval(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.assertIn("send_email", [schema["name"] for schema in app.live.tool_schemas()])
            self.assertNotIn("send_email", app_areas.tasks.COORDINATOR_TOOLS)
            self.assertIn("send_email", app_areas.tasks.COORDINATOR_SEND_TOOLS)


if __name__ == "__main__":
    unittest.main()
