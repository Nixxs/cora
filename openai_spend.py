"""What Cora costs: OpenAI's Costs API, read with an optional Admin key (Settings > General).

The Costs API only answers an organisation Admin key (`sk-admin-…`), never a project's API key.
The key needs one permission: Usage API, Read (a Restricted admin key). Only costs are read: by
day, and by project so the person can choose Cora's own. The key is kept in the system
credential store; the database only keeps which project's spend to show.

Blocking calls: run them off the UI thread.
"""

from datetime import datetime, timedelta, timezone

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
