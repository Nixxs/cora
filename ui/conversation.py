"""Conversation history, transcript grouping, and written-answer display."""

import io
import re
from urllib.parse import urlsplit

from PIL import Image

from rich.text import Text
from textual.widgets import RichLog

from .chat_widgets import ChatImage, ChatMarkdown, chat_link

# How the person's own Signal messages appear in the chat.
SIGNAL_PREFIX = "You · Signal: "
# And the emails they send her own address.
EMAIL_PREFIX = "You · Email: "
# And what they write on her web page, with the device it came from when known.
WEB_PREFIX = "You · web: "
WEB_LINE = re.compile(r"You · web(?: · [^:\n]*)?: ")
# How a request for approval ended, wherever it was asked (desk, Signal, the web page). Shown in the
# chat and given to Cora as history, so later turns know what was done even if her reply didn't say.
APPROVAL_PREFIX = "Cora · approval: "
APPROVAL_SHOWN = {"approved": "you approved {}", "declined": "you declined {}",
                  "timed out": "no answer in time for {}: not done"}
APPROVAL_FOR_CORA = {"approved": "You approved my request to use {}.", "declined": "You declined my request to use {}.",
                     "timed out": "You didn't answer my request to use {} in time, so it wasn't done."}

# Who is speaking, coloured so messages tell apart at a glance; the name stays as text too.
SPEAKER_STYLES = {"Cora": "bold #7fbf73", "You": "bold #7fb8d8", "Rocky": "bold #d7a06a"}
# A message's label, like "Cora:", "You · Signal:", "Cora · on screen:" or "Rocky:".
SPEAKER_LABEL = re.compile(r"(You|Cora|Rocky)( · [^:\n]+)?:")
# What Cora asked Rocky, her consultant, shown above his answer.
ROCKY_ASKED = "Cora · to Rocky: "
# What the person asked him directly (@rocky), and his answer while it's coming.
YOU_TO_ROCKY = "You · to Rocky"
ROCKY_THINKING = "Rocky · thinking…:"


class RockyAnswer(ChatMarkdown):
    """What Rocky said, shown as he said it; in the history later turns are given as hers to know,
    with what she asked him. Not saved: her reply says what she made of it."""

    def __init__(self, question: str, answer: str) -> None:
        super().__init__(answer, code_theme="monokai")
        self.for_cora = f"(I asked Rocky, my consultant: {question}\nHe said: {answer})"


# How much of the conversation Rocky is given in a group chat.
GROUP_TRANSCRIPT_CHARS = 6000
# The italic line under his answers saying what he looked up (rocky_browsing.looked_line).
LOOKED_UNDER = re.compile(r"\s*\*(?:From memory:|He searched|He read)[^\n]*\*\s*$")


def person_line(entry: str) -> str | None:
    """What the person said, from a chat line of theirs (typed, spoken, on Signal, by email or on
    the web page), or None for any other line."""
    if entry.startswith("You: "):
        return entry[5:]
    for prefix in (SIGNAL_PREFIX, EMAIL_PREFIX):
        if entry.startswith(prefix):
            return entry[len(prefix):]
    if web := WEB_LINE.match(entry):
        return entry[web.end():]
    return None


class GroupRockyAnswer(RockyAnswer):
    """What Rocky said in a group chat, answering the person before Cora does: part of her
    conversation (her later turns are given it), but not saved."""

    def __init__(self, answer: str) -> None:
        ChatMarkdown.__init__(self, answer, code_theme="monokai")
        self.for_cora = f"(Rocky, in our group chat, said: {answer})"


class DirectRockyAnswer(ChatMarkdown):
    """What Rocky said to the person directly (@rocky): shown only, never saved or given to Cora, so
    neither the question nor his answer reaches OpenAI."""


