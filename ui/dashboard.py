from collections import deque

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen

from ui.braille import BrailleGraph
from ui.chat_widgets import BorderLine, TitleBar


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

    def __init__(self, voice_levels) -> None:
        super().__init__()
        # Returns (Cora's playback level, the person's microphone level), each 0.0 to 1.0.
        self.voice_levels = voice_levels
        self.speaker_history = deque(maxlen=400)
        self.mic_history = deque(maxlen=400)

    def compose(self) -> ComposeResult:
        yield TitleBar("chat")
        with Vertical(id="dashboard-panel"):
            with Horizontal(id="dashboard-top", classes="dashboard-row"):
                # Cora's voice rises above the line and the person's hangs below it.
                with Vertical(id="voice-box", classes="dashboard-box"):
                    yield BrailleGraph(id="speaker-graph")
                    yield BorderLine(id="voice-axis")
                    yield BrailleGraph(id="mic-graph", downward=True)
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
        speaker, mic = self.voice_levels()
        # The same gentle gain as the status bar makes ordinary speech visible.
        self.speaker_history.append(speaker ** 0.5)
        self.mic_history.append(mic ** 0.5)
        self.query_one("#speaker-graph", BrailleGraph).values = tuple(self.speaker_history)
        self.query_one("#mic-graph", BrailleGraph).values = tuple(self.mic_history)
