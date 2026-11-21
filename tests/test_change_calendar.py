import app_areas.tasks
"""Changing her calendar (C2): change_calendar makes, changes, cancels or forwards an event, each
approved first, showing who's told."""

from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import AsyncMock, MagicMock

from mail.account import GraphError
from mail.calendar_events import Calendar, calendar_event, event_body, local_zone
from services.confirmation import APPROVED, DECLINED, TIMED_OUT
from tools.change_calendar import FIELDS, SCHEMA, ChangeCalendarTool, needs, validate_arguments
import test_keyboard_persistence as keyboard_tests
from test_email import FakeClient, FakeResponse

SOON = (datetime.now() + timedelta(days=2)).replace(hour=9, minute=0, second=0, microsecond=0)
LATER = SOON + timedelta(hours=1)
SLOT = SOON.strftime("%Y-%m-%dT%H:%M")
SLOT_END = LATER.strftime("%Y-%m-%dT%H:%M")
SARAH = {"address": "Sarah@Example.com", "name": "Sarah"}
NEW = {"subject": "Driving lesson", "start": SLOT, "end": SLOT_END, "location": "Bright Driving School",
       "notes": None, "attendees": [SARAH]}


def call(action, **given) -> dict:
    """change_calendar's arguments: the action, what's given, null for the rest."""
    return {key: None for key in FIELDS} | {"action": action} | given


def create(fields=None, **given) -> dict:
    return call("create", **((fields if fields is not None else NEW) | given))


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


FIRST_DAY = (datetime.now() + timedelta(days=30)).date()
LAST_DAY = FIRST_DAY + timedelta(days=5)
TRIP = {**NEW, "subject": "Family holiday", "start": FIRST_DAY.isoformat(), "end": LAST_DAY.isoformat(),
        "location": "Kuala Lumpur", "attendees": []}


def all_day_event(first=FIRST_DAY, days=6, organised=True):
    def midnight(day):
        return {"dateTime": f"{day.isoformat()}T00:00:00.0000000", "timeZone": local_zone()}
    return {**graph_event(organised=organised, attendees=()), "subject": "Family holiday", "isAllDay": True,
            "start": midnight(first), "end": midnight(first + timedelta(days=days))}


class AllDayTests(unittest.IsolatedAsyncioTestCase):
    def test_days_go_to_microsoft_as_midnights_in_your_zone_ending_the_day_after(self):
        body = event_body(start=FIRST_DAY, end=LAST_DAY + timedelta(days=1))
        self.assertEqual(body["start"], {"dateTime": f"{FIRST_DAY.isoformat()}T00:00:00", "timeZone": local_zone()})
        self.assertEqual(body["end"]["dateTime"], f"{(LAST_DAY + timedelta(days=1)).isoformat()}T00:00:00")
        self.assertTrue(body["isAllDay"])
        self.assertFalse(event_body(start=SOON.astimezone(), end=LATER.astimezone())["isAllDay"])

    async def test_a_trip_is_one_event_from_its_first_day_to_its_last(self):
        tool = ToolTests.tool(self)
        await tool.handle("c1", create(TRIP))
        shown = self.confirm.await_args.args[2]
        self.assertEqual(shown["when"], f"{FIRST_DAY:%a %d %b %Y} to {LAST_DAY:%a %d %b %Y}, all day")
        _, start, end, *_ = self.calendar.create_event.await_args.args
        self.assertEqual((start, end), (FIRST_DAY, LAST_DAY + timedelta(days=1)))
        self.assertEqual(calendar_event(all_day_event()).when(),
                         f"{FIRST_DAY:%a %d %b}, all day to {LAST_DAY:%a %d %b}")

    async def test_all_day_events_are_checked(self):
        for bad in ({**TRIP, "end": (FIRST_DAY - timedelta(days=1)).isoformat()},
                    {**TRIP, "end": (FIRST_DAY + timedelta(days=31)).isoformat()},
                    {**TRIP, "start": "2020-01-01", "end": "2020-01-02"}):
            tool = ToolTests.tool(self)
            await tool.handle("c1", create(bad))
            self.confirm.assert_not_awaited()
        self.assertRegex(needs(create({**TRIP, "end": SLOT})), "both")

    async def test_moving_a_trip_by_its_first_day_keeps_its_length(self):
        tool = ToolTests.tool(self, event=all_day_event())
        moved = FIRST_DAY + timedelta(days=7)
        await tool.handle("c1", call("update", event_id="e1", start=moved.isoformat()))
        changes = self.calendar.update_event.await_args.kwargs
        self.assertEqual((changes["start"], changes["end"]), (moved, moved + timedelta(days=6)))
        self.assertTrue(self.confirm.await_args.args[2]["now"].endswith("all day"))


