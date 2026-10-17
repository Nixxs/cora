"""The facts Cora gets for her opening greeting; plain text, no API or database."""

from datetime import datetime
import json
import random

# Greeting with no hook this often, even when one is available, so it never feels scripted.
NO_HOOK_CHANCE = 1 / 3


def last_thread_hook(summary: str) -> str:
    return (
        f"Your own summary of your last conversation: {json.dumps(summary, ensure_ascii=False)}. "
        "You may pick that thread up if it feels natural."
    )


def finding_hook(interest_title: str, finding: str) -> str:
    return (
        f"While exploring your interest {json.dumps(interest_title, ensure_ascii=False)} you recently "
        f"found: {json.dumps(finding, ensure_ascii=False)}. You may mention it as your own discovery "
        "if it feels natural; it may be outdated or wrong."
    )


def choose_hook(options: list, rng=random):
    """Pick at most one option, sometimes none; callers pass hook kinds or hook texts."""
    if not options or rng.random() < NO_HOOK_CHANCE:
        return None
    return rng.choice(options)


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


def greeting_brief(
    person_name: str | None, last_talked_at: datetime | None, now: datetime, hook: str | None = None,
) -> str:
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
    if hook:
        facts.append(hook)
    return " ".join(facts)
