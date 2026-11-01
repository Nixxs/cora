"""Emailing Cora yourself (T4b): verified mail from your own address is your chat, answered by email."""

from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from mail import account
from mail.account import READY, EmailAccount, EmailStatus, InboxMail, Mailbox, SentMail, verified_from
from session_config import EMAIL_CHAT_INSTRUCTIONS, NO_REPLY
import test_keyboard_persistence as keyboard_tests
from test_email import FakeClient, FakeResponse

ME = "nick@example.com"
VERIFIED = ("spf=pass (sender IP is 1.2.3.4) smtp.mailfrom=example.com; dkim=pass (signature was verified) "
            "header.d=example.com;dmarc=pass action=none header.from=example.com;compauth=pass reason=100")


def inbox(id="m1", sender=ME, authentication=VERIFIED, thread="t1", minutes_ago=0, attachments=False,
          subject="Book dinner", text="Can you book dinner for Friday?"):
    return InboxMail(id, thread, "Nick", sender, subject,
                     datetime.now(timezone.utc) - timedelta(minutes=minutes_ago), text, attachments, authentication)


class VerificationTests(unittest.TestCase):
    def test_only_their_address_with_dmarc_passing_for_its_domain_counts(self):
        self.assertTrue(verified_from(inbox(), ME))
        self.assertTrue(verified_from(inbox(), " Nick@Example.com "))
        for mail in (
            inbox(sender="someone@example.com"),
            inbox(authentication=VERIFIED.replace("dmarc=pass", "dmarc=fail")),
            inbox(authentication=VERIFIED.replace("dmarc=pass", "dmarc=none")),
            inbox(authentication=VERIFIED.replace("header.from=example.com", "header.from=evil.example")),
            inbox(authentication=""),
        ):
            with self.subTest(mail=mail.authentication[:40] or mail.sender_address):
                self.assertFalse(verified_from(mail, ME))
        self.assertFalse(verified_from(inbox(), ""))

    def test_their_address_is_saved_checked_and_forgotten(self):
        saved = {}
        with patch.object(account.keyring, "set_password", lambda s, a, v: saved.update({a: v})), \
                patch.object(account.keyring, "get_password", lambda s, a: saved.get(a)), \
                patch.object(account.keyring, "delete_password", lambda s, a: saved.pop(a)):
            mail_account = EmailAccount()
            self.assertEqual(mail_account.save_person_address(" Nick@Example.com "), ME)
            self.assertEqual(mail_account.person_address(), ME)
            for bad in ("nick", "a@b.c, d@e.f", "Nick <nick@example.com>"):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    mail_account.save_person_address(bad)
            self.assertEqual(mail_account.save_person_address(""), "")
            self.assertEqual(mail_account.person_address(), "")


class UnreadTests(unittest.IsolatedAsyncioTestCase):
    async def test_unread_inbox_mail_comes_with_microsofts_own_authentication_results(self):
        client = FakeClient([FakeResponse(200, {"value": [{
            "id": "m1", "conversationId": "c1", "subject": "Hi", "receivedDateTime": "2026-10-31T01:00:00Z",
            "from": {"emailAddress": {"name": "Nick", "address": "Nick@Example.com"}},
            "uniqueBody": {"content": " Do this "}, "hasAttachments": True,
            "internetMessageHeaders": [{"name": "Received", "value": "x"},
                                       {"name": "Authentication-Results", "value": "first"},
                                       {"name": "Authentication-Results", "value": "forwarded"}],
        }]})])
        [mail] = await Mailbox(lambda: "tok", client).unread(datetime(2026, 10, 30, 9, tzinfo=timezone.utc))
        self.assertEqual((mail.sender_address, mail.thread_id, mail.text, mail.has_attachments, mail.authentication),
                         (ME, "c1", "Do this", True, "first"))
        url, params, headers = client.calls[0]
        self.assertTrue(url.endswith("/me/mailFolders/inbox/messages"))
        self.assertEqual(params["$filter"], "receivedDateTime ge 2026-10-30T09:00:00Z and isRead eq false")
        self.assertIn("internetMessageHeaders", params["$select"])


