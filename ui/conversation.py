"""Conversation history, transcript grouping, and written-answer display."""

from textual.widgets import RichLog

from .chat_widgets import ChatMarkdown


class Conversation:
    def __init__(self, output: RichLog, set_timer, write_debug, record_message=None):
        self.output = output
        self.set_timer = set_timer
        self.write_debug = write_debug
        self.record_message = record_message
        self.transcript_speaker = None
        self.transcript_text = ""
        self.transcript_idle_timer = None
        self.active_transcript_index = None
        self.log_entries = []
        self.written_item_ids = set()

    def get_reconnect_history(self) -> list[dict]:
        """Bounded visible chat, not tool calls, internal reasoning, or unsent drafts."""
        history = []
        remaining = 6000
        for entry in reversed(self.log_entries):
            if isinstance(entry, ChatMarkdown):
                role, text = "assistant", entry.markup
            elif isinstance(entry, str) and entry.startswith("You: "):
                role, text = "user", entry[5:]
            elif isinstance(entry, str) and entry.startswith("Cora: "):
                role, text = "assistant", entry[6:]
            else:
                continue
            if not text.strip():
                continue
            # A UTF-8 byte cap is a conservative token budget, including non-ASCII text.
            text = text.encode("utf-8")[-remaining:].decode("utf-8", errors="ignore")
            if not text:
                continue
            history.append({
                "type": "message", "role": role,
                "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}],
            })
            remaining -= len(text.encode("utf-8"))
            if remaining == 0 or len(history) == 40:
                break
        return list(reversed(history))

    def write_log(self, message: str) -> None:
        self.flush_transcript()
        self.log_entries.append(message)
        self.output.write(message)

    def append_transcript(self, speaker: str, delta: str) -> None:
        if not delta:
            return
        if speaker != self.transcript_speaker:
            self.flush_transcript()

        if self.active_transcript_index is None:
            self.transcript_speaker = speaker
            self.log_entries.append("")
            self.active_transcript_index = len(self.log_entries) - 1

        # Keep the spacing supplied by Live; fragments can split inside words.
        self.transcript_text += delta
        self.log_entries[self.active_transcript_index] = (
            f"{speaker}: {self.transcript_text.strip()}"
        )
        self.redraw_log()

        if self.transcript_idle_timer is not None:
            self.transcript_idle_timer.stop()
        # A pause groups text for display; it is not an API turn boundary.
        self.transcript_idle_timer = self.set_timer(2.0, self.flush_transcript)

    def flush_transcript(self) -> None:
        if self.transcript_idle_timer is not None:
            self.transcript_idle_timer.stop()
            self.transcript_idle_timer = None

        had_active_transcript = self.active_transcript_index is not None
        speaker = self.transcript_speaker
        text = self.transcript_text.strip()
        if had_active_transcript and self.log_entries[-1] != "":
            self.log_entries.append("")

        self.transcript_text = ""
        self.transcript_speaker = None
        self.active_transcript_index = None

        if had_active_transcript:
            self.redraw_log()
            if text and self.record_message is not None:
                self.record_message("user" if speaker == "You" else "assistant", text, "voice")

    def redraw_log(self) -> None:
        # RichLog.write adds lines, so redraw to update the active message.
        self.output.clear()
        for entry in self.log_entries:
            self.output.write(entry, scroll_end=False)
        self.output.scroll_end(animate=False)

    def display_written_item(self, item: dict) -> None:
        item_id = item.get("id")
        if not item_id or item_id in self.written_item_ids:
            return

        # Read the backend's original answer, not the spoken transcript.
        # Ignore search/tool output, reasoning, and intermediate commentary.
        if item.get("type") != "message" or item.get("role") != "assistant":
            return
        if item.get("status") != "completed" or item.get("phase") == "commentary":
            return
        parts = []
        for content in item.get("content") or []:
            if content.get("type") == "output_text":
                parts.append(content.get("text") or "")
            elif content.get("type") == "refusal":
                parts.append(content.get("refusal") or "")

        text = "\n\n".join(parts)
        if not text.strip():
            self.write_debug(
                f"Completed backend message has no written text: item_id={item_id}",
                "No screen content",
            )
            return

        # Plain conversational answers belong in Live's spoken transcript.
        # Other written deliverables opt in with a leading backend marker.
        screen_marker = "<!-- cora:screen -->"
        screen_requested = text.lstrip().startswith(screen_marker)
        if screen_requested:
            text = text.lstrip()[len(screen_marker):].lstrip("\r\n")
        written = ChatMarkdown(text, code_theme="monokai")
        has_visual_content = any(
            token.type in ("fence", "code_block", "table_open")
            for token in written.parsed
        )
        if not text.strip() or not (screen_requested or has_visual_content):
            return

        self.written_item_ids.add(item_id)
        # Keep renderables in history so transcript redraws preserve formatting.
        # Do not end an active spoken/user transcript when a result arrives.
        self.log_entries.extend([
            "",
            "Cora · on screen:",
            written,
            "",
        ])
        self.redraw_log()
        self.write_debug(
            f"Written answer displayed: item_id={item_id}, characters={len(text)}",
            "Written answer displayed",
        )
        if self.record_message is not None:
            self.record_message("assistant", text, "screen")