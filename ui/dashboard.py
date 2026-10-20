from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, HorizontalGroup, Vertical
from textual.screen import Screen
from textual.widgets import Button, Static

from devices.audio_bands import BAND_COUNT
from ui.braille import BrailleGraph
from ui.chat_widgets import BorderLine, SelectableRichLog, TitleBar
from ui.composer import ChatComposer
from ui.meters import Meters, ToneRibbon


# A bar jumps straight up to a louder sound, then falls this much per tick (20 a second).
FALL_PER_TICK = 0.08

# How often the open dashboard re-reads the database.
REFRESH_SECONDS = 30

RELATIONSHIP_VALUES = ("trust", "warmth", "familiarity")


# Box id -> border title, in the btop-style layout: a wide voice band with the chat beside
# it, then the database panels below.
BOX_TITLES = {
    "voice-box": "voice",
    "chat-box": "chat",
    "relationship-box": "relationship",
    "memories-box": "memories",
    "interests-box": "interests",
    "conversations-box": "conversations",
    "background-box": "background",
}


class DashboardScreen(Screen):
    """A read-only view of what Cora has stored, switched to from the chat."""

    def __init__(self, voice_bands, conversation, audio_status, load_snapshot) -> None:
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

    def compose(self) -> ComposeResult:
        yield TitleBar("chat")
        with Vertical(id="dashboard-panel"):
            with Horizontal(id="dashboard-top", classes="dashboard-row"):
                with Vertical(id="voice-column"):
                    # Cora's voice rises above the line and the person's hangs below it.
                    with Vertical(id="voice-box", classes="dashboard-box"):
                        yield BrailleGraph(id="speaker-graph", bars=True)
                        yield BorderLine(id="voice-axis")
                        yield BrailleGraph(id="mic-graph", downward=True, bars=True)
                    # The box's bottom border, drawn as widgets so the toggles can sit on the line.
                    with HorizontalGroup(id="voice-controls"):
                        yield Static("╰", classes="voice-line")
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
            with Horizontal(id="dashboard-middle", classes="dashboard-row"):
                with Vertical(id="relationship-box", classes="dashboard-box"):
                    yield Meters(id="relationship-meters", placeholder="loading…")
                    yield Static("history", classes="relationship-label")
                    for name in RELATIONSHIP_VALUES:
                        with Horizontal(classes="relationship-history"):
                            yield Static(name, classes="relationship-history-name")
                            yield BrailleGraph(id=f"{name}-history")
                    yield Static("recent tone", classes="relationship-label")
                    yield ToneRibbon(id="relationship-tones")
                yield Vertical(id="memories-box", classes="dashboard-box")
                yield Vertical(id="interests-box", classes="dashboard-box")
            with Horizontal(id="dashboard-bottom", classes="dashboard-row"):
                yield Vertical(id="conversations-box", classes="dashboard-box")
                yield Vertical(id="background-box", classes="dashboard-box")

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

    def sample_voice(self) -> None:
        speaker, mic = self.voice_bands()
        for graph_id, bands in (("#speaker-graph", speaker), ("#mic-graph", mic)):
            graph = self.query_one(graph_id, BrailleGraph)
            current = graph.values or (0.0,) * BAND_COUNT
            latest = bands or (0.0,) * BAND_COUNT
            graph.values = tuple(max(new, old - FALL_PER_TICK) for old, new in zip(current, latest))