class DirectRockyDraft:
    """The person's question to Rocky (or, in a group chat, just their own message above it), then
    "thinking…" until his answer, or why there isn't one, takes its place. Entries are only ever
    appended, so its place stays right meanwhile. answer(text) is what his answer is shown as:
    private to the person (DirectRockyAnswer), or part of Cora's conversation (GroupRockyAnswer)."""

    def __init__(self, conversation: "Conversation", index: int,
                 answer=lambda text: DirectRockyAnswer(text, code_theme="monokai")) -> None:
        self.conversation = conversation
        self.index = index
        self.answer = answer

    def update(self, so_far: str) -> None:
        """His answer so far, as it streams in (a direct one: still never part of her history)."""
        if not so_far.strip():
            return
        entries = self.conversation.log_entries
        entries[self.index] = "Rocky:"
        entries[self.index + 1] = self.answer(so_far)
        self.conversation.schedule_redraw()

    def looking(self, what: str) -> None:
        """What he's doing on the web ('searching for "…"') in place of his answer, which starts
        again from nothing when he writes."""
        entries = self.conversation.log_entries
        entries[self.index] = f"Rocky · {what.replace(':', '')}…:"
        entries[self.index + 1] = ""
        self.conversation.schedule_redraw()

    def finish(self, answer: str) -> None:
        entries = self.conversation.log_entries
        entries[self.index] = "Rocky:"
        entries[self.index + 1] = self.answer(answer)
        self.conversation.redraw_log()

    def fail(self, why: str) -> None:
        """Why there's no answer, in its place (anything that had streamed in is taken away)."""
        self.conversation.log_entries[self.index] = QuietNote(f"· {why}")
        self.conversation.log_entries[self.index + 1] = ""
        self.conversation.redraw_log()


class ApprovalNote(str):
    """A chat line saying how a request for approval ended, carrying what Cora is told of it."""

    def __new__(cls, shown: str, for_cora: str):
        note = super().__new__(cls, shown)
        note.for_cora = for_cora
        return note


class PhotoNote(ApprovalNote):
    """A chat line under the person's photo: its description and id, given to Cora in later turns."""


class QuietNote(str):
    """A quiet line in the chat about what happened (such as "Cora checked where you are"): shown
    only, never saved or given to Cora as history."""


# A kept photo's id, at the start of its description (messaging/images.py photo_described).
PHOTO_ID = re.compile(r"Photo (img\d+) ")
# How much of a photo's description the chat line shows; Cora is given all of it.
PHOTO_NOTE_SHOWN_CHARS = 140


def styled_entry(entry):
    """A chat line with its speaker's label coloured; anything else is drawn as it is."""
    if isinstance(entry, (ApprovalNote, QuietNote)):
        # Quieter than something said: it's what happened, not a message.
        return Text(str(entry), style="italic #8f98a8")
    if not isinstance(entry, str):
        return entry
    label = SPEAKER_LABEL.match(entry)
    if label is None:
        return entry
    return Text.assemble((label.group(), SPEAKER_STYLES[label.group(1)]), entry[label.end():])


# A written reply still arriving, and one that stopped before it finished.
WRITING_LABEL = "Cora · writing…"
INCOMPLETE_SUFFIX = " · incomplete"
# Streamed text redraws the chat at most this often, not once per fragment.
STREAM_REDRAW_SECONDS = 0.05


