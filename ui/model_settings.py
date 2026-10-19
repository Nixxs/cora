from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Select

from session_config import MODEL_CHOICES


AREA_LABELS = {
    "delegation": "Delegation (Cora's backend work)",
    "memory": "Memory (memories and summaries)",
    "subconscious": "Relationship, interests and identity",
    "utility": "Duplicate check and small jobs",
}


class ModelSettingsScreen(ModalScreen):
    """One model picker per area; each change is handed to `save` straight away."""

    BINDINGS = [("escape", "dismiss", "Close")]

    def __init__(self, models: dict, save) -> None:
        super().__init__()
        self.models = dict(models)
        self.save = save

    def compose(self) -> ComposeResult:
        with Vertical(id="model-settings"):
            yield Label("Models", id="model-settings-title")
            for area, label in AREA_LABELS.items():
                yield Label(label, classes="model-settings-label")
                yield Select(
                    [(model, model) for model in MODEL_CHOICES], value=self.models[area],
                    allow_blank=False, id=f"model-{area}",
                )
            yield Button("Close", id="model-settings-close")

    def on_select_changed(self, event: Select.Changed) -> None:
        event.stop()
        area = event.select.id.removeprefix("model-")
        # Select also reports its starting value on mount; only real changes are saved.
        if event.value == self.models[area]:
            return
        self.models[area] = event.value
        self.save(area, event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss()
