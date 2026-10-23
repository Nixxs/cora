from datetime import datetime, timedelta, timezone

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, HorizontalGroup, HorizontalScroll, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Static

from devices.audio_bands import BAND_COUNT
from memory.store import fade_weight
from subconscious.identity.identity_rules import is_held
from ui.braille import BrailleGraph
from ui.chat_widgets import BorderLine, SelectableRichLog, TitleBar
from ui.composer import ChatComposer
from ui.meters import LABEL_STYLE, TRACK_STYLE, CalendarHeatmap, Meters, ToneRibbon


# A bar jumps straight up to a louder sound, then falls this much per tick (20 a second).
FALL_PER_TICK = 0.08

# How often the open dashboard re-reads the database.
REFRESH_SECONDS = 30

RELATIONSHIP_VALUES = ("trust", "warmth", "familiarity")


# Box id -> border title, in the btop-style layout: a wide voice band with the chat beside
# it, then the database panels below.
BOX_TITLES = {
    "chat-box": "chat",
    "relationship-box": "relationship",
    "memories-box": "memories",
    "interests-box": "interests",
    "identity-box": "identity",
    "conversations-box": "conversations",
    "context-box": "context",
    "background-box": "background",
}


def whole_unit(seconds: float) -> str | None:
    """The largest whole unit, like '5m', '3h' or '2d'; None under a minute."""
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit}"
    return None


def ago(then, now) -> str:
    """How long ago: 'never', 'just now', '5m ago', '3h ago', '2d ago'."""
    if then is None:
        return "never"
    span = whole_unit((now - then).total_seconds())
    return f"{span} ago" if span else "just now"


def until(when, now) -> str:
    """How long until: 'due' once the time has come, else 'soon', 'in 5m', 'in 3h'."""
    if when is None or when <= now:
        return "due"
    span = whole_unit((when - now).total_seconds())
    return f"in {span}" if span else "soon"


