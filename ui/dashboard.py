from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen

from devices.audio_bands import BAND_COUNT
from ui.braille import BrailleGraph
from ui.chat_widgets import BorderLine, TitleBar


# A bar jumps straight up to a louder sound, then falls this much per tick (20 a second).
FALL_PER_TICK = 0.08


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

    def __init__(self, voice_bands) -> None:
        super().__init__()
        # Returns (Cora's playback bands, the person's microphone bands), each 0.0 to 1.0,
        # or an empty tuple for silence.
        self.voice_bands = voice_bands

    def compose(self) -> ComposeResult:
        yield TitleBar("chat")
        with Vertical(id="dashboard-panel"):
            with Horizontal(id="dashboard-top", classes="dashboard-row"):
                # Cora's voice rises above the line and the person's hangs below it.
                with Vertical(id="voice-box", classes="dashboard-box"):
                    yield BrailleGraph(id="speaker-graph", bars=True)
                    yield BorderLine(id="voice-axis")
                    yield BrailleGraph(id="mic-graph", downward=True, bars=True)
                yield Vertical(id="chat-box", classes="dashboard-box")
            with Horizontal(id="dashboard-middle", classes="dashboard-row"):
                yield Vertical(id="relationship-box", classes="dashboard-box")
                yield Vertical(id="memories-box", classes="dashboard-box")
                yield Vertical(id="interests-box", classes="dashboard-box")
            with Horizontal(id="dashboard-bottom", classes="dashboard-row"):
                yield Vertical(id="conversations-box", classes="dashboard-box")
                yield Vertical(id="background-box", classes="dashboard-box")

    def on_mount(self) -> None:
        for box_id, title in BOX_TITLES.items():
            self.query_one(f"#{box_id}").border_title = title
        self.set_interval(0.05, self.sample_voice)

    def sample_voice(self) -> None:
        speaker, mic = self.voice_bands()
        for graph_id, bands in (("#speaker-graph", speaker), ("#mic-graph", mic)):
            graph = self.query_one(graph_id, BrailleGraph)
            current = graph.values or (0.0,) * BAND_COUNT
            latest = bands or (0.0,) * BAND_COUNT
            graph.values = tuple(max(new, old - FALL_PER_TICK) for old, new in zip(current, latest))
