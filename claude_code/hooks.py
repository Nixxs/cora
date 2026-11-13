"""Cora's hooks in your Claude Code settings, and the token they carry.

The hooks go in your user settings (~/.claude/settings.json), so they cover every project and
Claude Code wherever it runs: the terminal, VS Code and the desktop app. Each is a command hook run
in the background (async): curl posts the event to Cora and Claude never waits for it, and when
Cora isn't running nothing happens. Adding or removing them changes only Cora's own entries (those
posting to /claude-code/event) and keeps a copy of the file as it was.

The token lives in the system credential store and, because a hook needs it, in that settings file.
"""

import copy
import json
import os
from pathlib import Path
import re
import secrets

import keyring

from .routes import EVENT_PATH

KEYRING_SERVICE = "cora"
TOKEN_ACCOUNT = "claude-code:hook-token"
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
BACKUP_SUFFIX = ".cora-backup"
# PostToolUse is how Cora knows a session needing you has been answered: Claude carried on.
EVENTS = ("SessionStart", "UserPromptSubmit", "Notification", "PostToolUse", "Stop", "StopFailure", "SessionEnd")
HOOK_TIMEOUT_SECONDS = 10
CORA_HOOK = re.compile(r"Bearer ([A-Za-z0-9_-]+)\".*http://127\.0\.0\.1:(\d+)" + re.escape(EVENT_PATH))

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


def hook_command(port: int, token: str) -> str:
    """curl.exe on Windows, so PowerShell's own curl (Invoke-WebRequest) never stands in for it."""
    curl = "curl.exe" if os.name == "nt" else "curl"
    return (f'{curl} -s -m 5 -H "Content-Type: application/json" -H "Authorization: Bearer {token}" '
            f'--data-binary "@-" http://127.0.0.1:{port}{EVENT_PATH}')


def is_cora_hook(hook) -> bool:
    return isinstance(hook, dict) and EVENT_PATH in str(hook.get("command", ""))


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
    command = {"type": "command", "command": hook_command(port, token), "async": True,
               "timeout": HOOK_TIMEOUT_SECONDS}
    for event in EVENTS:
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise ValueError(f"The {event} hooks in Claude Code's settings aren't laid out as expected")
        groups.append({"hooks": [dict(command)]})
    return settings


def hooks_state(settings: dict, port: int, token: str) -> str:
    """CURRENT when every event posts to this port with this token, MISSING when none do, else OUTDATED."""
    hooks = settings.get("hooks") if isinstance(settings.get("hooks"), dict) else {}
    found = 0
    for event in EVENTS:
        commands = [str(hook.get("command", "")) for group in hooks.get(event, []) if isinstance(group, dict)
                    for hook in group.get("hooks", []) if is_cora_hook(hook)]
        if not commands:
            continue
        found += 1
        match = CORA_HOOK.search(commands[0])
        if len(commands) > 1 or match is None or match.group(1) != token or int(match.group(2)) != port:
            return OUTDATED
    if found == 0:
        return MISSING
    return CURRENT if found == len(EVENTS) else OUTDATED
