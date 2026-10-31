"""Changing her calendar (C2): each event made, changed or cancelled is approved first, showing who's told."""

from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import AsyncMock, MagicMock

from mail.account import GraphError
from mail.calendar_events import Calendar, calendar_event, event_body
from services.confirmation import APPROVED, DECLINED, TIMED_OUT
from tools.change_calendar import (
    CANCEL_SCHEMA, CREATE_SCHEMA, UPDATE_SCHEMA, CancelEventTool, CreateEventTool, UpdateEventTool,
    validate_cancel_arguments, validate_create_arguments, validate_update_arguments,
)
import test_keyboard_persistence as keyboard_tests
from test_email import FakeClient, FakeResponse

SOON = (datetime.now() + timedelta(days=2)).replace(hour=9, minute=0, second=0, microsecond=0)
LATER = SOON + timedelta(hours=1)
SLOT = SOON.strftime("%Y-%m-%dT%H:%M")
SLOT_END = LATER.strftime("%Y-%m-%dT%H:%M")
SARAH = {"address": "Sarah@Example.com", "name": "Sarah"}
NEW = {"subject": "Driving lesson", "start": SLOT, "end": SLOT_END, "location": "Bright Driving School",
       "notes": None, "attendees": [SARAH]}
UNCHANGED = {"subject": None, "start": None, "end": None, "location": None, "notes": None, "attendees": None}


