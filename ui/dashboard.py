from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen

from ui.chat_widgets import TitleBar


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

    def compose(self) -> ComposeResult:
        yield TitleBar("chat")
        with Vertical(id="dashboard-panel"):
            with Horizontal(id="dashboard-top", classes="dashboard-row"):
                yield Vertical(id="voice-box", classes="dashboard-box")
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
