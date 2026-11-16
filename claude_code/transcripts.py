"""The last few exchanges of a Claude Code session, from its transcript: for Cora to read, and for
you to follow a session in its pop-up.

For Cora only with Settings > Claude Code > "Let her read transcripts" on: what she reads goes to her
model. The pop-up only shows it to you, on this PC. Only files in Claude Code's own projects folder
are read, and only their end. Your words, Claude's replies and a short note of each tool it used;
never its thinking or the tools' output.
"""

import json
from pathlib import Path
import re

from .sessions import short

PROJECTS_DIR = Path.home() / ".claude" / "projects"
# How much of the file's end is read: plenty for the last few exchanges, never the whole of a long one.
TAIL_BYTES = 1024 * 1024
EXCHANGES_MAX = 12
ENTRY_MAX_CHARS = 600
TOTAL_MAX_CHARS = 6000
# How much the session pop-up shows: more than Cora reads, since it's only for you.
SHOWN_EXCHANGES_MAX = 40
SHOWN_ENTRY_MAX_CHARS = 2000
SHOWN_TOTAL_MAX_CHARS = 30000
SESSION_ID = re.compile(r"^[A-Za-z0-9-]{1,100}$")
# Of a tool's input, the part that says what it did.
TOOL_FIELDS = ("command", "file_path", "path", "pattern", "url", "query", "description", "prompt")


def transcript_file(path: str, projects: Path = PROJECTS_DIR) -> Path | None:
    """The transcript, if it's a .jsonl file inside Claude Code's projects folder; else None."""
    try:
        resolved = Path(path).resolve(strict=True)
        root = projects.resolve(strict=True)
    except (OSError, ValueError):
        return None
    if resolved.suffix != ".jsonl" or not resolved.is_file() or not resolved.is_relative_to(root):
        return None
    return resolved


def session_transcript(session_id: str, transcript_path: str = "", projects: Path = PROJECTS_DIR) -> str | None:
    """A session's transcript: the path its hooks gave, or else the file named after it in Claude
    Code's projects folder (sessions Cora runs report no path). None when there's neither."""
    if transcript_path:
        return transcript_path
    if not SESSION_ID.match(session_id or ""):
        return None
    try:
        found = next(projects.glob(f"*/{session_id}.jsonl"), None)
    except OSError:
        return None
    return str(found) if found is not None else None


def tail_lines(path: Path, limit: int = TAIL_BYTES) -> list[str]:
    with path.open("rb") as file:
        size = file.seek(0, 2)
        file.seek(max(0, size - limit))
        data = file.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    # The first line is likely cut part way unless the whole file was read.
    return lines if size <= limit else lines[1:]


def tool_note(block: dict) -> str:
    name = block.get("name") if isinstance(block.get("name"), str) else "a tool"
    tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
    what = next((tool_input[field] for field in TOOL_FIELDS if isinstance(tool_input.get(field), str)), "")
    return f"used {name}: {short(what, 200)}" if what else f"used {name}"


def entry_texts(entry: dict, entry_max: int = ENTRY_MAX_CHARS) -> list[tuple[str, str, bool]]:
    """(who, text, is a tool note) for what one transcript line says, who being 'you' or 'claude'."""
    if not isinstance(entry, dict) or entry.get("type") not in ("user", "assistant") \
            or entry.get("isSidechain") or entry.get("isMeta"):
        return []
    message = entry.get("message")
    if not isinstance(message, dict):
        return []
    who = "you" if entry["type"] == "user" else "claude"
    content = message.get("content")
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
    if not isinstance(blocks, list):
        return []
    found = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            text = block["text"].strip()
            # Claude Code's own wrappers (reminders, command output) aren't what either of you said.
            if text and not text.startswith("<"):
                found.append((who, short(text, entry_max), False))
        elif block.get("type") == "tool_use" and who == "claude":
            found.append((who, tool_note(block), True))
    return found


def recent_exchanges(path: str, projects: Path = PROJECTS_DIR, most: int = EXCHANGES_MAX,
                     total_max: int = TOTAL_MAX_CHARS, entry_max: int = ENTRY_MAX_CHARS) -> list[dict] | None:
    """The newest exchanges, oldest first, within the size limits; None if it can't be read."""
    transcript = transcript_file(path, projects)
    if transcript is None:
        return None
    try:
        lines = tail_lines(transcript)
    except OSError:
        return None
    # Newest first while reading back; a run of tool uses is one line, its newest said in full.
    picked, total = [], 0
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        for who, text, is_tool in reversed(entry_texts(entry, entry_max)):
            if is_tool and picked and picked[-1]["tools"]:
                picked[-1]["tools"] += 1
                continue
            if len(picked) >= most or total + len(text) > total_max:
                return exchanges(picked)
            picked.append({"who": who, "said": text, "tools": 1 if is_tool else 0})
            total += len(text)
    return exchanges(picked)


def exchanges(picked: list[dict]) -> list[dict]:
    """Oldest first, a run of tool uses saying how many came before its newest."""
    found = []
    for item in reversed(picked):
        said = item["said"]
        if item["tools"] > 1:
            said += f" (after {item['tools'] - 1} other tool use{'' if item['tools'] == 2 else 's'})"
        found.append({"who": item["who"], "said": said})
    return found