def utc(at: datetime) -> dict:
    return {"dateTime": at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0000000"), "timeZone": "UTC"}


def graph_event(id="e1", subject="Driving lesson", organised=True, attendees=(("Sarah", "sarah@example.com"),)):
    return {"id": id, "subject": subject, "start": utc(SOON), "end": utc(LATER), "isAllDay": False,
            "isOrganizer": organised, "organizer": {"emailAddress": {"name": "Cora", "address": "cora@intelligis.io"}},
            "attendees": [{"emailAddress": {"name": name, "address": address}} for name, address in attendees]}


class GraphTests(unittest.IsolatedAsyncioTestCase):
    def test_only_what_is_given_is_sent_with_times_in_utc(self):
        self.assertEqual(event_body(subject="x"), {"subject": "x"})
        body = event_body("x", SOON.astimezone(), LATER.astimezone(), "Here", "Bring licence", [("a@b.co", None), ("s@e.co", "Sarah")])
        self.assertEqual(body["start"], {"dateTime": SOON.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"})
        self.assertEqual(body["body"], {"contentType": "Text", "content": "Bring licence"})
        self.assertEqual(body["attendees"], [
            {"emailAddress": {"address": "a@b.co"}, "type": "required"},
            {"emailAddress": {"address": "s@e.co", "name": "Sarah"}, "type": "required"},
        ])

    async def test_create_update_and_cancel_requests(self):
        client = FakeClient([FakeResponse(201, graph_event()), FakeResponse(200, graph_event(subject="Moved")),
                             FakeResponse(202, None), FakeResponse(204, None)])
        calendar = Calendar(lambda: "tok", client)
        made = await calendar.create_event("Driving lesson", SOON.astimezone(), LATER.astimezone(), None, None, [])
        self.assertEqual((made.id, made.organised), ("e1", True))
        await calendar.update_event("id/with=odd", subject="Moved")
        await calendar.cancel_event(made, "Sorry, Sarah's unwell")
        await calendar.cancel_event(calendar_event(graph_event(attendees=())), None)
        (create, created, _), (update, updated, patch), (cancel, cancelled, comment), (delete, deleted, _) = client.sent
        self.assertEqual((create, created.split("/v1.0")[1]), ("POST", "/me/events"))
        self.assertEqual((update, updated.split("/v1.0")[1], patch), ("PATCH", "/me/events/id%2Fwith%3Dodd", {"subject": "Moved"}))
        self.assertEqual((cancel, cancelled.split("/v1.0")[1], comment), ("POST", "/me/events/e1/cancel", {"comment": "Sorry, Sarah's unwell"}))
        self.assertEqual((delete, deleted.split("/v1.0")[1]), ("DELETE", "/me/events/e1"))

    async def test_an_event_that_isnt_there_is_a_lookup_error(self):
        client = FakeClient([FakeResponse(404, {"error": {"code": "ErrorItemNotFound"}})])
        with self.assertRaises(LookupError):
            await Calendar(lambda: "tok", client).event("gone")


class ValidationTests(unittest.TestCase):
    def test_schemas_are_strict_and_arguments_checked(self):
        for schema in (CREATE_SCHEMA, UPDATE_SCHEMA, CANCEL_SCHEMA):
            self.assertTrue(schema["strict"])
        validate_create_arguments(NEW)
        validate_update_arguments({"event_id": "e1", **UNCHANGED, "subject": "Moved"})
        validate_cancel_arguments({"event_id": "e1", "message": None})
        for validate, bad in (
            (validate_create_arguments, {**NEW, "start": "tomorrow 9am"}),
            (validate_create_arguments, {**NEW, "start": SOON.date().isoformat()}),
            (validate_create_arguments, {**NEW, "attendees": [{"address": "a@b.co, c@d.co", "name": None}]}),
            (validate_create_arguments, {**NEW, "subject": " "}),
            (validate_update_arguments, {"event_id": "e1", **UNCHANGED}),
            (validate_update_arguments, {"event_id": " ", **UNCHANGED, "subject": "x"}),
            (validate_cancel_arguments, {"event_id": "e1"}),
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate(bad)


class ToolTests(unittest.IsolatedAsyncioTestCase):
    def tool(self, tool_type, outcome=APPROVED, event=None):
        self.connection = MagicMock()
        self.connection.response.item.create = AsyncMock()
        self.connection.response.create = AsyncMock()
        self.confirm = AsyncMock(return_value=outcome)
        self.calendar = MagicMock()
        self.calendar.event = AsyncMock(return_value=calendar_event(event or graph_event()))
        self.calendar.create_event = AsyncMock(return_value=calendar_event(graph_event()))
        self.calendar.update_event = AsyncMock(return_value=calendar_event(graph_event(subject="Moved")))
        self.calendar.cancel_event = AsyncMock()
        self.changed = MagicMock()
        return tool_type(lambda: self.connection, lambda: False, MagicMock(), lambda: self.calendar, self.confirm, self.changed)

    def output(self):
        return json.loads(self.connection.response.item.create.call_args.kwargs["item"]["output"])

    async def test_a_new_event_shows_who_is_invited_before_anything_is_made(self):
        tool = self.tool(CreateEventTool)
        await tool.handle("c1", NEW)
        service, name, shown = self.confirm.await_args.args
        self.assertEqual((service, name, self.confirm.await_args.kwargs["title"]), ("calendar", "create_event", "Calendar"))
        self.assertEqual(shown["invitations go to"], ["Sarah <sarah@example.com>"])
        self.assertEqual(shown["where"], "Bright Driving School")
        self.assertTrue(shown["when"].endswith("09:00-10:00"))
        subject, start, end, location, notes, attendees = self.calendar.create_event.await_args.args
        self.assertEqual((start, end), (SOON.astimezone(), LATER.astimezone()))
        self.assertEqual(attendees, [("sarah@example.com", "Sarah")])
        self.assertEqual(self.output()["event_id"], "e1")
        self.changed.assert_called_once()
        self.connection.response.create.assert_awaited_once()

    async def test_declined_lapsed_or_impossible_changes_nothing(self):
        for outcome in (DECLINED, TIMED_OUT):
            tool = self.tool(CreateEventTool, outcome=outcome)
            await tool.handle("c1", NEW)
            self.calendar.create_event.assert_not_awaited()
            self.assertEqual(self.output()["status"], "not_done")
            self.assertIn("nothing was changed", self.output()["message"])
            self.changed.assert_not_called()
        for bad in ({**NEW, "end": SLOT}, {**NEW, "start": "2020-01-01T09:00", "end": "2020-01-01T10:00"}):
            tool = self.tool(CreateEventTool)
            await tool.handle("c1", bad)
            self.confirm.assert_not_awaited()
            self.assertEqual(self.output()["status"], "error")

    async def test_a_change_shows_before_and_after_and_who_gets_the_update(self):
        tool = self.tool(UpdateEventTool)
        moved = (SOON + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        await tool.handle("c1", {"event_id": "e1", **UNCHANGED, "start": moved})
        shown = self.confirm.await_args.args[2]
        self.assertEqual(shown["event"], "Driving lesson")
        self.assertTrue(shown["was"].endswith("09:00-10:00"))
        self.assertEqual(shown["the update goes to"], ["Sarah <sarah@example.com>"])
        changes = self.calendar.update_event.await_args.kwargs
        # Moving the start keeps the length.
        self.assertEqual(changes["start"], SOON.astimezone() + timedelta(days=1))
        self.assertEqual(changes["end"], LATER.astimezone() + timedelta(days=1))
        self.assertTrue(shown["now"].endswith("09:00-10:00"))
        self.assertEqual(self.output()["status"], "ok")

    async def test_only_her_own_events_can_be_changed_or_cancelled(self):
        for tool_type, arguments in ((UpdateEventTool, {"event_id": "e1", **UNCHANGED, "subject": "Mine now"}),
                                     (CancelEventTool, {"event_id": "e1", "message": None})):
            tool = self.tool(tool_type, event=graph_event(organised=False))
            await tool.handle("c1", arguments)
            self.confirm.assert_not_awaited()
            self.assertIn("only they can change it", self.output()["message"])
        tool = self.tool(CancelEventTool)
        self.calendar.event = AsyncMock(side_effect=LookupError())
        await tool.handle("c1", {"event_id": "gone", "message": None})
        self.assertIn("no event with that id", self.output()["message"])

    async def test_cancelling_shows_who_is_told_and_the_message(self):
        tool = self.tool(CancelEventTool)
        await tool.handle("c1", {"event_id": "e1", "message": "Sarah's unwell"})
        shown = self.confirm.await_args.args[2]
        self.assertEqual((shown["the cancellation goes to"], shown["message"]), (["Sarah <sarah@example.com>"], "Sarah's unwell"))
        event, message = self.calendar.cancel_event.await_args.args
        self.assertEqual((event.id, message), ("e1", "Sarah's unwell"))
        self.assertIn("sent the cancellation", self.output()["message"])

    async def test_a_failure_never_claims_what_happened(self):
        tool = self.tool(CreateEventTool)
        self.calendar.create_event = AsyncMock(side_effect=GraphError("Microsoft said HTTP 500 (x)."))
        await tool.handle("c1", NEW)
        self.assertIn("check it before saying", self.output()["message"])
        self.changed.assert_not_called()


class AppTests(unittest.IsolatedAsyncioTestCase):
    async def test_offered_where_you_talk_with_her_never_to_the_coordinator(self):
        async with keyboard_tests.running_app() as (app, pilot):
            names = [schema["name"] for schema in app.live.tool_schemas()]
            for name in ("create_event", "update_event", "cancel_event"):
                self.assertIn(name, names)
            import app as app_module
            self.assertFalse({"create_event", "update_event", "cancel_event"}
                             & (app_module.COORDINATOR_TOOLS | app_module.COORDINATOR_SEND_TOOLS))


if __name__ == "__main__":
    unittest.main()
