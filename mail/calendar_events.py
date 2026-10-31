"""Cora's own Outlook calendar through Microsoft Graph: what's on, so she knows and can say (C1), and
events she makes, changes and cancels, each approved by the person first (C2).

Her calendar, not the person's. Anyone can send her an invitation, so what an event says (its
subject, location and notes) is untrusted data, like mail: it never asks her to do anything.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import cache
import json
from urllib.parse import quote

from .account import GraphError, Mailbox, MailSubscription, graph_time, parse_graph_time

EVENTS_MAX = 50
PREVIEW_MAX_CHARS = 200
ATTENDEES_MAX = 10
# This PC's time zone as Windows (and so Outlook) names it.
ZONE_KEY = r"SYSTEM\CurrentControlSet\Control\TimeZoneInformation"
# What's coming, kept in her context so she knows without looking: the next week, briefly.
CONTEXT_DAYS = 7
CONTEXT_EVENTS_MAX = 30
CALENDAR_CONTEXT_MAX_CHARS = 5000
EVENTS_RESOURCE = "me/events"
EVENT_FIELDS = (
    "id,subject,start,end,isAllDay,location,organizer,attendees,isCancelled,showAs,bodyPreview,isOnlineMeeting,"
    "isOrganizer"
)


@dataclass(frozen=True)
class CalendarEvent:
    id: str
    subject: str
    start: datetime
    end: datetime
    all_day: bool
    location: str
    organizer: str
    attendees: tuple[str, ...]
    cancelled: bool
    # free, tentative, busy, oof, workingElsewhere or unknown.
    show_as: str
    online: bool
    preview: str
    # Hers to change: she made it, rather than being invited to it.
    organised: bool = False

    def when(self) -> str:
        """In the person's local time, such as "Mon 02 Nov 09:00-10:00" or "Mon 02 Nov, all day"."""
        if self.all_day:
            # All-day events run from midnight to midnight wherever they were made: dates only.
            first, last = self.start.date(), self.end.date()
            days = (last - first).days
            shown = f"{first:%a %d %b}, all day"
            return shown if days <= 1 else f"{shown} to {date.fromordinal(last.toordinal() - 1):%a %d %b}"
        start, end = self.start.astimezone(), self.end.astimezone()
        if start.date() == end.date():
            return f"{start:%a %d %b %H:%M}-{end:%H:%M}"
        return f"{start:%a %d %b %H:%M} to {end:%a %d %b %H:%M}"


