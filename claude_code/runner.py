"""Claude Code sessions Cora runs herself, through the Claude Agent SDK, on your Claude subscription.

One session at a time, in a project folder inside one of the folders you allowed in Settings. It runs
in Claude Code's auto mode, as your own sessions do by default: a classifier lets routine work through
(editing its files, running commands in its folder) and blocks what goes beyond the brief. What still
needs a person (an ask rule of yours, or auto mode pausing after repeated blocks, or isn't available,
when Claude Code asks about everything) comes to you as one of Cora's approval prompts: the desk's
while she's active, your Signal chat while she's in standby. It never uses an API key: with one in Cora's
environment it won't start, since Claude Code would bill that instead.

Its progress reaches the session tracker as the same events the hooks send, so the dashboard, Activity
and her claude_code tool see it whether or not the hooks are added.
"""

import asyncio
from collections.abc import Awaitable, Callable
import json
from contextvars import copy_context
from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
from pathlib import Path
import re

from services.confirmation import APPROVED, TIMED_OUT

from .sessions import short

# Environment variables that would make Claude Code bill an API account instead of your subscription.
API_KEY_VARIABLES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
# A new project's folder name: letters, digits, spaces, dots, dashes and underscores.
PROJECT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,59}$")
BRIEF_MAX_CHARS = 8000
# The most the saved folders may take (as the JSON list kept in app_settings).
FOLDERS_MAX_CHARS = 2000
INPUT_SHOWN_CHARS = 300
RESULT_MAX_CHARS = 1500
# Claude Code's auto mode: a classifier approves routine actions and blocks risky ones, so only what
# still needs a person reaches Cora's approval prompts. Without auto mode, Claude Code asks about everything.
PERMISSION_MODE = "auto"
# Asked of Claude in every session Cora runs, after its own instructions.
SYSTEM_APPEND = (
    "You were started by Cora, the person's assistant, on their behalf; they may not be watching. "
    "Don't wait for answers to questions: make sensible choices, note them, and keep going. "
    "Finish with a short plain summary of what you did and anything the person needs to do next."
)
ANSWER_LATER = (
    "The person isn't watching this session, so they can't answer now. Make a sensible choice, "
    "say which in your final summary, and carry on."
)
# What Claude is told when the person says no to a step, or doesn't answer in time.
DECLINED_STEP = ("The person didn't approve that. Find another way, or stop and explain what you needed in "
                 "your summary.")
UNANSWERED_STEP = ("The person didn't answer in time (they may be away). Find another way, or stop and explain "
                   "what you needed in your summary, so they can approve it when they continue the session.")


def subscription_problem() -> str | None:
    """Why Cora can't run Claude Code on the subscription now, or None when she can."""
    found = [name for name in API_KEY_VARIABLES if os.environ.get(name)]
    if found:
        return (f"{' and '.join(found)} is set in Cora's environment, so Claude Code would bill that instead of "
                "your subscription. Remove it from .env or your system settings first.")
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") and not CREDENTIALS_PATH.exists():
        return "Claude Code isn't signed in on this PC: run claude and /login, or claude setup-token."
    return None


def parse_folders(text: str) -> list[str]:
    """The allowed folders as typed: separated by ';' or new lines, blanks dropped, each once."""
    seen, folders = set(), []
    for part in re.split(r"[;\n]", text or ""):
        folder = part.strip().strip('"')
        if folder and folder.casefold() not in seen:
            seen.add(folder.casefold())
            folders.append(folder)
    return folders


def check_folders(folders: list[str]) -> list[str]:
    """The folders, each an existing absolute folder; ValueError naming the first that isn't."""
    if len(json.dumps(folders)) > FOLDERS_MAX_CHARS:
        raise ValueError("That's too many folders to keep; leave out a few.")
    for folder in folders:
        path = Path(folder)
        if not path.is_absolute() or not path.is_dir():
            raise ValueError(f"{folder} isn't a folder on this PC (give its full path).")
    return folders


