"""Task deadlines (T5b): taken from the person's request, told once a day before and once when passed."""

from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from services.confirmation import APPROVED
from storage.client_store import Client
from tools.send_signal_message import SendSignalMessageTool, parse_deadline
import test_keyboard_persistence as keyboard_tests

SARAH = Client(uuid4(), "Sarah", "+61400000003")
TOMORROW = (datetime.now() + timedelta(days=1)).date().isoformat()


class ParseTests(unittest.TestCase):
    def test_a_day_means_by_the_end_of_it_and_a_time_is_kept_in_local_time(self):
        now = datetime(2026, 10, 31, 9).astimezone()
        self.assertEqual(parse_deadline("2026-11-06", now), datetime(2026, 11, 6, 23, 59).astimezone())
        self.assertEqual(parse_deadline(" 2026-11-06T17:00 ", now), datetime(2026, 11, 6, 17).astimezone())
        for bad in ("Friday", "06/11/2026", "2026-13-01"):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
                parse_deadline(bad, now)
        with self.assertRaisesRegex(ValueError, "already passed"):
            parse_deadline("2026-10-30", now)


class ToolTests(unittest.IsolatedAsyncioTestCase):
    def tool(self):
        self.connection = MagicMock()
        self.connection.response.item.create = AsyncMock()
        self.connection.response.create = AsyncMock()
        self.confirm = AsyncMock(return_value=APPROVED)
        self.record = AsyncMock(return_value=uuid4())
        return SendSignalMessageTool(
            lambda: self.connection, lambda: False, MagicMock(), lambda name: (SARAH, None), self.confirm,
            AsyncMock(), task_goal=AsyncMock(return_value="Goal"), record_task=self.record,
        )

    def output(self):
        return json.loads(self.connection.response.item.create.call_args.kwargs["item"]["output"])

    async def test_the_deadline_is_approved_with_the_task_and_saved_with_it(self):
        tool = self.tool()
        task = {"goal": "Lesson for Sarah", "task_id": None, "brief": "When suits her", "deadline": TOMORROW}
        await tool.handle("c1", {"to": "Sarah", "message": "Hi", "task": task})
        shown = self.confirm.await_args.args[2]
        self.assertTrue(shown["deadline"].endswith("23:59"))
        deadline = self.record.await_args.kwargs["deadline"]
        self.assertEqual((deadline.date().isoformat(), deadline.hour, deadline.minute), (TOMORROW, 23, 59))

    async def test_a_deadline_already_gone_asks_nothing(self):
        tool = self.tool()
        task = {"goal": "Lesson", "task_id": None, "brief": "b", "deadline": "2020-01-01"}
        await tool.handle("c1", {"to": "Sarah", "message": "Hi", "task": task})
        self.confirm.assert_not_awaited()
        self.assertIn("already passed", self.output()["message"])


class DeadlineCheckTests(unittest.IsolatedAsyncioTestCase):
    async def ready(self, app, pilot, soon=(), overdue=()):
        for _ in range(2):
            await pilot.pause()
            await app.workers.wait_for_complete()
        app.persistence.load_deadlines_due.return_value = (list(soon), list(overdue))
        app.tell_person = AsyncMock()
        app.task_needs_person = AsyncMock(return_value=True)

    async def test_a_day_before_you_hear_once_and_when_it_passes_it_comes_back_to_you(self):
        async with keyboard_tests.running_app() as (app, pilot):
            soon_id, late_id = uuid4(), uuid4()
            due = datetime(2026, 11, 6, 17, tzinfo=timezone.utc)
            await self.ready(app, pilot, soon=[(soon_id, "Lesson", due)], overdue=[(late_id, "Quote", due)])
            await app.check_deadlines(datetime.now(timezone.utc))
            app.tell_person.assert_awaited_once()
            self.assertIn('About your task "Lesson": it\'s due by', app.tell_person.await_args.args[0])
            app.task_needs_person.assert_awaited_once()
            self.assertIn("deadline", app.task_needs_person.await_args.args[2])
            self.assertEqual([call.args for call in app.persistence.task_deadline_told.call_args_list],
                             [(soon_id, False), (late_id, True)])

    async def test_if_you_couldnt_be_told_it_is_tried_again_next_round(self):
        async with keyboard_tests.running_app() as (app, pilot):
            due = datetime(2026, 11, 6, 17, tzinfo=timezone.utc)
            await self.ready(app, pilot, soon=[(uuid4(), "Lesson", due)], overdue=[(uuid4(), "Quote", due)])
            app.tell_person = AsyncMock(side_effect=RuntimeError("no way"))
            app.task_needs_person = AsyncMock(return_value=False)
            await app.check_deadlines(datetime.now(timezone.utc))
            app.persistence.task_deadline_told.assert_not_called()

    async def test_the_hourly_round_checks_deadlines(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.ready(app, pilot)
            app.persistence.load_task_checks.return_value = ([], [], [], [])
            app.check_tasks()
            for _ in range(3):
                await pilot.pause()
                await app.workers.wait_for_complete()
            app.persistence.load_deadlines_due.assert_called_once()


if __name__ == "__main__":
    unittest.main()
