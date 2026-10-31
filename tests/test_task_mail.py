"""Replies in a task's email thread (E3a): read every few minutes, answered within the brief, reported through pass_on."""

from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from backend_chat import BackendRoute
from mail.account import READY, Bounce, EmailNotReady, EmailStatus, MailMessage, Mailbox, SentMail
from session_config import NO_REPLY, email_party_instructions
from storage.task_store import EmailThread
from tools.pass_on import PassOnTool
from tools.send_email import SendEmailTool, validate_arguments
import test_keyboard_persistence as keyboard_tests
from test_email import FakeClient, FakeResponse

CORA = "cora@intelligis.io"
SCHOOL = "info@school.example"
SENT_AT = datetime(2026, 10, 30, 9, tzinfo=timezone.utc)


def mail(id, sender, minutes, text="Hi", name=""):
    return MailMessage(id, name, sender, "Lesson times", SENT_AT + timedelta(minutes=minutes), text)


def graph_message(id, sender, minutes, draft=False):
    return {"id": id, "from": {"emailAddress": {"name": "", "address": sender}}, "subject": "Lesson times",
            "receivedDateTime": (SENT_AT + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z"),
            "uniqueBody": {"content": f" text {id} "}, "isDraft": draft}


class MailboxTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_thread_is_its_own_new_text_oldest_first_without_drafts(self):
        client = FakeClient([FakeResponse(200, {"value": [
            graph_message("b", "INFO@school.example", 30), graph_message("a", CORA, 0),
            graph_message("d", CORA, 40, draft=True),
        ]})])
        messages = await Mailbox(lambda: "tok", client).thread("it's-1")
        self.assertEqual([(m.id, m.sender_address, m.text) for m in messages],
                         [("a", CORA, "text a"), ("b", SCHOOL, "text b")])
        url, params, headers = client.calls[0]
        self.assertTrue(url.endswith("/me/messages"))
        self.assertEqual(params["$filter"], "conversationId eq 'it''s-1'")
        self.assertIn("uniqueBody", params["$select"])
        self.assertEqual(headers["Prefer"], 'outlook.body-content-type="text"')

    async def test_a_reply_goes_to_one_address_in_plain_text_in_the_thread(self):
        client = FakeClient([FakeResponse(201, {"id": "r1", "conversationId": "c1"}), FakeResponse(200, {}),
                             FakeResponse(202, None)])
        sent = await Mailbox(lambda: "tok", client).reply("m1", SCHOOL, "School", "Tuesday works")
        self.assertEqual(sent, SentMail("r1", "c1"))
        (create, create_url, _), (patch, patch_url, body), (send, send_url, _) = client.sent
        self.assertEqual((create, patch, send), ("POST", "PATCH", "POST"))
        self.assertTrue(create_url.endswith("/me/messages/m1/createReply"))
        self.assertTrue(patch_url.endswith("/me/messages/r1") and send_url.endswith("/me/messages/r1/send"))
        self.assertEqual(body, {
            "body": {"contentType": "Text", "content": "Tuesday works"},
            "toRecipients": [{"emailAddress": {"address": SCHOOL, "name": "School"}}],
            "ccRecipients": [], "bccRecipients": [],
        })

    async def test_her_text_goes_above_the_quoted_mail_and_never_becomes_markup(self):
        quoted = '<html><head></head><body dir="ltr"><div id="quote">From: Nick</div></body></html>'
        client = FakeClient([FakeResponse(201, {"id": "r1", "conversationId": "c1",
                                                "body": {"contentType": "html", "content": quoted}}),
                             FakeResponse(200, {}), FakeResponse(202, None)])
        await Mailbox(lambda: "tok", client).reply("m1", SCHOOL, None, "Hi Nick,\n\nTuesday <b>works</b>.\nCora")
        body = client.sent[1][2]["body"]
        self.assertEqual(body, {"contentType": "HTML", "content": (
            '<html><head></head><body dir="ltr"><div>Hi Nick,<br><br>Tuesday &lt;b&gt;works&lt;/b&gt;.<br>Cora</div>'
            '<br><div id="quote">From: Nick</div></body></html>'
        )})

    async def test_bounces_are_inbox_failure_reports_since_she_wrote(self):
        def report(id, subject):
            return {"id": id, "conversationId": "c1", "subject": subject, "receivedDateTime": "2026-10-30T10:00:00Z",
                    "body": {"content": f"Couldn't deliver to {SCHOOL}"}}
        client = FakeClient([FakeResponse(200, {"value": [
            report("n1", "Undeliverable: Lesson times"), report("n2", "Delivery Status Notification (Failure)"),
            report("m1", "RE: Undeliverable times?"), report("d1", "Delivery delayed: Lesson times"),
        ]})])
        bounces = await Mailbox(lambda: "tok", client).bounces(SENT_AT)
        self.assertEqual([bounce.id for bounce in bounces], ["n1", "n2"])
        self.assertTrue(bounces[0].names(SCHOOL) and not bounces[0].names("o@school.example"))
        url, params, headers = client.calls[0]
        self.assertTrue(url.endswith("/me/mailFolders/inbox/messages"))
        self.assertEqual(params["$filter"], "receivedDateTime ge 2026-10-30T09:00:00Z")
        self.assertEqual(headers["Prefer"], 'outlook.body-content-type="text"')

    async def test_replying_in_a_thread_that_is_gone_says_so(self):
        client = FakeClient([FakeResponse(200, {"value": []})])
        with self.assertRaises(LookupError):
            await Mailbox(lambda: "tok", client).reply_in_thread("c1", SCHOOL, None, "Hi")


class PassOnFromEmailTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_email_thread_reports_as_its_party(self):
        party_id, task_id = uuid4(), uuid4()
        relay, relay_email = AsyncMock(return_value=None), AsyncMock(return_value=None)
        route = BackendRoute(email_party=(party_id, task_id, "Bright School"))
        tool = PassOnTool(lambda: route, lambda: False, MagicMock(), relay, relay_email)
        await tool.handle("c1", {"message": "Tuesdays at 4", "task_id": str(task_id), "settled": True})
        relay_email.assert_awaited_once_with(party_id, task_id, "Bright School", "Tuesdays at 4", task_id, True)
        relay.assert_not_awaited()
        self.assertEqual(json.loads(route.outputs["c1"])["status"], "ok")


class SendInThreadTests(unittest.IsolatedAsyncioTestCase):
    REPLY = {"to": SCHOOL, "name": "Bright School", "subject": None, "message": "Nick says Tuesday is fine. Cora",
             "task": {"goal": None, "task_id": None, "brief": "Confirm Tuesday at 4."}}

    def tool(self, thread_id="c1"):
        self.route = BackendRoute()
        self.mailbox = MagicMock()
        self.mailbox.address = AsyncMock(return_value=CORA)
        self.mailbox.reply_in_thread = AsyncMock(return_value=SentMail("r1", "c1"))
        self.mailbox.send = AsyncMock()
        self.confirm = AsyncMock(return_value="approved")
        self.record = AsyncMock(return_value=self.task_id)
        self.find_thread = AsyncMock(return_value=thread_id)
        return SendEmailTool(lambda: self.route, lambda: False, MagicMock(), lambda: self.mailbox, self.confirm,
                             AsyncMock(return_value="Book a lesson"), self.record, self.find_thread)

    def setUp(self):
        self.task_id = uuid4()
        self.arguments = {**self.REPLY, "task": {**self.REPLY["task"], "task_id": str(self.task_id)}}

    def test_no_subject_only_for_an_open_tasks_thread(self):
        validate_arguments(self.arguments)
        for bad in ({**self.REPLY, "task": None},
                    {**self.REPLY, "task": {"goal": "New", "task_id": None, "brief": "b"}}):
            with self.subTest(arguments=bad), self.assertRaises(ValueError):
                validate_arguments(bad)

    async def test_it_goes_as_an_approved_reply_in_her_thread_with_them(self):
        tool = self.tool()
        await tool.handle("c1", self.arguments)
        self.find_thread.assert_awaited_once_with(self.task_id, SCHOOL)
        self.assertEqual(self.confirm.await_args.args[2]["subject"], "(a reply in your thread with them)")
        self.mailbox.reply_in_thread.assert_awaited_once_with("c1", SCHOOL, "Bright School", self.arguments["message"])
        self.mailbox.send.assert_not_awaited()
        self.record.assert_awaited_once_with(SCHOOL, "Bright School", None, self.task_id, "Confirm Tuesday at 4.",
                                             self.arguments["message"], "c1")

    async def test_without_a_thread_nobody_is_asked(self):
        tool = self.tool(thread_id=None)
        await tool.handle("c1", self.arguments)
        self.confirm.assert_not_awaited()
        self.assertIn("give a subject", json.loads(self.route.outputs["c1"])["message"])


class MailCheckAppTests(unittest.IsolatedAsyncioTestCase):
    def thread(self, **changes):
        values = dict(party_id=uuid4(), task_id=uuid4(), goal="Book a lesson", address=SCHOOL, name="Bright School",
                      brief="Times next week and the price.", thread_id="c1", since=SENT_AT)
        return EmailThread(**(values | changes))

    async def ready(self, app, pilot, messages, reply="Thanks! Tuesday at 4 sounds good; I'll confirm. Cora"):
        for _ in range(2):
            await pilot.pause()
            await app.workers.wait_for_complete()
        app.email_status = EmailStatus(READY, address=CORA)
        app.mailbox = MagicMock()
        app.mailbox.thread = AsyncMock(return_value=messages)
        app.mailbox.reply = AsyncMock(return_value=SentMail("r1", "c1"))
        app.mailbox.mark_read = AsyncMock()
        app.mailbox.bounces = AsyncMock(return_value=[])
        app.mailbox.unread = AsyncMock(return_value=[])
        app.backend_chat.reply = AsyncMock(return_value=reply) if isinstance(reply, str) else reply
        app.task_needs_person = AsyncMock()

    async def test_new_mail_from_their_end_is_answered_within_the_brief_and_noted(self):
        async with keyboard_tests.running_app() as (app, pilot):
            thread = self.thread()
            old, mine = mail("m0", SCHOOL, -10, "Earlier"), mail("m1", CORA, 0, "Hi, lesson times? Cora")
            new = [mail("m2", SCHOOL, 30, "Tuesday 4pm, $70", "Bright School"), mail("m3", "jo@school.example", 31, "Or Weds")]
            await self.ready(app, pilot, [old, mine, *new])
            app.persistence.load_email_threads.return_value = [thread]
            await app.read_task_mail()
            kwargs = app.backend_chat.reply.await_args.kwargs
            # Her own self and only this brief: nothing of the person's.
            self.assertEqual(json.loads(kwargs["sections"]["tasks"][kwargs["sections"]["tasks"].index("["):]),
                             [{"task_id": str(thread.task_id), "brief": thread.brief}])
            self.assertNotIn("memories", kwargs["sections"])
            self.assertEqual(frozenset(app.tools.names()) - kwargs["route"].excluded, {"pass_on"})
            self.assertEqual(kwargs["route"].email_party, (thread.party_id, thread.task_id, "Bright School"))
            note = kwargs["content"][0]["text"]
            self.assertEqual([m["text"] for m in json.loads(note[note.index("["):])], ["Tuesday 4pm, $70", "Or Weds"])
            self.assertEqual([item["role"] for item in kwargs["history"]], ["user", "assistant"])
            # To whoever wrote last, in the thread.
            app.mailbox.reply.assert_awaited_once_with(
                "m3", "jo@school.example", None, "Thanks! Tuesday at 4 sounds good; I'll confirm. Cora")
            app.persistence.email_party_heard.assert_called_once_with(thread.party_id, new[-1].received_at)
            self.assertEqual([c.args[0] for c in app.mailbox.mark_read.await_args_list], ["m2", "m3"])
            app.task_needs_person.assert_not_awaited()

    async def test_nothing_new_from_them_means_no_turn(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.ready(app, pilot, [mail("m0", SCHOOL, -10), mail("m1", CORA, 5)])
            app.persistence.load_email_threads.return_value = [self.thread()]
            await app.read_task_mail()
            app.backend_chat.reply.assert_not_awaited()
            app.persistence.email_party_heard.assert_not_called()

    async def test_mail_needing_no_answer_is_read_but_not_answered(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.ready(app, pilot, [mail("m2", SCHOOL, 30, "Out of office")], reply=NO_REPLY)
            app.persistence.load_email_threads.return_value = [self.thread()]
            await app.read_task_mail()
            app.mailbox.reply.assert_not_awaited()
            app.persistence.email_party_heard.assert_called_once()

    async def test_a_failed_answer_goes_back_to_the_person_and_isnt_retried(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.ready(app, pilot, [mail("m2", SCHOOL, 30)], reply=AsyncMock(side_effect=RuntimeError("private")))
            thread = self.thread()
            app.persistence.load_email_threads.return_value = [thread]
            await app.read_task_mail()
            app.persistence.email_party_heard.assert_called_once()
            app.task_needs_person.assert_awaited_once()
            self.assertIn("couldn't answer", app.task_needs_person.await_args.args[2])

    async def test_she_stops_answering_one_thread_after_a_days_replies(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.ready(app, pilot, [mail("m2", SCHOOL, 30)])
            thread = self.thread()
            app.persistence.load_email_threads.return_value = [thread]
            app.email_replies[(thread.party_id, datetime.now().date())] = app_module_limit()
            await app.read_task_mail()
            app.backend_chat.reply.assert_not_awaited()
            app.task_needs_person.assert_awaited_once()

    async def test_a_lapsed_sign_in_stops_the_check(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.ready(app, pilot, [])
            app.mailbox.thread = AsyncMock(side_effect=EmailNotReady("lapsed"))
            app.persistence.load_email_threads.return_value = [self.thread(), self.thread()]
            await app.read_task_mail()
            self.assertEqual(app.mailbox.thread.await_count, 1)

    async def test_only_checked_while_her_email_is_ready(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.ready(app, pilot, [])
            app.email_status = EmailStatus()
            app.check_task_mail()
            self.assertIsNone(app.task_mail_worker)

    async def test_their_word_reaches_the_coordinator_only_for_their_own_task(self):
        async with keyboard_tests.running_app() as (app, pilot):
            party_id, task_id = uuid4(), uuid4()
            app.coordinate_task = MagicMock()
            app.persistence.report_email_task.return_value = "Book a lesson"
            self.assertIn("isn't the task", await app.relay_from_email(
                party_id, task_id, "Bright School", "Tuesdays", uuid4(), True))
            self.assertIsNone(await app.relay_from_email(party_id, task_id, "Bright School", "Tuesdays", task_id, True))
            app.persistence.report_email_task.assert_called_once_with(task_id, party_id, "Tuesdays", True)
            news, fallback, settled = app.coordinate_task.call_args.args[1:]
            self.assertEqual(news, "Bright School answered: Tuesdays")
            self.assertIn("by email, via Cora", fallback)

    async def test_a_bounce_is_never_answered_and_brings_the_task_back_to_the_person(self):
        async with keyboard_tests.running_app() as (app, pilot):
            bounce = MailMessage("n1", "Microsoft Outlook", "postmaster@outlook.com", "Undeliverable: Lesson times",
                                 SENT_AT + timedelta(minutes=1), "Your message couldn't be delivered.")
            await self.ready(app, pilot, [mail("m1", CORA, 0), bounce])
            app.mailbox.bounces = AsyncMock(return_value=[Bounce("n1", "c1", bounce.received_at, bounce.text)])
            thread = self.thread(contacted_at=SENT_AT)
            app.persistence.load_email_threads.return_value = [thread]
            await app.read_task_mail()
            app.backend_chat.reply.assert_not_awaited()
            app.mailbox.bounces.assert_awaited_once_with(SENT_AT)
            app.persistence.task_email_bounced.assert_called_once_with(thread.party_id, "My email to Bright School bounced.")
            app.mailbox.mark_read.assert_awaited_once_with("n1")
            app.task_needs_person.assert_awaited_once()
            self.assertIn("bounced", app.task_needs_person.await_args.args[2])

    async def test_a_bounce_elsewhere_counts_only_if_it_names_them_and_came_after_she_wrote(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.ready(app, pilot, [])
            app.mailbox.bounces = AsyncMock(return_value=[
                Bounce("n1", "other", SENT_AT - timedelta(minutes=1), f"Couldn't deliver to {SCHOOL}."),
                Bounce("n2", "other", SENT_AT + timedelta(minutes=1), "Couldn't deliver to my-info@school.example."),
            ])
            app.persistence.load_email_threads.return_value = [
                self.thread(contacted_at=SENT_AT), self.thread(contacted_at=SENT_AT, waiting=False)]
            await app.read_task_mail()
            app.persistence.task_email_bounced.assert_not_called()
            app.mailbox.bounces.return_value.append(
                Bounce("n3", "other", SENT_AT + timedelta(minutes=2), f"Your message to {SCHOOL.upper()}. failed"))
            await app.read_task_mail()
            app.persistence.task_email_bounced.assert_called_once()
            app.mailbox.mark_read.assert_awaited_once_with("n3")


class EmailFollowUpTests(unittest.IsolatedAsyncioTestCase):
    thread = MailCheckAppTests.thread
    ready = MailCheckAppTests.ready

    async def test_a_quiet_party_gets_one_short_follow_up_in_their_thread(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.ready(app, pilot, [mail("m1", CORA, 0, "Hi, lesson times? Cora")],
                             reply="Just following up on lesson times. Cora")
            thread = self.thread()
            await app.nudge_email_party(thread)
            kwargs = app.backend_chat.reply.await_args.kwargs
            self.assertIn(str(thread.task_id), kwargs["content"][0]["text"])
            self.assertEqual(kwargs["route"].excluded, frozenset(app.tools.names()))
            self.assertEqual([item["role"] for item in kwargs["history"]], ["assistant"])
            app.mailbox.reply.assert_awaited_once_with("m1", SCHOOL, "Bright School", "Just following up on lesson times. Cora")
            app.persistence.record_task_nudge.assert_called_once_with(
                thread.party_id, "Followed up with Bright School by email: Just following up on lesson times. Cora")

    async def test_no_follow_up_over_an_unread_reply_or_when_she_has_nothing_to_say(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.ready(app, pilot, [mail("m1", CORA, 0), mail("m2", SCHOOL, 30)])
            await app.nudge_email_party(self.thread())
            app.backend_chat.reply.assert_not_awaited()
            await self.ready(app, pilot, [mail("m1", CORA, 0)], reply=NO_REPLY)
            await app.nudge_email_party(self.thread())
            app.mailbox.reply.assert_not_awaited()
            app.persistence.record_task_nudge.assert_not_called()

    async def test_the_hourly_round_follows_up_by_email_and_after_two_comes_back_to_you(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.ready(app, pilot, [])
            app.nudge_email_party = AsyncMock()
            quiet, ignored = self.thread(), self.thread(nudges=2)
            app.persistence.load_task_checks.return_value = ([], [], [], [quiet, ignored])
            with patch.object(app_module(), "NUDGE_HOURS", range(0, 24)):
                app.check_tasks()
                for _ in range(3):
                    await pilot.pause()
                    await app.workers.wait_for_complete()
            if datetime.now().astimezone().weekday() == 6:
                app.nudge_email_party.assert_not_awaited()
            else:
                app.nudge_email_party.assert_awaited_once_with(quiet)
            app.task_needs_person.assert_awaited_once()
            self.assertIn("even after 2 follow-ups", app.task_needs_person.await_args.args[2])


class InstructionTests(unittest.TestCase):
    def test_the_thread_knows_who_its_with_and_how_to_say_nothing(self):
        text = email_party_instructions("Bright School", SCHOOL, "Nick")
        self.assertIn('"Bright School" (info@school.example)', text)
        self.assertIn(NO_REPLY, text)
        self.assertIn("untrusted", text)


def app_module():
    import app
    return app


def app_module_limit():
    return app_module().EMAIL_REPLIES_PER_DAY


if __name__ == "__main__":
    unittest.main()