def location_name(folder: str) -> str:
    return Path(folder).name or folder


def find_project(folders: list[str], project: str) -> Path | None:
    """An existing project: a folder of that name (any case) directly inside one of the allowed folders."""
    wanted = project.strip().casefold()
    for folder in folders:
        try:
            for child in Path(folder).iterdir():
                if child.is_dir() and child.name.casefold() == wanted:
                    return child
        except OSError:
            continue
    return None


def new_project(folders: list[str], project: str, location: str | None) -> Path:
    """Where a new project goes (not made yet); ValueError when the name or place won't do."""
    name = project.strip()
    if not PROJECT_NAME.match(name) or name in (".", ".."):
        raise ValueError("A new project's name can only use letters, numbers, spaces, dots, dashes and "
                         "underscores, up to 60 characters.")
    if not folders:
        raise ValueError("No projects folders are set up in Settings > Claude Code.")
    parent = folders[0]
    if location:
        matching = [folder for folder in folders if location_name(folder).casefold() == location.strip().casefold()]
        if not matching:
            raise ValueError(f"There's no projects folder called {location}; the folders are: "
                             f"{', '.join(location_name(folder) for folder in folders)}.")
        parent = matching[0]
    if find_project([parent], name) is not None:
        raise ValueError(f"There's already a project called {name} in {location_name(parent)}; continue it instead.")
    return Path(parent) / name


def shown_input(tool_input: dict) -> dict:
    """A tool call's input for the approval prompt: each value on one short line."""
    return {key: short(value if isinstance(value, str) else repr(value), INPUT_SHOWN_CHARS)
            for key, value in tool_input.items()} if isinstance(tool_input, dict) else {}


@dataclass
class Run:
    project: str
    folder: Path
    brief: str
    started_at: datetime
    session_id: str = ""
    last_text: str = ""
    task: asyncio.Task | None = None
    client: object = None
    stopping: bool = False
    stderr: list[str] = field(default_factory=list)


def keep_tail(lines: list[str], line: str, keep: int = 20) -> None:
    """Claude Code's last few lines of stderr, for saying why a session failed."""
    lines.append(line)
    del lines[:-keep]


def default_client(options):
    from claude_agent_sdk import ClaudeSDKClient
    return ClaudeSDKClient(options)


def default_options(**options):
    from claude_agent_sdk import ClaudeAgentOptions
    return ClaudeAgentOptions(**options)


