"""The last few exchanges of a Claude Code session, from its transcript, for Cora to read.

Only with Settings > Claude Code > "Let her read transcripts" on: what's read here goes to Cora's
model. Only files in Claude Code's own projects folder are read, and only their end. Your words,
Claude's replies and a short note of each tool it used; never its thinking or the tools' output.
"""

import json
from pathlib import Path

from .sessions import short

PROJECTS_DIR = Path.home() / ".claude" / "projects"
# How much of the file's end is read: plenty for the last few exchanges, never the whole of a long one.
TAIL_BYTES = 1024 * 1024
EXCHANGES_MAX = 12
ENTRY_MAX_CHARS = 600
TOTAL_MAX_CHARS = 6000
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


def entry_texts(entry: dict) -> list[tuple[str, str, bool]]:
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
                found.append((who, short(text, ENTRY_MAX_CHARS), False))
        elif block.get("type") == "tool_use" and who == "claude":
            found.append((who, tool_note(block), True))
    return found


def recent_exchanges(path: str, projects: Path = PROJECTS_DIR) -> list[dict] | None:
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
        for who, text, is_tool in reversed(entry_texts(entry)):
            if is_tool and picked and picked[-1]["tools"]:
                picked[-1]["tools"] += 1
                continue
            if len(picked) >= EXCHANGES_MAX or total + len(text) > TOTAL_MAX_CHARS:
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
