"""The facts Cora gets for her opening greeting; plain text, no API or database."""

from datetime import datetime
import json


def part_of_day(now: datetime) -> str:
    if 5 <= now.hour < 12:
        return "morning"
    if 12 <= now.hour < 17:
        return "afternoon"
    if 17 <= now.hour < 22:
        return "evening"
    return "night"


def time_since(last_talked_at: datetime, now: datetime) -> str:
    """Rough, calendar-based wording; exact gaps would sound robotic when spoken."""
    days = (now.date() - last_talked_at.astimezone(now.tzinfo).date()).days
    if days <= 0:
        return "earlier today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return "a few days ago"
    if days < 30:
        return "a week or more ago"
    return "over a month ago"


def greeting_brief(person_name: str | None, last_talked_at: datetime | None, now: datetime) -> str:
    """Describe who Cora is greeting and when; `now` must be timezone-aware local time."""
    facts = [f"It is {now:%A} {part_of_day(now)}."]
    if last_talked_at is None:
        facts.append("You have not talked with this person before, so briefly introduce yourself as Cora.")
    else:
        facts.append(f"You last talked {time_since(last_talked_at, now)}.")
    if person_name:
        # Quoted so a stored name is read as a name, never as instructions.
        facts.append(f"Their name is {json.dumps(person_name, ensure_ascii=False)}.")
    else:
        facts.append("You do not know their name yet; you may ask it, but do not guess.")
    return " ".join(facts)