class ClaudeCodeRunner:
    """Runs one Claude Code session at a time for Cora."""

    def __init__(
        self, *, ask_approval: Callable[[str, str, dict], Awaitable[str]],
        on_event: Callable[[dict], None], on_done: Callable[[Run, str, str], Awaitable[None]],
        write_activity: Callable[[str, str], None], schedule: Callable,
        client_factory: Callable = default_client, options_factory: Callable = default_options,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        # ask_approval(project, tool, shown input) -> APPROVED, DECLINED or TIMED_OUT (Cora's approval
        # prompts, wherever you are).
        self.ask_approval = ask_approval
        # Each step as a hook-style event, for the session tracker.
        self.on_event = on_event
        # on_done(run, state, text) once it ends: state is finished, failed or stopped.
        self.on_done = on_done
        self.write_activity = write_activity
        # schedule(coroutine) -> a Task, run outside any conversation's context.
        self.schedule = schedule
        self.client_factory = client_factory
        self.options_factory = options_factory
        self.clock = clock
        self.current: Run | None = None
        # The last session Cora ran in each project folder (this run of Cora only), to continue it.
        self.sessions: dict[str, str] = {}

    @property
    def busy(self) -> bool:
        return self.current is not None

    def start(self, project: str, folder: Path, brief: str, resume: str | None = None,
              fork: bool = False) -> Run:
        """Start a session in folder (made if new); RuntimeError while one is already running."""
        if self.current is not None:
            raise RuntimeError(f"Claude Code is already working on {self.current.project} for you.")
        run = Run(project, folder, brief, self.clock())
        self.current = run
        # Outside the conversation that asked: approvals come to the desk, not to a chat that has moved on.
        run.task = self.schedule(self.drive(run, resume, fork), context=copy_context_without_route())
        return run

    async def stop(self) -> bool:
        """Interrupt the running session; False when none is running."""
        run = self.current
        if run is None:
            return False
        run.stopping = True
        try:
            if run.client is not None:
                await asyncio.wait_for(run.client.interrupt(), 10)
        except Exception:
            if run.task is not None:
                run.task.cancel()
        return True

    def options(self, run: Run, resume: str | None, fork: bool):
        return self.options_factory(
            cwd=str(run.folder), permission_mode=PERMISSION_MODE, can_use_tool=self.permit_for(run),
            resume=resume, fork_session=bool(resume and fork), setting_sources=["user", "project", "local"],
            system_prompt={"type": "preset", "preset": "claude_code", "append": SYSTEM_APPEND},
            stderr=lambda line: keep_tail(run.stderr, line),
        )

    def permit_for(self, run: Run):
        async def permit(tool_name: str, tool_input: dict, context):
            from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
            if tool_name == "AskUserQuestion":
                return PermissionResultDeny(message=ANSWER_LATER)
            outcome = await self.ask_approval(run.project, tool_name, shown_input(tool_input))
            if outcome == APPROVED:
                return PermissionResultAllow()
            return PermissionResultDeny(message=UNANSWERED_STEP if outcome == TIMED_OUT else DECLINED_STEP)
        return permit

    def event(self, run: Run, name: str, **fields) -> None:
        if run.session_id:
            self.on_event({"hook_event_name": name, "session_id": run.session_id, "cwd": str(run.folder), **fields})

    async def drive(self, run: Run, resume: str | None, fork: bool) -> None:
        from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock
        state, text = "failed", ""
        try:
            run.folder.mkdir(parents=False, exist_ok=True)
            client = self.client_factory(self.options(run, resume, fork))
            run.client = client
            async with client:
                await client.query(run.brief)
                async for message in client.receive_response():
                    if isinstance(message, SystemMessage) and message.subtype == "init":
                        run.session_id = str(message.data.get("session_id") or "")
                        self.sessions[str(run.folder).casefold()] = run.session_id
                        self.event(run, "UserPromptSubmit", prompt=run.brief)
                    elif isinstance(message, AssistantMessage) and message.parent_tool_use_id is None:
                        said = " ".join(block.text for block in message.content if isinstance(block, TextBlock))
                        if said.strip():
                            run.last_text = said
                    elif isinstance(message, ResultMessage):
                        run.session_id = run.session_id or message.session_id
                        self.sessions[str(run.folder).casefold()] = run.session_id
                        text = message.result or run.last_text
                        state = "failed" if message.is_error else "finished"
            if run.stopping:
                state, text = "stopped", run.last_text
        except asyncio.CancelledError:
            state, text = "stopped", run.last_text
        except Exception as error:
            detail = " ".join(run.stderr[-3:]).strip()
            text = f"{type(error).__name__}: {short(str(error) or detail, 300)}"
            self.write_activity(f"Claude Code in {run.project} stopped with an error ({type(error).__name__}).",
                                "Claude Code error")
        finally:
            self.current = None
        text = short(text, RESULT_MAX_CHARS) if text else ""
        if state == "finished":
            self.event(run, "Stop", last_assistant_message=text)
        elif state == "failed":
            self.event(run, "StopFailure", error=text or "it stopped")
        else:
            self.event(run, "Stop", last_assistant_message=f"Stopped: {text}" if text else "Stopped.")
        await self.on_done(run, state, text)


def copy_context_without_route():
    """This context, but not in any conversation, so approvals go to the desk's pop-up."""
    from tools.route import current_route
    context = copy_context()
    context.run(current_route.set, None)
    return context
