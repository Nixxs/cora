from collections.abc import Callable
from dataclasses import dataclass, field

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Click
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static, Switch, TabbedContent, TabPane

from services.catalogue import SERVICES
from services.connection import CONNECTED, CONNECTING, FAILED, ServiceStatus
from session_config import MODEL_CHOICES


AREA_LABELS = {
    "delegation": "Delegation (Cora's backend work)",
    "memory": "Memory (memories and summaries)",
    "subconscious": "Relationship, interests and identity",
    "utility": "Duplicate check and small jobs",
}


@dataclass
class ServiceControls:
    """What the Services page reads and does; the app owns the connections and storage."""

    status: Callable[[str], ServiceStatus]
    set_enabled: Callable[[str, bool], None]
    save_token: Callable[[str, str], None]
    forget_token: Callable[[str], None]
    set_tool_enabled: Callable[[str, str, bool], None] = lambda service, tool, enabled: None
    enabled: set[str] = field(default_factory=set)
    has_token: set[str] = field(default_factory=set)
    tools_on: dict[str, set[str]] = field(default_factory=dict)


def status_line(status: ServiceStatus, enabled: bool) -> Text:
    """Short enough for the service's row; a failure's reason shows in its details."""
    if status.state == CONNECTED:
        return Text(f"● connected · {len(status.tools)} tools", style="#8fcf8f")
    if status.state == CONNECTING:
        return Text("◌ connecting…", style="#d7c27a")
    if status.state == FAILED:
        return Text("× failed", style="#e07a7a")
    return Text("○ on, not connected" if enabled else "○ off", style="#8f98a8")


def tool_label(tool) -> Text:
    text = Text(tool.name, style="bold #e0e6ef")
    # The server's own label, a hint only. Tools that change things wait for confirmation support.
    text.append("  read-only" if tool.read_only else "  changes things · later", style="#8fcf8f" if tool.read_only else "#d7a06a")
    summary = tool.description.splitlines()[0] if tool.description else ""
    if summary:
        text.append(f"\n{summary}", style="#8f98a8")
    return text


def tools_heading(status: ServiceStatus, on: set[str]) -> Text:
    if status.state != CONNECTED:
        return Text("Tools appear here once connected.", style="#8f98a8")
    read_only = [tool for tool in status.tools if tool.read_only]
    chosen = sum(tool.name in on for tool in read_only)
    return Text(
        f"{chosen} of {len(read_only)} read-only tools on for Cora · changes reach her at her next connect",
        style="#8f98a8",
    )