class PersonEmailAppTests(unittest.IsolatedAsyncioTestCase):
    async def ready(self, app, pilot, mails, reply="Booked for 7pm Friday.\n\nCora"):
        for _ in range(2):
            await pilot.pause()
            await app.workers.wait_for_complete()
        app.email_status = EmailStatus(READY, address="cora@intelligis.io")
        app.email_person = ME
        app.mailbox = MagicMock()
        app.mailbox.unread = AsyncMock(return_value=mails)
        app.mailbox.mark_read = AsyncMock()
        app.mailbox.reply = AsyncMock(return_value=SentMail("r1", "t1"))
        app.reply_in_writing = AsyncMock(return_value=reply) if isinstance(reply, str) else reply
        app.tell_person = AsyncMock()
        app.live.note_signal_exchange = AsyncMock()

    async def settle(self, app, pilot):
        for _ in range(3):
            await pilot.pause()
            await app.workers.wait_for_complete()

    async def test_your_verified_email_is_your_chat_and_answered_by_email(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.ready(app, pilot, [inbox()])
            await app.read_person_mail(app.mailbox.unread.return_value)
            await self.settle(app, pilot)
            app.mailbox.mark_read.assert_awaited_once_with("m1")
            content, history, route, instructions = app.reply_in_writing.await_args.args
            self.assertEqual(content[0]["text"], "Subject: Book dinner\n\nCan you book dinner for Friday?")
            self.assertEqual(instructions, EMAIL_CHAT_INSTRUCTIONS)
            self.assertIn("send_email", app.tools.names() - route.excluded)
            app.mailbox.reply.assert_awaited_once_with("m1", ME, "Nick", "Booked for 7pm Friday.\n\nCora")
            app.live.note_signal_exchange.assert_awaited_once()
            self.assertEqual(app.live.note_signal_exchange.await_args.args[2], "emailed you")
            history = app.conversation.get_reconnect_history()
            self.assertEqual([item["role"] for item in history[-2:]], ["user", "assistant"])
            app.tell_person.assert_not_awaited()

    async def test_mail_only_claiming_to_be_yours_is_never_acted_on_and_you_hear_once(self):
        async with keyboard_tests.running_app() as (app, pilot):
            forged = inbox(authentication=VERIFIED.replace("dmarc=pass", "dmarc=fail"))
            await self.ready(app, pilot, [forged])
            await app.read_person_mail(app.mailbox.unread.return_value)
            await self.settle(app, pilot)
            app.reply_in_writing.assert_not_awaited()
            app.mailbox.reply.assert_not_awaited()
            app.mailbox.mark_read.assert_awaited_once_with("m1")
            app.tell_person.assert_awaited_once()

    async def test_others_mail_old_mail_and_no_address_are_left_alone(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.ready(app, pilot, [inbox("a", sender="someone@example.com"), inbox("b", minutes_ago=60 * 30)])
            await app.read_person_mail(app.mailbox.unread.return_value)
            app.email_person = ""
            await app.read_person_mail([inbox()])
            await self.settle(app, pilot)
            app.mailbox.mark_read.assert_not_awaited()
            app.reply_in_writing.assert_not_awaited()

    async def test_the_mail_check_keeps_task_threads_and_bounces_out_of_your_mail(self):
        async with keyboard_tests.running_app() as (app, pilot):
            mails = [inbox("a"), inbox("b", thread="task-thread"), inbox("c", subject="Undeliverable: hi")]
            await self.ready(app, pilot, mails)
            app.persistence.load_email_threads.return_value = [MagicMock(thread_id="task-thread", waiting=False)]
            app.mailbox.thread = AsyncMock(return_value=[])
            app.read_person_mail = AsyncMock()
            app.sort_new_mail = AsyncMock()
            await app.read_task_mail()
            self.assertEqual([mail.id for mail in app.read_person_mail.await_args.args[0]], ["a"])
            self.assertEqual([mail.id for mail in app.sort_new_mail.await_args.args[0]], ["a"])

    async def test_nothing_to_say_sends_nothing_and_a_failed_turn_says_sorry(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.ready(app, pilot, [inbox()], reply=NO_REPLY)
            await app.read_person_mail(app.mailbox.unread.return_value)
            await self.settle(app, pilot)
            app.mailbox.reply.assert_not_awaited()
            await self.ready(app, pilot, [inbox()], reply=AsyncMock(side_effect=RuntimeError("private")))
            await app.read_person_mail(app.mailbox.unread.return_value)
            await self.settle(app, pilot)
            app.mailbox.reply.assert_awaited_once()
            self.assertIn("Sorry", app.mailbox.reply.await_args.args[3])

    def test_a_late_email_says_when_it_was_sent_and_attachments_stay_closed(self):
        from app_areas.mail import person_email_text
        text = person_email_text(inbox(minutes_ago=90, attachments=True), datetime.now(timezone.utc))
        self.assertTrue(text.startswith("[sent "))
        self.assertIn("1 hour ago", text)
        self.assertTrue(text.endswith("[attachments, not opened]"))


if __name__ == "__main__":
    unittest.main()
