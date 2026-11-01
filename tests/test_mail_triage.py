import app_areas.mail
"""Her other new mail (T4b-2): sorted by one cheap call; urgent now, the rest of note in one digest."""

from datetime import datetime, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock

from mail.account import READY, EmailStatus, InboxMail
from mail.triage import IGNORE, MENTION, TRIAGE_INSTRUCTIONS, URGENT, Sorted, digest_text, triage_mail
import test_keyboard_persistence as keyboard_tests

CORA, ME = "cora@intelligis.io", "nick@example.com"


def mail(id, sender="sarah@school.example", name="Sarah", subject="Lesson", text="Friday is off"):
    return InboxMail(id, f"thread-{id}", name, sender, subject, datetime(2026, 10, 31, 9, tzinfo=timezone.utc),
                     text, False, "")


def client(emails):
    response = SimpleNamespace(status="completed", output=[], output_text=json.dumps({"emails": emails}))
    api = MagicMock()
    api.responses.create.return_value = response
    return api


class TriageTests(unittest.TestCase):
    def test_each_mail_gets_a_verdict_and_one_left_out_is_mentioned(self):
        api = client([
            {"id": "0", "verdict": URGENT, "summary": "  Sarah:\n Friday's lesson is cancelled  "},
            {"id": "1", "verdict": IGNORE, "summary": ""},
        ])
        mails = [mail("a"), mail("b", sender="news@shop.example", name=""), mail("c", subject="Quote")]
        result = triage_mail(mails, model="small", client=api)
        self.assertEqual([(item.mail.id, item.verdict, item.summary) for item in result], [
            ("a", URGENT, "Sarah: Friday's lesson is cancelled"), ("b", IGNORE, ""), ("c", MENTION, "Quote"),
        ])
        kwargs = api.responses.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "small")
        self.assertNotIn("tools", kwargs)
        sent = json.loads(kwargs["input"])
        self.assertEqual([item["id"] for item in sent], ["0", "1", "2"])
        self.assertIn("untrusted", TRIAGE_INSTRUCTIONS)

    def test_a_failed_call_raises_so_the_mail_is_sorted_next_time(self):
        api = client([])
        api.responses.create.return_value.status = "incomplete"
        with self.assertRaises(ValueError):
            triage_mail([mail("a")], client=api)
        self.assertEqual(triage_mail([], client=api), [])

    def test_the_digest_is_a_line_each(self):
        one = Sorted(mail("a"), MENTION, "Asked for a quote")
        self.assertEqual(digest_text([one]), "An email came in that's worth knowing about:\n- Sarah: Asked for a quote")
        two = digest_text([one, Sorted(mail("b", name=""), MENTION, "Invoice")])
        self.assertTrue(two.startswith("2 emails came in"))
        self.assertIn("- sarah@school.example: Invoice", two)


class SortingAppTests(unittest.IsolatedAsyncioTestCase):
    async def ready(self, app, pilot, sorted_mail):
        for _ in range(2):
            await pilot.pause()
            await app.workers.wait_for_complete()
        app.email_status = EmailStatus(READY, address=CORA)
        app.email_person = ME
        app.mailbox = MagicMock()
        app.mailbox.mark_read = AsyncMock()
        app.persistence.load_models.return_value = {"utility": "small"}
        app.tell_person = AsyncMock()
        self.sorted_mail = sorted_mail

    async def settle(self, app, pilot):
        for _ in range(3):
            await pilot.pause()
            await app.workers.wait_for_complete()

    async def sort(self, app, mails):
        original = app_areas.mail.triage_mail
        app_areas.mail.triage_mail = MagicMock(return_value=self.sorted_mail)
        try:
            await app.sort_new_mail(mails)
        finally:
            sorter, app_areas.mail.triage_mail = app_areas.mail.triage_mail, original
        return sorter

    async def test_urgent_reaches_you_now_worth_mentioning_waits_and_the_rest_is_read(self):
        async with keyboard_tests.running_app() as (app, pilot):
            urgent, note, junk = mail("u"), mail("m"), mail("i")
            await self.ready(app, pilot, [Sorted(urgent, URGENT, "Friday is off"), Sorted(note, MENTION, "A quote"),
                                          Sorted(junk, IGNORE, "")])
            sorter = await self.sort(app, [urgent, note, junk, mail("x", sender=CORA), mail("y", sender=ME)])
            self.assertEqual([m.id for m in sorter.call_args.args[0]], ["u", "m", "i"])
            self.assertEqual(sorter.call_args.kwargs["model"], "small")
            app.tell_person.assert_awaited_once_with("An urgent email to me, from Sarah: Friday is off")
            self.assertEqual(sorted(c.args[0] for c in app.mailbox.mark_read.await_args_list), ["i", "u"])
            self.assertEqual(list(app.mail_digest), ["m"])
            # Already sorted: not sorted again.
            sorter = await self.sort(app, [urgent, note, junk])
            sorter.assert_not_called()

    async def test_a_failed_sort_leaves_the_mail_for_next_time(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.ready(app, pilot, [])
            original = app_areas.mail.triage_mail
            app_areas.mail.triage_mail = MagicMock(side_effect=ValueError("no"))
            try:
                await app.sort_new_mail([mail("a")])
            finally:
                app_areas.mail.triage_mail = original
            self.assertEqual(app.mail_sorted, set())
            app.mailbox.mark_read.assert_not_awaited()

    async def test_the_digest_goes_once_when_you_next_talk_and_its_mail_is_then_read(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.ready(app, pilot, [])
            app.mail_digest = {"m": Sorted(mail("m"), MENTION, "A quote"), "n": Sorted(mail("n"), MENTION, "Invoice")}
            app.person_is_talking()
            app.person_is_talking()  # still being told: not twice
            await self.settle(app, pilot)
            app.tell_person.assert_awaited_once()
            text = app.tell_person.await_args.args[0]
            self.assertTrue(text.startswith("2 emails came in"))
            await app.tell_person.await_args.kwargs["delivered"]()
            self.assertEqual(app.mail_digest, {})
            self.assertEqual(sorted(c.args[0] for c in app.mailbox.mark_read.await_args_list), ["m", "n"])
            self.assertFalse(app.mail_digest_sending)

    async def test_a_digest_that_couldnt_be_given_waits_for_next_time(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.ready(app, pilot, [])
            app.mail_digest = {"m": Sorted(mail("m"), MENTION, "A quote")}
            app.tell_person = AsyncMock(side_effect=RuntimeError("no way to reach them"))
            app.person_is_talking()
            await self.settle(app, pilot)
            self.assertEqual(list(app.mail_digest), ["m"])
            self.assertFalse(app.mail_digest_sending)
            app.mailbox.mark_read.assert_not_awaited()
            app.person_is_talking()
            await self.settle(app, pilot)
            self.assertEqual(app.tell_person.await_count, 2)


if __name__ == "__main__":
    unittest.main()
