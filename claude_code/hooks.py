"""Cora's hooks in your Claude Code settings, and the token they carry.

The hooks go in your user settings (~/.claude/settings.json), so they cover every project and
Claude Code wherever it runs: the terminal, VS Code and the desktop app. Each is a command hook run
in the background (async): curl posts the event to Cora and Claude never waits for it, and when
Cora isn't running nothing happens.

One isn't in the background: the PermissionRequest hook waits for Cora's answer, so that while she's
in standby she can ask you on Signal. Mostly she answers at once with nothing, and Claude Code asks
you itself; when Cora isn't running, curl fails and the hook lets the usual prompt through
(onFailure "continue"), never blocking the call.

Adding or removing them changes only Cora's own entries (those posting to /claude-code/...) and
keeps a copy of the file as it was.

The token lives in the system credential store and, because a hook needs it, in that settings file.
"""

import copy
import json
import os
from pathlib import Path
import re
import secrets

import keyring

from .routes import EVENT_PATH, PERMISSION_PATH

KEYRING_SERVICE = "cora"
TOKEN_ACCOUNT = "claude-code:hook-token"
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
BACKUP_SUFFIX = ".cora-backup"
# PostToolUse is how Cora knows a session needing you has been answered: Claude carried on.
EVENTS = ("SessionStart", "UserPromptSubmit", "Notification", "PostToolUse", "Stop", "StopFailure", "SessionEnd")
HOOK_TIMEOUT_SECONDS = 10
# The hook that waits for Cora's answer, and how long it may: her Signal ask waits up to 10 minutes.
PERMISSION_EVENT = "PermissionRequest"
PERMISSION_CURL_SECONDS = 650
PERMISSION_HOOK_SECONDS = 660
CORA_PATHS = (EVENT_PATH, PERMISSION_PATH)
CORA_HOOK = re.compile(r"Bearer ([A-Za-z0-9_-]+)\".*http://127\.0\.0\.1:(\d+)(/claude-code/[a-z]+)")

CURRENT = "current"
MISSING = "missing"
OUTDATED = "outdated"


def hook_token(rotate: bool = False) -> str:
    """Made on first use, or a new one when rotating. Blocking (credential store)."""
    token = None if rotate else keyring.get_password(KEYRING_SERVICE, TOKEN_ACCOUNT)
    if not token:
        token = secrets.token_urlsafe(32)
        keyring.set_password(KEYRING_SERVICE, TOKEN_ACCOUNT, token)
    return token


def hook_command(port: int, token: str, path: str = EVENT_PATH, seconds: int = 5) -> str:
    """curl.exe on Windows, so PowerShell's own curl (Invoke-WebRequest) never stands in for it."""
    curl = "curl.exe" if os.name == "nt" else "curl"
    return (f'{curl} -s -m {seconds} -H "Content-Type: application/json" -H "Authorization: Bearer {token}" '
            f'--data-binary "@-" http://127.0.0.1:{port}{path}')


def is_cora_hook(hook) -> bool:
    command = str(hook.get("command", "")) if isinstance(hook, dict) else ""
    return any(path in command for path in CORA_PATHS)


def cora_hooks(port: int, token: str) -> dict[str, dict]:
    """Each event's hook, as Cora adds it."""
    background = {"type": "command", "command": hook_command(port, token), "async": True,
                  "timeout": HOOK_TIMEOUT_SECONDS}
    found = {event: dict(background) for event in EVENTS}
    found[PERMISSION_EVENT] = {
        "type": "command", "command": hook_command(port, token, PERMISSION_PATH, PERMISSION_CURL_SECONDS),
        "timeout": PERMISSION_HOOK_SECONDS, "onFailure": "continue",
    }
    return found


def read_settings(path: Path = SETTINGS_PATH) -> dict:
    """The settings as saved ({} when there's no file); ValueError when they can't be read safely."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        settings = json.loads(text) if text.strip() else {}
    except ValueError:
        raise ValueError(f"{path} isn't valid JSON; it was left alone") from None
    if not isinstance(settings, dict) or not isinstance(settings.get("hooks", {}), dict):
        raise ValueError(f"{path} isn't laid out as expected; it was left alone")
    return settings


def write_settings(settings: dict, path: Path = SETTINGS_PATH) -> None:
    """A copy of the file as it was beside it, then the new one written whole."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.with_name(path.name + BACKUP_SUFFIX).write_bytes(path.read_bytes())
    temporary = path.with_name(path.name + ".cora-new")
    temporary.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def without_cora(settings: dict) -> dict:
    """The settings with Cora's hooks taken out, and anything left empty by that removed."""
    settings = copy.deepcopy(settings)
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept = []
        for group in groups:
            if isinstance(group, dict) and isinstance(group.get("hooks"), list) \
                    and any(is_cora_hook(hook) for hook in group["hooks"]):
                group["hooks"] = [hook for hook in group["hooks"] if not is_cora_hook(hook)]
                if not group["hooks"]:
                    continue
            kept.append(group)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        del settings["hooks"]
    return settings


def with_cora(settings: dict, port: int, token: str) -> dict:
    """The settings with Cora's hooks, for this port and token, in place of any she had."""
    settings = without_cora(settings)
    hooks = settings.setdefault("hooks", {})
    for event, hook in cora_hooks(port, token).items():
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise ValueError(f"The {event} hooks in Claude Code's settings aren't laid out as expected")
        groups.append({"hooks": [hook]})
    return settings


def hooks_state(settings: dict, port: int, token: str) -> str:
    """CURRENT when every event has Cora's hook as she'd add it now (this port and token), MISSING when
    none has one, else OUTDATED."""
    hooks = settings.get("hooks") if isinstance(settings.get("hooks"), dict) else {}
    wanted = cora_hooks(port, token)
    found = 0
    for event, hook in wanted.items():
        groups = hooks.get(event, []) if isinstance(hooks.get(event), list) else []
        present = [each for group in groups if isinstance(group, dict) and isinstance(group.get("hooks"), list)
                   for each in group["hooks"] if is_cora_hook(each)]
        if not present:
            continue
        found += 1
        match = CORA_HOOK.search(str(present[0].get("command", "")))
        if len(present) > 1 or match is None or match.group(1) != token or int(match.group(2)) != port                 or present[0] != hook:
            return OUTDATED
    if found == 0:
        return MISSING
    return CURRENT if found == len(wanted) else OUTDATED
