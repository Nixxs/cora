"""Cora's own Outlook calendar, read through Microsoft Graph (C1): what's on, so she knows and can say.

Her calendar, not the person's. Anyone can send her an invitation, so what an event says (its
subject, location and notes) is untrusted data, like mail: it never asks her to do anything.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json

from .account import Mailbox, graph_time, parse_graph_time

EVENTS_MAX = 50
PREVIEW_MAX_CHARS = 200
ATTENDEES_MAX = 10
# Times come back in UTC, so they're shown in the person's own time zone here.
IN_UTC = {"Prefer": 'outlook.timezone="UTC"'}
# What's coming, kept in her context so she knows without looking: the next week, briefly.
CONTEXT_DAYS = 7
CONTEXT_EVENTS_MAX = 30
CALENDAR_CONTEXT_MAX_CHARS = 5000
EVENT_FIELDS ="id,subject,start,end,isAllDay,location,organizer,attendees,isCancelled,showAs,bodyPreview,isOnlineMeeting"


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


def utc_time(value: dict) -> datetime:
    # {"dateTime": "2026-11-02T09:00:00.0000000", "timeZone": "UTC"}, as asked for with IN_UTC.
    return parse_graph_time(value["dateTime"]).replace(tzinfo=timezone.utc)


def person(entry: dict | None) -> str:
    address = (entry or {}).get("emailAddress") or {}
    name, mail = address.get("name") or "", address.get("address") or ""
    return f"{name} <{mail}>" if name and mail and name != mail else name or mail


def calendar_event(event: dict) -> CalendarEvent:
    attendees = tuple(filter(None, map(person, event.get("attendees") or [])))
    return CalendarEvent(
        event["id"], event.get("subject") or "(no subject)", utc_time(event["start"]), utc_time(event["end"]),
        bool(event.get("isAllDay")), ((event.get("location") or {}).get("displayName") or "").strip(),
        person(event.get("organizer")), attendees[:ATTENDEES_MAX], bool(event.get("isCancelled")),
        event.get("showAs") or "unknown", bool(event.get("isOnlineMeeting")),
        (event.get("bodyPreview") or "").strip()[:PREVIEW_MAX_CHARS],
    )


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
        }, headers=IN_UTC)
        return [calendar_event(event) for event in page.get("value", [])]

    async def coming_week(self, now: datetime) -> str:
        """calendar_context for the next CONTEXT_DAYS."""
        return calendar_context(await self.events(now, now + timedelta(days=CONTEXT_DAYS)), now)
