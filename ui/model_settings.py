from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Select, Switch, TabbedContent, TabPane

from session_config import MODEL_CHOICES


AREA_LABELS = {
    "delegation": "Delegation (Cora's backend work)",
    "memory": "Memory (memories and summaries)",
    "subconscious": "Relationship, interests and identity",
    "utility": "Duplicate check and small jobs",
}


class ModelSettingsScreen(ModalScreen):
    """Settings pages: one model picker per area, and general switches. Changes apply straight away."""

    BINDINGS = [("escape", "dismiss", "Close")]

    def __init__(self, models: dict, save, screen_looking: bool, set_screen_looking) -> None:
        super().__init__()
        self.models = dict(models)
        self.save = save
        self.screen_looking = screen_looking
        self.set_screen_looking = set_screen_looking

    def compose(self) -> ComposeResult:
        with Vertical(id="model-settings"):
            yield Label("Settings", id="model-settings-title")
            with TabbedContent():
                with TabPane("Models", id="settings-models"):
                    for area, label in AREA_LABELS.items():
                        yield Label(label, classes="model-settings-label")
                        yield Select(
                            [(model, model) for model in MODEL_CHOICES], value=self.models[area],
                            allow_blank=False, id=f"model-{area}",
                        )
                with TabPane("General", id="settings-general"):
                    yield Label("Let Cora look at the screen", classes="model-settings-label")
                    yield Switch(value=self.screen_looking, id="screen-looking")
            yield Button("Close", id="model-settings-close")

    def on_switch_changed(self, event: Switch.Changed) -> None:
        event.stop()
        if event.value == self.screen_looking:
            return
        self.screen_looking = event.value
        self.set_screen_looking(event.value)

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