class WrittenDraft:
    """A written reply shown as it streams in: one message, updated in place, then finished or marked incomplete."""

    def __init__(self, conversation: "Conversation", label: str, channel: str) -> None:
        self.conversation = conversation
        self.label = label
        self.channel = channel
        self.text = ""
        # Where its label line sits; None until there is text to show. Entries are only ever
        # appended, so this stays right while other messages arrive.
        self.index = None
        self.done = False

    def update(self, text: str) -> None:
        if self.done or (self.index is None and not text.strip()):
            return
        self.text = text
        entries = self.conversation.log_entries
        body = ChatMarkdown(text, code_theme="monokai")
        if self.index is None:
            entries.extend(["", f"{WRITING_LABEL}:", body, ""])
            self.index = len(entries) - 3
        else:
            entries[self.index + 1] = body
        self.conversation.schedule_redraw()

    def finish(self, text: str) -> None:
        """The whole reply: shown as its final Markdown, in place of what streamed."""
        if self.done:
            return
        self.done = True
        if self.index is None:
            self.conversation.show_written_reply(text, label=self.label, channel=self.channel)
            return
        entries = self.conversation.log_entries
        entries[self.index] = f"{self.label}:"
        entries[self.index + 1] = ChatMarkdown(text, code_theme="monokai")
        self.conversation.redraw_log()
        self.conversation.record("assistant", text, self.channel)

    def fail(self) -> None:
        """It stopped early: what was shown stays, marked incomplete, and is not saved as her reply."""
        if self.done:
            return
        self.done = True
        if self.index is not None:
            self.conversation.log_entries[self.index] = f"{self.label}{INCOMPLETE_SUFFIX}:"
            self.conversation.redraw_log()


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
        # Extra logs, such as the dashboard's chat box, that show the same conversation.
        self.mirrors = []
        # A redraw waiting to show streamed text.
        self.redraw_timer = None
        # Told about every message said in this conversation, as (role, text, channel): the web
        # page's feed. Called after the message is recorded.
        self.listeners: list = []
        # Told the id of each of the person's kept photos as its note is shown (photo_listeners), and
        # each picture she shows as (image, caption, page_url) (picture_listeners): the web page
        # shows them, from memory.
        self.photo_listeners: list = []
        self.picture_listeners: list = []

    def record(self, role: str, text: str, channel: str, device: str | None = None,
               announce: bool = True) -> None:
        """A message said in this conversation: saved, and passed on to listeners (unless announce
        is false, for one told to them another way). device is the paired device a web message
        came from."""
        if self.record_message is not None:
            if device is None:
                self.record_message(role, text, channel)
            else:
                self.record_message(role, text, channel, device)
        if announce:
            self.announce(role, text, channel, device)

    def tell(self, listeners: list, *details) -> None:
        for listener in list(listeners):
            try:
                listener(*details)
            except Exception as error:
                self.write_debug(f"Message listener failed ({type(error).__name__}).", "Message listener failed")

    def announce(self, role: str, text: str, channel: str, device: str | None = None) -> None:
        """Pass a message on to listeners only, for one saved elsewhere (typed at the desk)."""
        for listener in list(self.listeners):
            try:
                listener(role, text, channel, device)
            except Exception as error:
                self.write_debug(f"Message listener failed ({type(error).__name__}).", "Message listener failed")

    def add_mirror(self, output: RichLog) -> None:
        self.mirrors.append(output)
        self.draw(output)

    def remove_mirror(self, output: RichLog) -> None:
        self.mirrors.remove(output)

    def group_transcript(self, max_chars: int = GROUP_TRANSCRIPT_CHARS, most: int = 16) -> list[tuple[str, str]]:
        """The conversation as Rocky sees it in a group chat: (who, said) oldest first, who being
        "person", "cora" or "rocky"; the latest within max_chars and most. Quiet notes, photos'
        descriptions and his private (@rocky) answers aren't in it."""
        said, remaining = [], max_chars
        for entry in reversed(self.log_entries):
            if isinstance(entry, (DirectRockyAnswer, ApprovalNote, QuietNote)):
                continue
            if isinstance(entry, RockyAnswer):  # before ChatMarkdown, which it's built on
                # Without Cora's line under it about what he looked up, so he doesn't copy it.
                who, text = "rocky", LOOKED_UNDER.sub("", entry.markup)
            elif isinstance(entry, ChatMarkdown):
                who, text = "cora", entry.markup
            elif isinstance(entry, str) and entry.startswith("Cora: "):
                who, text = "cora", entry[6:]
            elif isinstance(entry, str) and (person := person_line(entry)) is not None:
                who, text = "person", person
            else:
                continue
            if not text.strip():
                continue
            text = text[-remaining:]
            said.append((who, text))
            remaining -= len(text)
            if remaining <= 0 or len(said) >= most:
                break
        return list(reversed(said))

    def get_reconnect_history(self) -> list[dict]:
        """Bounded visible chat, not tool calls, internal reasoning, or unsent drafts."""
        history = []
        remaining = 6000
        for entry in reversed(self.log_entries):
            if isinstance(entry, DirectRockyAnswer):  # the person's own, never hers
                continue
            if isinstance(entry, RockyAnswer):  # before ChatMarkdown, which it's built on
                role, text = "assistant", entry.for_cora
            elif isinstance(entry, ChatMarkdown):
                role, text = "assistant", entry.markup
            elif isinstance(entry, str) and entry.startswith("You: "):
                role, text = "user", entry[5:]
            elif isinstance(entry, str) and entry.startswith(SIGNAL_PREFIX):
                role, text = "user", entry[len(SIGNAL_PREFIX):]
            elif isinstance(entry, str) and entry.startswith(EMAIL_PREFIX):
                role, text = "user", entry[len(EMAIL_PREFIX):]
            elif isinstance(entry, str) and (web := WEB_LINE.match(entry)):
                role, text = "user", entry[web.end():]
            elif isinstance(entry, PhotoNote):  # before ApprovalNote, which it's built on
                role, text = "user", entry.for_cora
            elif isinstance(entry, ApprovalNote):
                role, text = "assistant", entry.for_cora
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
        for output in (self.output, *self.mirrors):
            output.write(styled_entry(message))

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
            if text:
                self.record("user" if speaker == "You" else "assistant", text, "voice")

    def redraw_log(self) -> None:
        if self.redraw_timer is not None:
            self.redraw_timer.stop()
            self.redraw_timer = None
        for output in (self.output, *self.mirrors):
            self.draw(output)

    def schedule_redraw(self) -> None:
        """Redraw soon, so fast streamed fragments share one redraw."""
        if self.redraw_timer is None:
            self.redraw_timer = self.set_timer(STREAM_REDRAW_SECONDS, self.redraw_log)

    def draw(self, output: RichLog) -> None:
        # RichLog.write adds lines, so redraw to update the active message.
        output.clear()
        for entry in self.log_entries:
            output.write(styled_entry(entry), scroll_end=False)
        output.scroll_end(animate=False)

    def show_image(self, image, caption: str, page_url: str) -> None:
        # Like written answers, this does not end an active spoken transcript.
        self.log_entries.extend([
            "",
            f"Cora · image: {caption}",
            ChatImage(image, caption),
            Text.assemble("From ", (urlsplit(page_url).hostname, chat_link(page_url))),
            "",
        ])
        self.redraw_log()
        # The image is never saved; only the caption and its page. Listeners get the picture itself.
        self.record("assistant", f"[image] {caption} ({page_url})", "screen", announce=False)
        self.tell(self.picture_listeners, image, caption, page_url)

    def show_map(self, caption: str, link: str) -> None:
        """A map she showed on the web page: here, its caption and a Google Maps link. Saved as
        text, so she remembers showing it; listeners (the page) are told another way."""
        self.log_entries.extend([
            "",
            f"Cora · map: {caption}",
            Text.assemble("Open in ", ("Google Maps", chat_link(link))),
            "",
        ])
        self.redraw_log()
        self.record("assistant", f"[map] {caption} ({link})", "screen", announce=False)

    def start_written_reply(self, label: str = "Cora · written", channel: str = "screen") -> WrittenDraft:
        """A written reply to show as it streams in; nothing appears until its first text."""
        return WrittenDraft(self, label, channel)

    def show_written_reply(self, text: str, label: str = "Cora · written", channel: str = "screen") -> None:
        """A reply from a chat without Live: there is no voice, so all of it is shown."""
        self.log_entries.extend(["", f"{label}:", ChatMarkdown(text, code_theme="monokai"), ""])
        self.redraw_log()
        self.record("assistant", text, channel)

    def show_signal_message(self, text: str) -> None:
        """The person's own message over Signal, in the same chat as the desktop's."""
        self.flush_transcript()
        self.write_log(f"{SIGNAL_PREFIX}{text}")
        self.record("user", text, "signal")

    def show_email_message(self, text: str) -> None:
        """The person's own email to her address, in the same chat as the desktop's."""
        self.flush_transcript()
        self.write_log(f"{EMAIL_PREFIX}{text}")
        self.record("user", text, "email")

    def show_approval_outcome(self, what: str, outcome: str) -> None:
        """How a request for approval ended (what is "Email · send_email"): a quiet line in the chat,
        and a note for Cora in the history later turns are given. Not saved: her reply says what
        came of it."""
        if outcome not in APPROVAL_SHOWN:
            return
        self.flush_transcript()
        self.write_log(ApprovalNote(f"{APPROVAL_PREFIX}{APPROVAL_SHOWN[outcome].format(what)}",
                                    APPROVAL_FOR_CORA[outcome].format(what)))

    def show_photo_note(self, described: str) -> None:
        """A kept photo's description and id (photo_described), under the message it came with: a
        quiet line in the chat, and in the history later turns are given, so Cora still knows what
        it showed and can look again. Not saved: her reply says what she made of it."""
        shown = described if len(described) <= PHOTO_NOTE_SHOWN_CHARS else described[:PHOTO_NOTE_SHOWN_CHARS - 1] + "…"
        self.write_log(PhotoNote(f"· {shown}", f"(The photo I sent. {described})"))
        if (photo := PHOTO_ID.match(described)) is not None:
            self.tell(self.photo_listeners, photo.group(1))

    def show_rocky(self, question: str, answer: str) -> None:
        """Rocky's answer to her question, word for word, so she doesn't stand between him and the person."""
        self.flush_transcript()
        self.log_entries.extend(["", f"{ROCKY_ASKED}{question}", "", "Rocky:", RockyAnswer(question, answer), ""])
        self.redraw_log()

    def rocky_in_group(self) -> DirectRockyDraft:
        """Rocky's turn in a group chat, under the person's message (already shown): "thinking…"
        until his answer, which is part of her conversation (GroupRockyAnswer)."""
        self.flush_transcript()
        self.log_entries.extend(["", ROCKY_THINKING, "", ""])
        self.redraw_log()
        return DirectRockyDraft(self, len(self.log_entries) - 3, answer=GroupRockyAnswer)

    def ask_rocky_directly(self, question: str, where: str = "", image: bytes | None = None) -> DirectRockyDraft:
        """The person's question to Rocky ("You · to Rocky: …", or "You · to Rocky · web · iPad: …"
        from the web page), with any image sent with it shown small under it, and "thinking…"
        below for his answer. None of it is saved or part of her history: "You · to Rocky" isn't a
        line she's given, and an image in the chat never is."""
        self.flush_transcript()
        asked = f"{YOU_TO_ROCKY}{f' · {where}' if where else ''}: {'[image] ' if image else ''}{question}"
        shown = [asked]
        if image is not None:
            try:
                picture = Image.open(io.BytesIO(image)).convert("RGB")
                picture.thumbnail((1024, 1024))
                shown.append(ChatImage(picture, question))
            except Exception:
                pass  # He's still sent it (or told it couldn't be read); only the thumbnail's missing.
        self.log_entries.extend(["", *shown, "", ROCKY_THINKING, "", ""])
        self.redraw_log()
        return DirectRockyDraft(self, len(self.log_entries) - 3)

    def show_note(self, text: str) -> None:
        """A quiet line in the chat, not saved and not part of her history."""
        self.write_log(QuietNote(f"· {text}"))

    def show_web_message(self, text: str, device: str | None = None) -> None:
        """The person's own message from her web page, in the same chat as the desktop's, labelled
        with the device it came from when known ("You · web · iPad: …")."""
        self.flush_transcript()
        self.write_log(f"You · web · {device}: {text}" if device else f"{WEB_PREFIX}{text}")
        self.record("user", text, "web", device)

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
        self.record("assistant", text, "screen")