class ModelSettingsScreen(ModalScreen):
    """Settings pages: model pickers, general switches and services. Changes apply straight away."""

    BINDINGS = [("escape", "dismiss", "Close")]

    def __init__(
        self, models: dict, save, screen_looking: bool, set_screen_looking, services: ServiceControls | None = None,
    ) -> None:
        super().__init__()
        self.models = dict(models)
        self.save = save
        self.screen_looking = screen_looking
        self.set_screen_looking = set_screen_looking
        self.services = services
        self.shown_tools: dict[str, tuple] = {}
        self.tool_switches: dict[str, list[str]] = {}

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
                if self.services is not None:
                    with TabPane("Services", id="settings-services"):
                        for service in SERVICES.values():
                            yield from self.compose_service(service)
            yield Button("Close", id="model-settings-close")

    def compose_service(self, service) -> ComposeResult:
        # One row per service; clicking it (anywhere but the switch) opens or closes its details.
        with Horizontal(classes="service-heading", id=f"service-heading-{service.id}"):
            yield Label(f"▸ {service.title}", classes="service-title", id=f"service-title-{service.id}")
            yield Static(id=f"service-status-{service.id}", classes="service-status")
            yield Switch(value=service.id in self.services.enabled, id=f"service-enabled-{service.id}")
        with Vertical(classes="service-details", id=f"service-details-{service.id}"):
            yield Static(id=f"service-reason-{service.id}", classes="service-reason")
            yield Label(service.token_help, classes="model-settings-label service-help")
            yield Input(
                password=True, id=f"service-token-{service.id}",
                placeholder="token saved · paste to replace" if service.id in self.services.has_token else "paste a token",
            )
            with Horizontal(classes="service-buttons"):
                yield Button("Save token", id=f"service-save-{service.id}")
                yield Button("Forget token", id=f"service-forget-{service.id}")
            yield Static(id=f"service-tools-heading-{service.id}", classes="service-tools-heading")
            # Filled with one row per tool once connected.
            yield VerticalScroll(classes="service-tools", id=f"service-tools-{service.id}")

    def on_click(self, event: Click) -> None:
        if isinstance(event.widget, Switch):
            return
        for node in event.widget.ancestors_with_self:
            if (node.id or "").startswith("service-heading-"):
                self.toggle_service_details(node.id.removeprefix("service-heading-"))
                return

    def toggle_service_details(self, service_id: str) -> None:
        details = self.query_one(f"#service-details-{service_id}")
        details.display = not details.display
        arrow = "▾" if details.display else "▸"
        self.query_one(f"#service-title-{service_id}", Label).update(f"{arrow} {SERVICES[service_id].title}")

    async def on_mount(self) -> None:
        if self.services is not None:
            await self.show_services()
            self.set_interval(0.5, self.show_services)

    async def show_services(self) -> None:
        for service_id in SERVICES:
            status = self.services.status(service_id)
            self.query_one(f"#service-status-{service_id}", Static).update(
                status_line(status, service_id in self.services.enabled)
            )
            reason = self.query_one(f"#service-reason-{service_id}", Static)
            reason.display = status.state == FAILED
            reason.update(Text(f"Failed: {status.reason}", style="#e07a7a"))
            on = self.services.tools_on.setdefault(service_id, set())
            self.query_one(f"#service-tools-heading-{service_id}", Static).update(tools_heading(status, on))
            tools = status.tools if status.state == CONNECTED else ()
            # Rebuild the rows only when the server's tools change, so switches keep their place.
            if self.shown_tools.get(service_id) != tools:
                self.shown_tools[service_id] = tools
                await self.show_tool_rows(service_id, tools, on)

    async def show_tool_rows(self, service_id: str, tools, on: set[str]) -> None:
        listing = self.query_one(f"#service-tools-{service_id}", VerticalScroll)
        # Finish removing the old rows first, so the new ones can reuse their ids.
        await listing.remove_children()
        listing.display = bool(tools)
        # Read-only tools first: those are the ones that can be switched on for now.
        ordered = sorted(tools, key=lambda tool: (not tool.read_only, tool.name))
        # Switch ids use positions: tool names come from the server and may not be valid ids.
        self.tool_switches[service_id] = [tool.name for tool in ordered]
        await listing.mount_all(
            Horizontal(
                Switch(
                    value=tool.read_only and tool.name in on, disabled=not tool.read_only,
                    id=f"tool-{service_id}-{index}",
                ),
                Static(tool_label(tool), classes="tool-label"),
                classes="tool-row",
            )
            for index, tool in enumerate(ordered)
        )

    def on_switch_changed(self, event: Switch.Changed) -> None:
        event.stop()
        if event.switch.id == "screen-looking":
            if event.value == self.screen_looking:
                return
            self.screen_looking = event.value
            self.set_screen_looking(event.value)
            return
        if event.switch.id.startswith("tool-"):
            service_id, index = event.switch.id.removeprefix("tool-").rsplit("-", 1)
            tool = self.tool_switches[service_id][int(index)]
            on = self.services.tools_on.setdefault(service_id, set())
            if event.value == (tool in on):
                return
            if event.value:
                on.add(tool)
            else:
                on.discard(tool)
            self.services.set_tool_enabled(service_id, tool, event.value)
            return
        service_id = event.switch.id.removeprefix("service-enabled-")
        # The switch also reports changes made here, such as turning back off below.
        if event.value == (service_id in self.services.enabled):
            return
        if event.value and service_id not in self.services.has_token:
            self.notify(f"Save a {SERVICES[service_id].title} token first.", severity="warning")
            event.switch.value = False
            return
        if event.value:
            self.services.enabled.add(service_id)
        else:
            self.services.enabled.discard(service_id)
        self.services.set_enabled(service_id, event.value)

    def on_select_changed(self, event: Select.Changed) -> None:
        event.stop()
        area = event.select.id.removeprefix("model-")
        # Select also reports its starting value on mount; only real changes are saved.
        if event.value == self.models[area]:
            return
        self.models[area] = event.value
        self.save(area, event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.save_service_token(event.input.id.removeprefix("service-token-"))

    def save_service_token(self, service_id: str) -> None:
        field_ = self.query_one(f"#service-token-{service_id}", Input)
        token = field_.value.strip()
        if not token:
            self.notify("Paste a token first.", severity="warning")
            return
        # The token goes straight to the credential store; the field never keeps it.
        field_.value = ""
        field_.placeholder = "token saved · paste to replace"
        self.services.has_token.add(service_id)
        self.services.save_token(service_id, token)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        button = event.button.id or ""
        if button.startswith("service-save-"):
            self.save_service_token(button.removeprefix("service-save-"))
        elif button.startswith("service-forget-"):
            service_id = button.removeprefix("service-forget-")
            self.services.has_token.discard(service_id)
            self.query_one(f"#service-token-{service_id}", Input).placeholder = "paste a token"
            self.services.forget_token(service_id)
            # Turning the switch off also saves the service as off and disconnects it.
            self.query_one(f"#service-enabled-{service_id}", Switch).value = False
        else:
            self.dismiss()
