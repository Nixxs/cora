"""Cora starting, continuing or stopping a Claude Code session for the person (see claude_code/runner.py).

Offered while Settings > Claude Code lets her run sessions. The person's own chats only: clients'
chats and task work offer allowlists without it. Starting or continuing one asks the person first.
"""

import json

SCHEMA = {
    "type": "function",
    "name": "run_claude_code",
    "description": (
        "Have Claude Code (an AI coding assistant) build or change something for the person in one of their "
        "project folders: a new project (an app, a script, a World of Warcraft addon) or work on an "
        "existing one. It runs in the background on their Claude subscription and can take many minutes; "
        "you're told when it finishes, and its progress shows in the claude_code tool. One session at a "
        "time. Only when the person asks for it. Before starting, be sure what they want: write the brief "
        "as a clear request to Claude in the person's own terms (what to build, how it should behave, "
        "anything they mentioned), never secrets or passwords. The person approves each start. "
        "action: 'start' a session (a new project with create true, or an existing project folder), "
        "'continue' the last session in that project with a follow-up brief, or 'stop' the one running."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["start", "continue", "stop"]},
            "project": {
                "type": ["string", "null"],
                "description": "The project's folder name, such as 'MountTracker'; null only to stop.",
            },
            "create": {
                "type": "boolean",
                "description": "True to make a new project folder of that name (start only).",
            },
            "location": {
                "type": ["string", "null"],
                "description": ("For a new project, which of the person's projects folders to make it in, by "
                                "name (such as 'AddOns' for a WoW addon); null for the first."),
            },
            "brief": {
                "type": ["string", "null"],
                "description": "What to ask Claude to do (start and continue); null to stop.",
            },
        },
        "required": ["action", "project", "create", "location", "brief"],
        "additionalProperties": False,
    },
    "strict": True,
}

ACTIONS = ("start", "continue", "stop")
NAME_MAX_CHARS = 100
BRIEF_MAX_CHARS = 8000
ERROR_RESULT = {"status": "error", "message": "Claude Code couldn't be run just now; say so rather than guessing."}


def validate_arguments(arguments: dict) -> None:
    if not isinstance(arguments, dict) or set(arguments) != {"action", "project", "create", "location", "brief"}:
        raise ValueError("run_claude_code requires exactly action, project, create, location and brief")
    if arguments["action"] not in ACTIONS:
        raise ValueError("action must be start, continue or stop")
    if not isinstance(arguments["create"], bool):
        raise ValueError("create must be true or false")
    for name, limit in (("project", NAME_MAX_CHARS), ("location", NAME_MAX_CHARS), ("brief", BRIEF_MAX_CHARS)):
        value = arguments[name]
        if value is not None and (not isinstance(value, str) or len(value) > limit):
            raise ValueError(f"{name} must be text of at most {limit} characters, or null")
    if arguments["action"] != "stop":
        if not (arguments["project"] or "").strip():
            raise ValueError("start and continue need a project")
        if not (arguments["brief"] or "").strip():
            raise ValueError("start and continue need a brief")


class RunClaudeCodeTool:
    def __init__(self, get_connection, is_closing, run, write_debug):
        # run(action, project, create, location, brief) -> the result for Cora (see ClaudeCodeMixin.run_claude_code).
        self.get_connection = get_connection
        self.is_closing = is_closing
        self.run = run
        self.write_debug = write_debug

    def _stale(self, connection) -> bool:
        return self.is_closing() or self.get_connection() is not connection

    async def handle(self, call_id: str, arguments: dict) -> None:
        try:
            if self.is_closing():
                return
            connection = self.get_connection()
            if connection is None:
                return
            validate_arguments(arguments)
            try:
                result = await self.run(
                    arguments["action"], (arguments["project"] or "").strip() or None, arguments["create"],
                    (arguments["location"] or "").strip() or None, (arguments["brief"] or "").strip() or None,
                )
            except Exception as error:
                self.write_debug(f"Running Claude Code failed ({type(error).__name__})", "Tool error")
                result = ERROR_RESULT
            if self._stale(connection):
                return
            await connection.response.item.create(item={
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps(result, ensure_ascii=False),
            })
            if self._stale(connection):
                return
            await connection.response.create()
        except Exception as error:
            self.write_debug(f"run_claude_code failed ({type(error).__name__})", "Tool error")
