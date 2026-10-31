"""A reply to a contact that would commit the person waits for their YES (T5a)."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from mail.account import MailMessage, READY, EmailStatus, SentMail
from messaging.commitment import COMMITMENT_INSTRUCTIONS, Commitment, check_commitment
from messaging.setup import LINKED, SignalStatus
from services.confirmation import APPROVED, DECLINED, TIMED_OUT
from session_config import client_chat_instructions, email_party_instructions
from storage.signal_settings_store import SignalSettings
from storage.task_store import EmailThread
import test_keyboard_persistence as keyboard_tests

CORA, NICK = "+61400000001", "+61400000002"
REPLY = "Great, Tuesday at 4 for $70 is confirmed. Cora"


def client(payload, status="completed"):
    api = MagicMock()
    api.responses.create.return_value = SimpleNamespace(status=status, output=[], output_text=json.dumps(payload))
    return api


class CheckTests(unittest.TestCase):
    def test_a_commitment_says_what_and_anything_else_is_free_to_go(self):
        api = client({"commits": True, "what": " confirms  Tuesday 4pm at $70 "})
        self.assertEqual(check_commitment(REPLY, brief="Times and price", client=api),
                         Commitment(True, "confirms Tuesday 4pm at $70"))
        data = json.loads(api.responses.create.call_args.kwargs["input"])
        self.assertEqual(data["reply"], REPLY)
        self.assertNotIn("tools", api.responses.create.call_args.kwargs)
        self.assertEqual(check_commitment("What times suit?", client=client({"commits": False, "what": "x"})),
                         Commitment(False, ""))
        self.assertIn("untrusted", COMMITMENT_INSTRUCTIONS)

    def test_a_check_that_fails_holds_the_reply(self):
        self.assertTrue(check_commitment(REPLY, client=client({}, status="incomplete")).commits)
        broken = MagicMock()
        broken.responses.create.side_effect = RuntimeError("down")
        self.assertTrue(check_commitment(REPLY, client=broken).commits)

    def test_her_contacts_are_told_she_never_commits_the_person(self):
        self.assertIn('Never commit "Nick"', client_chat_instructions("Sarah", "Nick"))
        self.assertIn('Never commit "Nick"', email_party_instructions("School", "a@b.example", "Nick"))


class HoldingAppTests(unittest.IsolatedAsyncioTestCase):
    async def ready(self, app, pilot, commits=True):
        for _ in range(2):
            await pilot.pause()
            await app.workers.wait_for_complete()
        app.live.person_name = "Nick"
        app.persistence.load_person_name.return_value = "Nick"
        app.persistence.load_models.return_value = {"utility": "small"}
        self.check = MagicMock(return_value=Commitment(commits, "confirms Tuesday 4pm at $70" if commits else ""))
        app.ask_person_approval = AsyncMock(return_value=APPROVED)
        app.tell_person = AsyncMock()

    async def test_only_a_reply_that_commits_you_is_asked_about(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.ready(app, pilot, commits=False)
            with patch("app.check_commitment", self.check):
                self.assertEqual(await app.hold_commitment("Sarah", "What suits?", "brief", "hi"), ("What suits?", None, ""))
            app.ask_person_approval.assert_not_awaited()
            self.assertEqual(self.check.call_args.kwargs["model"], "small")

    async def test_your_yes_sends_it_and_anything_else_sends_a_holding_line(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.ready(app, pilot)
            with patch("app.check_commitment", self.check):
                self.assertEqual(await app.hold_commitment("Sarah", REPLY, "brief", "hi"),
                                 (REPLY, APPROVED, "confirms Tuesday 4pm at $70"))
                shown = app.ask_person_approval.await_args.args[2]
                self.assertEqual(shown, {"to": "Sarah", "commits you": "confirms Tuesday 4pm at $70", "reply": REPLY})
                for outcome in (DECLINED, TIMED_OUT):
                    app.ask_person_approval.return_value = outcome
                    text, got, _ = await app.hold_commitment("School", REPLY, "brief", "hi", sign_off="\n\nCora")
                    self.assertEqual((text, got), ("Let me check with Nick and come back to you.\n\nCora", outcome))

    async def test_a_held_reply_on_signal_that_you_missed_is_raised_with_you(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.ready(app, pilot)
            app.ask_person_approval.return_value = TIMED_OUT
            sarah = MagicMock(id=uuid4())
            sarah.name = "Sarah"
            app.persistence.load_task_briefs.return_value = "[]"
            with patch("app.check_commitment", self.check):
                text = await app.review_client_reply(sarah, [], REPLY)
            self.assertEqual(text, "Let me check with Nick and come back to you.")
            app.tell_person.assert_awaited_once()
            self.assertIn("didn't send Sarah my reply that confirms Tuesday 4pm at $70",
                          app.tell_person.await_args.args[0])

    async def test_a_held_email_reply_sends_the_holding_line_and_the_task_waits_on_you(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.ready(app, pilot)
            app.ask_person_approval.return_value = DECLINED
            app.email_status = EmailStatus(READY, address="cora@intelligis.io")
            app.mailbox = MagicMock()
            app.mailbox.reply = AsyncMock(return_value=SentMail("r1", "c1"))
            app.mailbox.mark_read = AsyncMock()
            app.backend_chat.reply = AsyncMock(return_value=REPLY)
            app.task_needs_person = AsyncMock()
            sent = datetime(2026, 10, 30, 9, tzinfo=timezone.utc)
            thread = EmailThread(uuid4(), uuid4(), "Book a lesson", "info@school.example", "School", "Times", "c1", sent)
            new = [MailMessage("m2", "", "info@school.example", "Re", sent + timedelta(minutes=5), "Tuesday 4pm, $70?")]
            with patch("app.check_commitment", self.check):
                await app.answer_email_party(thread, "cora@intelligis.io", new, new)
            app.mailbox.reply.assert_awaited_once_with(
                "m2", "info@school.example", None, "Let me check with Nick and come back to you.\n\nCora")
            self.assertEqual(self.check.call_args.kwargs["their_message"], "Tuesday 4pm, $70?")
            app.task_needs_person.assert_awaited_once()
            self.assertIn("held back my reply to School", app.task_needs_person.await_args.args[2])


class AskingTests(unittest.IsolatedAsyncioTestCase):
    async def test_asked_on_signal_when_linked_and_lapsing_after_the_wait(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.signal.status = SignalStatus(LINKED, account=CORA)
            app.signal_settings = SignalSettings(True, CORA, NICK)
            app.signal.client = MagicMock()
            app.signal.client.send = AsyncMock()
            with patch("app.APPROVAL_SECONDS", 0.05):
                self.assertEqual(await app.ask_person_approval("a reply to Sarah", "that commits you", {"to": "Sarah"}),
                                 TIMED_OUT)
            self.assertIn("Reply YES", app.signal.client.send.await_args.args[2])
            asking = asyncio.create_task(app.ask_person_approval("a reply to Sarah", "that commits you", {}))
            await pilot.pause()
            app.signal_approvals[NICK].set_result(True)
            self.assertEqual(await asking, APPROVED)

    async def test_without_signal_the_desk_pop_up_asks(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.confirmations.ask = AsyncMock(return_value=DECLINED)
            self.assertEqual(await app.ask_person_approval("a reply to Sarah", "that commits you", {"to": "Sarah"}),
                             DECLINED)
            app.confirmations.ask.assert_awaited_once_with("tasks", "that commits you", {"to": "Sarah"},
                                                          title="a reply to Sarah")


if __name__ == "__main__":
    unittest.main()