class DashboardScreen(Screen):
    """A read-only view of what Cora has stored, switched to from the chat."""

    def __init__(self, voice_bands, conversation, audio_status, load_snapshot, activity) -> None:
        super().__init__()
        # Returns (Cora's playback bands, the person's microphone bands), each 0.0 to 1.0,
        # or an empty tuple for silence.
        self.voice_bands = voice_bands
        self.conversation = conversation
        # Owns the chat's mic and speaker buttons, which the voice box's toggles press.
        self.audio_status = audio_status
        # Reads a DashboardSnapshot off the UI thread; returns None if the database failed.
        self.load_snapshot = load_snapshot
        # The last good snapshot; a failed refresh keeps showing it.
        self.snapshot = None
        # The chat's activity panel, whose latest summary the voice box shows.
        self.activity = activity

    def compose(self) -> ComposeResult:
        yield TitleBar("chat")
        with Vertical(id="dashboard-panel"):
            with Horizontal(id="dashboard-top", classes="dashboard-row"):
                with Vertical(id="voice-column"):
                    # The box's top border, drawn as widgets so standby can show at its right.
                    with HorizontalGroup(id="voice-header"):
                        yield Static("╭─", classes="voice-line")
                        yield Static("voice", id="voice-title")
                        yield BorderLine(id="voice-header-fill")
                        yield Static("○ standby", id="dashboard-standby")
                        yield Static("─╮", classes="voice-line")
                    # Cora's voice rises above the line and the person's hangs below it.
                    with Vertical(id="voice-box", classes="dashboard-box"):
                        yield BrailleGraph(id="speaker-graph", bars=True)
                        yield BorderLine(id="voice-axis")
                        yield BrailleGraph(id="mic-graph", downward=True, bars=True)
                    # The box's bottom border, drawn as widgets so the toggles can sit on the line.
                    with HorizontalGroup(id="voice-controls"):
                        yield Static("╰─", classes="voice-line")
                        yield Static(id="dashboard-activity")
                        yield BorderLine(id="voice-controls-fill")
                        yield Button("🔊 speaker", id="dashboard-speaker")
                        yield Static("─", classes="voice-line")
                        yield Button("🎙 mic", id="dashboard-mic")
                        yield Static("─╯", classes="voice-line")
                with Vertical(id="chat-box", classes="dashboard-box"):
                    yield SelectableRichLog(id="dashboard-chat", wrap=True, min_width=1)
                    yield BorderLine(id="dashboard-input-line")
                    # Posts the same Submitted message as the chat's composer, which the app sends.
                    yield ChatComposer(
                        placeholder="Type a message to Cora...",
                        id="dashboard-input",
                        soft_wrap=True,
                        show_line_numbers=False,
                        highlight_cursor_line=False,
                    )
            # Pans sideways when the boxes don't fit; unfocusable so clicks leave typing alone.
            with HorizontalScroll(id="dashboard-middle", can_focus=False):
                with Vertical(id="relationship-box", classes="dashboard-box"):
                    yield Meters(id="relationship-meters", placeholder="loading…")
                    yield Static("history", classes="dashboard-label")
                    for name in RELATIONSHIP_VALUES:
                        with Horizontal(classes="relationship-history"):
                            yield Static(name, classes="relationship-history-name")
                            yield BrailleGraph(id=f"{name}-history", pulse=True, grow=True)
                    yield Static("recent tone", classes="dashboard-label")
                    yield ToneRibbon(id="relationship-tones")
                with Vertical(id="memories-box", classes="dashboard-box"):
                    yield Meters(id="memory-kinds", placeholder="loading…")
                    yield Static("new per day", classes="dashboard-label")
                    # Older days dim as recall fades them.
                    yield BrailleGraph(id="memory-days", pulse=True, grow=True,
                                       shade=lambda days: fade_weight(timedelta(days=days)))
                    yield Static("waiting", classes="dashboard-label")
                    yield Static(id="memory-pending")
                with Vertical(id="interests-box", classes="dashboard-box"):
                    yield Meters(id="interest-strengths", placeholder="loading…")
                    yield Static("latest finding", classes="dashboard-label")
                    # Scrolls, so a long finding can be read in full.
                    with VerticalScroll(id="interest-finding-scroll"):
                        yield Static(id="interest-finding")
                with Vertical(id="identity-box", classes="dashboard-box"):
                    # Scrolls, so every trait can be read in full.
                    with VerticalScroll(id="identity-scroll"):
                        yield Static(id="identity-traits")
            # Pans sideways like the middle row once the boxes reach their minimum widths.
            with HorizontalScroll(id="dashboard-bottom", classes="dashboard-row", can_focus=False):
                with Vertical(id="conversations-box", classes="dashboard-box"):
                    yield Static("your messages per day", classes="dashboard-label")
                    yield CalendarHeatmap(id="conversation-days")
                with Vertical(id="context-box", classes="dashboard-box"):
                    yield Static("connect to measure", id="context-placeholder")
                with Vertical(id="background-box", classes="dashboard-box"):
                    yield Static("last ran · next", classes="dashboard-label")
                    yield Static(id="background-runs")

    def on_mount(self) -> None:
        for box_id, title in BOX_TITLES.items():
            self.query_one(f"#{box_id}").border_title = title
        self.set_interval(0.05, self.sample_voice)
        self.sync_audio_controls()
        self.set_interval(0.1, self.sync_audio_controls)
        self.chat_log = self.query_one("#dashboard-chat", SelectableRichLog)
        self.conversation.add_mirror(self.chat_log)
        self.query_one("#dashboard-input", ChatComposer).focus()
        self.refresh_snapshot()
        self.set_interval(REFRESH_SECONDS, self.refresh_snapshot)

    @work(group="dashboard-snapshot")
    async def refresh_snapshot(self) -> None:
        snapshot = await self.load_snapshot()
        if snapshot is not None:
            self.snapshot = snapshot
            self.show_snapshot()

    def show_snapshot(self) -> None:
        meters = self.query_one("#relationship-meters", Meters)
        relationship = self.snapshot.relationship
        if relationship is None:
            meters.placeholder = "no relationship yet"
            meters.rows = ()
        else:
            meters.rows = tuple((name, getattr(relationship, name)) for name in RELATIONSHIP_VALUES)
        # Ends at the current value, so each graph meets its meter.
        history = self.snapshot.history + ((relationship,) if relationship else ())
        for name in RELATIONSHIP_VALUES:
            self.query_one(f"#{name}-history", BrailleGraph).values = tuple(
                getattr(values, name) for values in history
            )
        self.query_one("#relationship-tones", ToneRibbon).tones = self.snapshot.tones
        kinds = self.query_one("#memory-kinds", Meters)
        total = sum(self.snapshot.memory_kinds.values())
        kinds.placeholder = "no memories yet"
        kinds.rows = tuple(
            (kind, count / total, str(count)) for kind, count in self.snapshot.memory_kinds.items()
        ) if total else ()
        days = self.query_one("#memory-days", BrailleGraph)
        # Scaled to the busiest day on screen, two days per cell.
        days.maximum = max(self.snapshot.memory_days[-days.size.width * 2:], default=0) or 1
        days.values = self.snapshot.memory_days
        waiting = {step: count for step, count in self.snapshot.memory_pending.items() if count}
        pending = self.query_one("#memory-pending", Static)
        pending.update(" · ".join(f"{count} {step}" for step, count in waiting.items()) or "nothing")
        pending.set_class(bool(waiting), "memory-waiting")
        strengths = self.query_one("#interest-strengths", Meters)
        strengths.placeholder = "no interests yet"
        strengths.rows = tuple(
            (title, strength, f"{'↑' if rising else '↓'} {strength:.0%}")
            for title, strength, rising in self.snapshot.interests
        )
        latest = self.snapshot.latest_finding
        # Text rather than a string, so brackets in the model's finding are not read as markup.
        self.query_one("#interest-finding", Static).update(
            Text.assemble((latest[0], "#7fb8d8"), ": ", latest[1]) if latest
            else Text("nothing found yet", style=TRACK_STYLE)
        )
        traits = Text()
        for index, trait in enumerate(self.snapshot.traits):
            held = is_held(trait)
            # A forming trait is dimmed: it doesn't reach conversations until it is held.
            traits.append(("\n\n" if index else "") + f"{trait.confidence:.0%} ", style="#d8a6c8" if held else LABEL_STYLE)
            traits.append(trait.kind + ("" if held else " · forming"), style=LABEL_STYLE)
            traits.append("\n" + trait.statement, style="" if held else LABEL_STYLE)
        self.query_one("#identity-traits", Static).update(traits or Text("no traits yet", style=TRACK_STYLE))
        now = datetime.now(timezone.utc)
        heatmap = self.query_one("#conversation-days", CalendarHeatmap)
        heatmap.today_row = now.astimezone().weekday()
        heatmap.days = self.snapshot.message_days
        width = max(map(len, self.snapshot.last_runs), default=0)
        runs = Text()
        for index, (job, then) in enumerate(self.snapshot.last_runs.items()):
            runs.append(("\n" if index else "") + f"{job:<{width}}  ", style=LABEL_STYLE)
            last = ago(then, now)
            runs.append(last, style=TRACK_STYLE if then is None else "")
            # Only the timed jobs have a next time; the rest run when there is work.
            if job in self.snapshot.next_due:
                runs.append(" " * (10 - len(last)) + until(self.snapshot.next_due[job], now), style=LABEL_STYLE)
        self.query_one("#background-runs", Static).update(runs)

    def on_unmount(self) -> None:
        self.conversation.remove_mirror(self.chat_log)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        chat_button = {
            "dashboard-speaker": self.audio_status.speaker,
            "dashboard-mic": self.audio_status.mic,
        }.get(event.button.id)
        if chat_button is None:
            return
        event.stop()
        # The chat's buttons own the mute logic, so both screens stay in step.
        chat_button.press()
        self.query_one("#dashboard-input", ChatComposer).focus()

    def sync_audio_controls(self) -> None:
        live = self.audio_status.live
        for button_id, chat_button, muted, labels in (
            ("#dashboard-speaker", self.audio_status.speaker, live.speaker_muted, ("🔊 speaker", "🔇 muted")),
            ("#dashboard-mic", self.audio_status.mic, live.microphone_muted, ("🎙 mic", "🎙 muted")),
        ):
            button = self.query_one(button_id, Button)
            label = labels[muted]
            if str(button.label) != label:
                button.label = label
                # Button.label repaints but does not invalidate its cached auto width.
                button.refresh(layout=True)
            button.set_class(muted, "audio-muted")
            button.tooltip = chat_button.tooltip
            button.disabled = chat_button.disabled
        summary = self.query_one("#dashboard-activity", Static)
        panel = self.activity.panel
        if str(summary.render()) != panel.title:
            # Text, so brackets in an error summary are not read as markup.
            summary.update(Text(panel.title))
        summary.set_class(panel.has_class("activity-error"), "activity-error")
        self.query_one("#dashboard-standby", Static).display = self.app.standby

    def sample_voice(self) -> None:
        speaker, mic = self.voice_bands()
        for graph_id, bands in (("#speaker-graph", speaker), ("#mic-graph", mic)):
            graph = self.query_one(graph_id, BrailleGraph)
            current = graph.values or (0.0,) * BAND_COUNT
            latest = bands or (0.0,) * BAND_COUNT
            graph.values = tuple(max(new, old - FALL_PER_TICK) for old, new in zip(current, latest))