class ValidationTests(unittest.TestCase):
    def test_types_are_checked_up_front(self):
        self.assertTrue(SCHEMA["strict"])
        self.assertEqual(SCHEMA["parameters"]["required"], list(FIELDS))
        validate_arguments(create())
        validate_arguments(call("update", event_id="e1", subject="Moved"))
        validate_arguments(call("cancel", event_id="e1"))
        for bad in (
            create() | {"action": "delete"},
            create(attendees=[{"address": "a@b.co, c@d.co", "name": None}]),
            create(subject=" "),
            {key: value for key, value in create().items() if key != "message"},
            create(start=9),
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_arguments(bad)

    def test_what_an_action_needs_comes_back_to_her_to_put_right(self):
        # A call that fails validation is dropped without an answer, so these are told, not refused.
        self.assertIsNone(needs(create()))
        self.assertEqual(needs(call("create", subject="Lunch", start=SLOT)), "To create an event, give end.")
        self.assertRegex(needs(create(start="tomorrow 9am")), "YYYY-MM-DD")
        self.assertRegex(needs(create(start=SOON.date().isoformat())), "both")
        self.assertEqual(needs(call("update", event_id="e1")), "Give at least one thing to change.")
        self.assertRegex(needs(call("cancel", event_id=" ")), "event_id")
        self.assertRegex(needs(call("forward", event_id="e1")), "attendees")


class ToolTests(unittest.IsolatedAsyncioTestCase):
    def tool(self, outcome=APPROVED, event=None):
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
        return ChangeCalendarTool(lambda: self.connection, lambda: False, MagicMock(), lambda: self.calendar, self.confirm,
                                  self.changed)

    def output(self):
        return json.loads(self.connection.response.item.create.call_args.kwargs["item"]["output"])

    async def test_a_new_event_shows_who_is_invited_before_anything_is_made(self):
        tool = self.tool()
        await tool.handle("c1", create())
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
            tool = self.tool(outcome=outcome)
            await tool.handle("c1", create())
            self.calendar.create_event.assert_not_awaited()
            self.assertEqual(self.output()["status"], "not_done")
            self.assertIn("nothing was changed", self.output()["message"])
            self.changed.assert_not_called()
        for bad in ({**NEW, "end": SLOT}, {**NEW, "start": "2020-01-01T09:00", "end": "2020-01-01T10:00"}):
            tool = self.tool()
            await tool.handle("c1", create(bad))
            self.confirm.assert_not_awaited()
            self.assertEqual(self.output()["status"], "error")

    async def test_a_change_shows_before_and_after_and_who_gets_the_update(self):
        tool = self.tool()
        moved = (SOON + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        await tool.handle("c1", call("update", event_id="e1", start=moved))
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
        for arguments in (call("update", event_id="e1", subject="Mine now"), call("cancel", event_id="e1")):
            tool = self.tool(event=graph_event(organised=False))
            await tool.handle("c1", arguments)
            self.confirm.assert_not_awaited()
            self.assertIn("only they can change it", self.output()["message"])
        tool = self.tool()
        self.calendar.event = AsyncMock(side_effect=LookupError())
        await tool.handle("c1", call("cancel", event_id="gone"))
        self.assertIn("no event with that id", self.output()["message"])

    async def test_cancelling_shows_who_is_told_and_the_message(self):
        tool = self.tool()
        await tool.handle("c1", call("cancel", event_id="e1", message="Sarah's unwell"))
        shown = self.confirm.await_args.args[2]
        self.assertEqual((shown["the cancellation goes to"], shown["message"]), (["Sarah <sarah@example.com>"], "Sarah's unwell"))
        event, message = self.calendar.cancel_event.await_args.args
        self.assertEqual((event.id, message), ("e1", "Sarah's unwell"))
        self.assertIn("sent the cancellation", self.output()["message"])

    async def test_a_failure_never_claims_what_happened(self):
        tool = self.tool()
        self.calendar.create_event = AsyncMock(side_effect=GraphError("Microsoft said HTTP 500 (x)."))
        await tool.handle("c1", create())
        self.assertIn("check it before saying", self.output()["message"])
        self.changed.assert_not_called()


class ForwardTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_invitation_can_be_forwarded_once_approved_showing_whose_it_is(self):
        tool = ToolTests.tool(self, event=graph_event(organised=False))
        self.calendar.forward_event = AsyncMock()
        arguments = call("forward", event_id="e1", attendees=[{"address": "Nick@Example.com", "name": "Nick"}],
                         message="FYI")
        validate_arguments(arguments)
        await tool.handle("c1", arguments)
        shown = self.confirm.await_args.args[2]
        self.assertEqual(shown["forwarded to"], ["Nick <nick@example.com>"])
        self.assertEqual((shown["organised by"], shown["comment"]), ("Cora <cora@intelligis.io>", "FYI"))
        self.calendar.forward_event.assert_awaited_once_with("e1", [("nick@example.com", "Nick")], "FYI")
        self.assertEqual(ToolTests.output(self)["status"], "ok")

    async def test_declined_forwards_nothing_and_nobody_is_refused(self):
        tool = ToolTests.tool(self, outcome=DECLINED)
        self.calendar.forward_event = AsyncMock()
        await tool.handle("c1", call("forward", event_id="e1", attendees=[{"address": "a@b.co", "name": None}]))
        self.calendar.forward_event.assert_not_awaited()
        tool = ToolTests.tool(self)
        await tool.handle("c1", call("forward", event_id="e1", attendees=[]))
        self.confirm.assert_not_awaited()
        self.assertIn("who to forward it to", ToolTests.output(self)["message"])

    async def test_the_request_microsoft_gets(self):
        client = FakeClient([FakeResponse(202, None)])
        await Calendar(lambda: "tok", client).forward_event("e1", [("a@b.co", None), ("n@e.co", "Nick")], None)
        method, url, body = client.sent[0]
        self.assertEqual((method, url.split("/v1.0")[1]), ("POST", "/me/events/e1/forward"))
        self.assertEqual(body, {"toRecipients": [{"emailAddress": {"address": "a@b.co"}},
                                                 {"emailAddress": {"address": "n@e.co", "name": "Nick"}}], "comment": ""})


class AppTests(unittest.IsolatedAsyncioTestCase):
    async def test_offered_where_you_talk_with_her_never_to_the_coordinator(self):
        async with keyboard_tests.running_app() as (app, pilot):
            names = [schema["name"] for schema in app.live.tool_schemas()]
            self.assertIn("change_calendar", names)
            self.assertNotIn("create_event", names)
            self.assertNotIn("change_calendar", app_areas.tasks.COORDINATOR_TOOLS | app_areas.tasks.COORDINATOR_SEND_TOOLS)


if __name__ == "__main__":
    unittest.main()
