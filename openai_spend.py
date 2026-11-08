"""What Cora costs: OpenAI's Costs API, read with an optional Admin key (Settings > General).

The Costs API only answers an organisation Admin key (`sk-admin-…`), never a project's API key.
The key needs one permission: Usage API, Read (a Restricted admin key). Only costs are read: by
day, and by project so the person can choose Cora's own. The key is kept in the system
credential store; the database only keeps which project's spend to show.

Blocking calls: run them off the UI thread.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import httpx2 as httpx
import keyring
from keyring.errors import PasswordDeleteError

from services.credentials import KEYRING_SERVICE

ACCOUNT = "openai:admin_key"
COSTS_URL = "https://api.openai.com/v1/organization/costs"
API_SECONDS = 20
# Daily buckets per page; the API allows up to 180.
PAGE_DAYS = 180
# How far back to look for projects with spend, when choosing whose spend to show.
PROJECT_LOOKBACK_DAYS = 90
# Months of spend on the dashboard, this one included.
SPEND_MONTHS = 12
# The spend setting's value for the whole organisation rather than one project; without a saved
# choice it's automatic (see shown_project).
ALL_PROJECTS = "all"
AUTOMATIC = "auto"


class AdminKeyError(Exception):
    """A short reason for the person (never the key)."""


def saved_admin_key() -> str | None:
    return keyring.get_password(KEYRING_SERVICE, ACCOUNT) or None


def save_admin_key(key: str) -> None:
    key = key.strip()
    if not key:
        raise ValueError("An empty key cannot be saved")
    keyring.set_password(KEYRING_SERVICE, ACCOUNT, key)


def forget_admin_key() -> None:
    try:
        keyring.delete_password(KEYRING_SERVICE, ACCOUNT)
    except PasswordDeleteError:
        pass  # nothing was saved


def masked(key: str) -> str:
    return f"…{key.strip()[-4:]}" if len(key.strip()) > 8 else ""


def costs(key: str, start: datetime, *, group_by_project: bool = False, project_id: str | None = None,
          transport=None) -> list[dict]:
    """Every daily bucket of costs from start until now, following the pages.

    Raises AdminKeyError with a reason for the person when it can't be read.
    """
    params = {"start_time": int(start.timestamp()), "bucket_width": "1d", "limit": PAGE_DAYS}
    if group_by_project:
        params["group_by"] = "project_id"
    if project_id and project_id != ALL_PROJECTS:
        params["project_ids"] = project_id
    buckets = []
    with httpx.Client(timeout=API_SECONDS, transport=transport,
                      headers={"Authorization": f"Bearer {key.strip()}"}) as client:
        while True:
            try:
                response = client.get(COSTS_URL, params=params)
            except httpx.HTTPError as error:
                raise AdminKeyError("Couldn't reach OpenAI; check the internet connection.") from error
            if response.status_code == 401:
                raise AdminKeyError(
                    "OpenAI didn't accept that key: it needs an Admin key (sk-admin-…), made by an "
                    "organisation owner, that hasn't expired."
                )
            if response.status_code == 403:
                raise AdminKeyError("That Admin key can't read costs: give it Usage API: Read.")
            if response.status_code != 200:
                raise AdminKeyError(f"OpenAI answered {response.status_code}; try again later.")
            page = response.json()
            buckets += page.get("data") or []
            if not (page.get("has_more") and page.get("next_page")):
                return buckets
            params = {**params, "page": page["next_page"]}


def projects_with_spend(key: str, *, now: datetime | None = None, transport=None) -> list[tuple[str, str]]:
    """Projects with any spend in the last PROJECT_LOOKBACK_DAYS, as (id, name), by name."""
    now = now or datetime.now(timezone.utc)
    names: dict[str, str] = {}
    for bucket in costs(key, now - timedelta(days=PROJECT_LOOKBACK_DAYS), group_by_project=True,
                        transport=transport):
        for result in bucket.get("results") or []:
            project_id = result.get("project_id")
            if project_id:
                names[project_id] = result.get("project_name") or names.get(project_id) or project_id
    return sorted(names.items(), key=lambda project: project[1].lower())


def likely_cora_project(projects: list[tuple[str, str]]) -> str | None:
    """The project named for Cora, when exactly one is."""
    named = [project_id for project_id, name in projects if "cora" in name.lower()]
    return named[0] if len(named) == 1 else None


def check_admin_key(key: str, *, transport=None) -> list[tuple[str, str]]:
    """The projects with spend that this key can see; AdminKeyError when it can't be used."""
    key = key.strip()
    if not key:
        raise AdminKeyError("Paste a key first.")
    if any(character.isspace() for character in key):
        raise AdminKeyError("That doesn't look like a key: it has spaces in it.")
    return projects_with_spend(key, transport=transport)


def shown_project(chosen: str | None, projects: list[tuple[str, str]]) -> str:
    """Whose spend to show: the person's choice, else Cora's project once it has spend, else everyone's."""
    if chosen:
        return chosen
    return likely_cora_project(projects) or ALL_PROJECTS


@dataclass(frozen=True)
class Spend:
    """Spend per calendar month (UTC), oldest first, ending with this month so far; and whose it is."""
    months: tuple[tuple[date, float], ...]
    whose: str  # a project's name, or "all projects"
    currency: str = "usd"


def month_starts(now: datetime, count: int) -> list[date]:
    """The first day of each of the last `count` months, oldest first, this month last."""
    year, month = now.year, now.month
    starts = []
    for _ in range(count):
        starts.append(date(year, month, 1))
        year, month = (year, month - 1) if month > 1 else (year - 1, 12)
    return starts[::-1]


def monthly_spend(key: str, chosen: str | None, *, now: datetime | None = None, transport=None) -> Spend:
    """The last SPEND_MONTHS months' spend for the chosen project (automatic when None).

    One listing grouped by project serves both choosing the project and adding up its months.
    Raises AdminKeyError with a reason for the person when it can't be read.
    """
    now = now or datetime.now(timezone.utc)
    starts = month_starts(now, SPEND_MONTHS)
    first = datetime(starts[0].year, starts[0].month, 1, tzinfo=timezone.utc)
    buckets = costs(key, first, group_by_project=True, transport=transport)
    names: dict[str, str] = {}
    for bucket in buckets:
        for result in bucket.get("results") or []:
            if result.get("project_id"):
                names[result["project_id"]] = result.get("project_name") or result["project_id"]
    shown = shown_project(chosen, sorted(names.items(), key=lambda project: project[1].lower()))
    totals = dict.fromkeys(starts, 0.0)
    currency = "usd"
    for bucket in buckets:
        day = datetime.fromtimestamp(int(bucket.get("start_time") or 0), timezone.utc)
        month = date(day.year, day.month, 1)
        if month not in totals:
            continue
        for result in bucket.get("results") or []:
            if shown != ALL_PROJECTS and result.get("project_id") != shown:
                continue
            amount = result.get("amount") or {}
            totals[month] += float(amount.get("value") or 0)
            currency = amount.get("currency") or currency
    whose = "all projects" if shown == ALL_PROJECTS else names.get(shown, shown)
    return Spend(tuple(totals.items()), whose, currency)