@cache
def local_zone() -> str:
    """The person's time zone, as Windows names it (such as "W. Australia Standard Time"), which
    Microsoft accepts; "UTC" when it can't be read."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, ZONE_KEY) as key:
            return str(winreg.QueryValueEx(key, "TimeZoneKeyName")[0]).strip() or "UTC"
    except Exception:
        return "UTC"


def in_local_zone() -> dict:
    # Times come back in the person's zone, so an all-day event's dates are their dates.
    return {"Prefer": f'outlook.timezone="{local_zone()}"'}


def event_time(value: dict) -> datetime:
    # {"dateTime": "2026-11-02T09:00:00.0000000", "timeZone": ...}: in UTC, or in this PC's own zone
    # as asked for with in_local_zone().
    moment = parse_graph_time(value["dateTime"])
    if moment.tzinfo is not None:
        return moment
    return moment.replace(tzinfo=timezone.utc) if value.get("timeZone") == "UTC" else moment.astimezone()


def person(entry: dict | None) -> str:
    address = (entry or {}).get("emailAddress") or {}
    name, mail = address.get("name") or "", address.get("address") or ""
    return f"{name} <{mail}>" if name and mail and name != mail else name or mail


def calendar_event(event: dict) -> CalendarEvent:
    attendees = tuple(filter(None, map(person, event.get("attendees") or [])))
    return CalendarEvent(
        event["id"], event.get("subject") or "(no subject)", event_time(event["start"]), event_time(event["end"]),
        bool(event.get("isAllDay")), ((event.get("location") or {}).get("displayName") or "").strip(),
        person(event.get("organizer")), attendees[:ATTENDEES_MAX], bool(event.get("isCancelled")),
        event.get("showAs") or "unknown", bool(event.get("isOnlineMeeting")),
        (event.get("bodyPreview") or "").strip()[:PREVIEW_MAX_CHARS], bool(event.get("isOrganizer")),
    )


def graph_slot(at: date) -> dict:
    """A time in UTC, or a day (for an all-day event) starting at midnight in the person's zone."""
    if isinstance(at, datetime):
        return {"dateTime": at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"}
    return {"dateTime": f"{at.isoformat()}T00:00:00", "timeZone": local_zone()}


def event_body(subject: str | None = None, start: date | None = None, end: date | None = None,
               location: str | None = None, notes: str | None = None,
               attendees: list[tuple[str, str | None]] | None = None) -> dict:
    """An event as Graph takes it, with only what's given: None leaves a field as it is. Times make
    a timed event; days (dates) an all-day one, with end the day after the last."""
    body = {}
    if subject is not None:
        body["subject"] = subject
    if start is not None:
        body["start"] = graph_slot(start)
        body["isAllDay"] = not isinstance(start, datetime)
    if end is not None:
        body["end"] = graph_slot(end)
    if location is not None:
        body["location"] = {"displayName": location}
    if notes is not None:
        body["body"] = {"contentType": "Text", "content": notes}
    if attendees is not None:
        body["attendees"] = [
            {"emailAddress": {"address": address} | ({"name": name} if name else {}), "type": "required"}
            for address, name in attendees
        ]
    return body


def calendar_context(events: list[CalendarEvent], now: datetime) -> str:
    """Her coming week for her context: when, what and where, cancelled ones left out, the latest
    dropped first to fit."""
    view = {
        "as_of": f"{now.astimezone():%a %d %b %H:%M}",
        "events": [{"when": event.when(), "subject": event.subject[:200], "where": event.location[:200] or None}
                   for event in events if not event.cancelled][:CONTEXT_EVENTS_MAX],
    }
    while view["events"] and len(json.dumps(view, ensure_ascii=False)) > CALENDAR_CONTEXT_MAX_CHARS:
        view["events"].pop()
    return json.dumps(view, ensure_ascii=False)


class Calendar(Mailbox):
    """Her calendar, through the same signed-in Graph requests as her mail (with a calendar token)."""

    async def events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        """Every event between the two times, recurring ones as each occurrence, earliest first."""
        page = await self._request("GET", "/me/calendarView", params={
            "startDateTime": graph_time(start), "endDateTime": graph_time(end),
            "$orderby": "start/dateTime", "$top": str(EVENTS_MAX), "$select": EVENT_FIELDS,
        }, headers=in_local_zone())
        return [calendar_event(event) for event in page.get("value", [])]

    def _path(self, event_id: str) -> str:
        return f"/me/events/{quote(event_id, safe='')}"

    async def event(self, event_id: str) -> CalendarEvent:
        """One event by its id; LookupError if it isn't in her calendar."""
        try:
            return calendar_event(await self._request(
                "GET", self._path(event_id), params={"$select": EVENT_FIELDS}, headers=in_local_zone()))
        except GraphError as error:
            if "HTTP 400" in str(error) or "HTTP 404" in str(error):
                raise LookupError("No such event") from None
            raise

    async def create_event(self, subject: str, start: datetime, end: datetime, location: str | None,
                           notes: str | None, attendees: list[tuple[str, str | None]]) -> CalendarEvent:
        """A new event of hers; Outlook sends each attendee an invitation."""
        return calendar_event(await self._request("POST", "/me/events", body=event_body(
            subject, start, end, location, notes, attendees), headers=in_local_zone()))

    async def update_event(self, event_id: str, **changes) -> CalendarEvent:
        """Changes one of her events (event_body's fields); Outlook sends attendees the update."""
        return calendar_event(await self._request(
            "PATCH", self._path(event_id), body=event_body(**changes), headers=in_local_zone()))

    async def cancel_event(self, event: CalendarEvent, message: str | None) -> None:
        """Takes one of her events off: a meeting is cancelled, so its attendees are told (with the
        message); an event with nobody invited is just deleted."""
        if event.attendees:
            await self._request("POST", f"{self._path(event.id)}/cancel", body={"comment": message or ""})
        else:
            await self._request("DELETE", self._path(event.id))

    async def forward_event(self, event_id: str, recipients: list[tuple[str, str | None]], comment: str | None) -> None:
        """Sends an event on as an invitation, from her, with her comment: hers or one she was invited to."""
        await self._request("POST", f"{self._path(event_id)}/forward", body={
            "toRecipients": [{"emailAddress": {"address": address} | ({"name": name} if name else {})}
                             for address, name in recipients],
            "comment": comment or "",
        })

    async def subscribe_to_changes(self, url: str, lifecycle_url: str, client_state: str,
                                   expires: datetime) -> MailSubscription:
        """Graph calls `url` when an event in her calendar is made, changed or deleted."""
        return await self.subscribe(url, lifecycle_url, client_state, expires,
                                    resource=EVENTS_RESOURCE, change_type="created,updated,deleted")

    async def coming_week(self, now: datetime) -> str:
        """calendar_context for the next CONTEXT_DAYS."""
        return calendar_context(await self.events(now, now + timedelta(days=CONTEXT_DAYS)), now)
