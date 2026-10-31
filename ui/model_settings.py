from collections.abc import Callable
from dataclasses import dataclass, field

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Click
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static, Switch, TabbedContent, TabPane

from mail import account as email_setup
from mail.account import EmailStatus
from messaging import setup as signal_setup
from messaging.qr import qr_text
from messaging.setup import SignalStatus
from services.catalogue import SERVICES
from services.connection import CONNECTED, CONNECTING, FAILED, ServiceStatus
from session_config import MODEL_CHOICES
from storage.signal_settings_store import normalise_number


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


@dataclass
class SignalControls:
    """What the Signal row reads and does; the app owns the container, linking and storage."""

    status: Callable[[], SignalStatus]
    set_enabled: Callable[[bool], None]
    save_number: Callable[[str], None]
    link: Callable[[], None]
    enabled: bool = False
    person_number: str | None = None
    # People she may talk with as clients, and adding or removing them (by number).
    clients: Callable[[], list] = lambda: []
    add_client: Callable[[str, str], None] = lambda name, number: None
    remove_client: Callable[[str], None] = lambda number: None


@dataclass
class EmailControls:
    """What the Email row reads and does; the app owns her sign-in and the credential store."""

    status: Callable[[], EmailStatus]
    # save_app(client_id, tenant): a blank app ID keeps the saved one; a blank tenant means a personal account.
    save_app: Callable[[str, str], None]
    sign_in: Callable[[], None]
    sign_out: Callable[[], None]
    has_client_id: bool = False
    tenant: str = ""


@dataclass
class WebhookControls:
    """What the Webhooks row reads and does; the app owns the listener, the secrets and Graph's subscription."""

    # status() -> (the listener's line, the new-mail subscription's line); either may be empty.
    status: Callable[[], tuple[str, str]]
    set_enabled: Callable[[bool], None]
    # save_address(public URL, port as typed): the app checks both and says if not.
    save_address: Callable[[str, str], None]
    rotate: Callable[[], None]
    enabled: bool = False
    public_url: str = ""
    port: int = 8000


WEBHOOK_INTRO = (
    "Lets Microsoft tell Cora the moment new mail lands, instead of her checking every 10 minutes. "
    "She listens only on this PC (127.0.0.1, the port below); you route public HTTPS from the "
    "address below to that port yourself, with a reverse proxy or tunnel. See the README."
)
WEBHOOK_URL_HELP = "The public HTTPS address that reaches this PC, such as https://cora.example.com."
WEBHOOK_SECRET_HELP = (
    "Her webhook URLs carry a secret path, and Microsoft sends back a second secret with every "
    "notification; anything without both is ignored. Both are kept in the system credential store."
)


EMAIL_INTRO = (
    "This is Cora's own Outlook account, not yours. Sign in as her account, never your own: "
    "she would be reading your mail."
)
EMAIL_APP_HELP = (
    "The Application (client) ID of the app registered for her in the Azure portal (Entra ID › "
    "App registrations, public client flows on). See the README."
)
EMAIL_TENANT_HELP = (
    "For a work or school account (Microsoft 365, outlook.office.com): her organisation's domain, "
    "such as example.com, or the Directory (tenant) ID. Leave empty for a personal outlook.com account."
)


SIGNAL_INTRO = (
    "This is Cora's own Signal account, on her own phone and number, not yours. Put Signal on a "
    "spare phone or second SIM with Cora's number, then link that phone here. Never scan the code "
    "with your own phone: Cora would become you."
)
SIGNAL_CLIENTS_HELP = (
    "She talks with them like a professional with a client: friendly, but never about your "
    "private life, in their own chats you can't see here yet. Anyone not listed gets one short "
    "polite reply."
)
SIGNAL_SCAN_HINT = (
    "On Cora's phone: Signal › Settings › Linked devices › +, then scan. The code lasts five minutes."
)


def signal_line(status: SignalStatus, enabled: bool) -> Text:
    if status.state == signal_setup.LINKED:
        return Text(f"● Cora's number · {status.account}", style="#8fcf8f")
    if status.state == signal_setup.LINKING:
        return Text("◌ waiting for Cora's phone…", style="#d7c27a")
    if status.state == signal_setup.NOT_LINKED:
        return Text("○ ready · not linked yet", style="#d7a06a")
    if status.state == signal_setup.STARTING:
        return Text("◌ starting…", style="#d7c27a")
    if status.state == signal_setup.FAILED:
        return Text("× failed", style="#e07a7a")
    return Text("○ on, not started" if enabled else "○ off", style="#8f98a8")


def email_line(status: EmailStatus) -> Text:
    if status.state == email_setup.READY:
        return Text(f"● Cora's address · {status.address}", style="#8fcf8f")
    if status.state == email_setup.SIGNING_IN:
        return Text("◌ waiting for her sign-in…", style="#d7c27a")
    if status.state == email_setup.SIGNED_OUT:
        return Text("○ not signed in", style="#d7a06a")
    if status.state == email_setup.FAILED:
        return Text("× failed", style="#e07a7a")
    return Text("○ not set up", style="#8f98a8")


def email_code_text(status: EmailStatus) -> Text:
    text = Text("Go to ", style="#8f98a8")
    text.append(status.link or "", style="bold #e0e6ef")
    text.append(" and enter ", style="#8f98a8")
    text.append(status.code or "", style="bold #e0e6ef")
    text.append(", then sign in as Cora's own account. The code lasts 15 minutes.", style="#8f98a8")
    return text


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
    # The server's own label, a hint only. Anything not marked read-only asks the person each time.
    text.append("  read-only" if tool.read_only else "  changes things · asks you first",
                style="#8fcf8f" if tool.read_only else "#d7a06a")
    summary = tool.description.splitlines()[0] if tool.description else ""
    if summary:
        text.append(f"\n{summary}", style="#8f98a8")
    return text


def tools_heading(status: ServiceStatus, on: set[str]) -> Text:
    if status.state != CONNECTED:
        return Text("Tools appear here once connected.", style="#8f98a8")
    chosen = sum(tool.name in on for tool in status.tools)
    return Text(
        f"{chosen} of {len(status.tools)} tools on for Cora · changes reach her at her next connect",
        style="#8f98a8",
    )


class ModelSettingsScreen(ModalScreen):
    """Settings pages: model pickers, general switches and services. Changes apply straight away."""

    BINDINGS = [("escape", "dismiss", "Close")]

    def __init__(
        self, models: dict, save, screen_looking: bool, set_screen_looking, services: ServiceControls | None = None,
        signal: SignalControls | None = None, email: EmailControls | None = None,
        webhooks: WebhookControls | None = None,
    ) -> None:
        super().__init__()
        self.models = dict(models)
        self.save = save
        self.screen_looking = screen_looking
        self.set_screen_looking = set_screen_looking
        self.services = services
        self.signal = signal
        self.email = email
        self.webhooks = webhooks
        self.shown_link_uri: str | None = None
        self.shown_clients: list[tuple[str, str]] | None = None
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
                        with VerticalScroll(id="services-scroll"):
                            for service in SERVICES.values():
                                yield from self.compose_service(service)
                            if self.signal is not None:
                                yield from self.compose_signal()
                            if self.email is not None:
                                yield from self.compose_email()
                            if self.webhooks is not None:
                                yield from self.compose_webhooks()
            yield Button("Close", id="model-settings-close")

    def compose_signal(self) -> ComposeResult:
        with Horizontal(classes="service-heading", id="service-heading-signal"):
            yield Label("▸ Signal", classes="service-title", id="service-title-signal")
            yield Static(id="signal-status", classes="service-status")
            yield Switch(value=self.signal.enabled, id="signal-enabled")
        with Vertical(classes="service-details", id="service-details-signal"):
            yield Static(Text(SIGNAL_INTRO, style="#d7a06a"), classes="service-help")
            yield Static(id="signal-reason", classes="service-reason")
            yield Label("Your own number: the only one she answers for now", classes="model-settings-label")
            yield Input(
                value=self.signal.person_number or "", placeholder="+61400000000", id="signal-person",
            )
            with Horizontal(classes="service-buttons"):
                yield Button("Save your number", id="signal-save-number")
                yield Button("Link Cora's phone", id="signal-link")
            yield Static(id="signal-qr", classes="signal-qr")
            yield Label(SIGNAL_SCAN_HINT, id="signal-scan-hint", classes="model-settings-label service-help")
            yield Label("People she may talk with, as clients", classes="model-settings-label")
            yield Static(Text(SIGNAL_CLIENTS_HELP, style="#8f98a8"), classes="service-help")
            # One row per person, filled from the app's list.
            yield Vertical(id="signal-clients", classes="signal-clients")
            with Horizontal(classes="service-buttons"):
                yield Input(placeholder="name", id="signal-client-name")
                yield Input(placeholder="+61400000000", id="signal-client-number")
            yield Button("Add person", id="signal-add-client")

    def compose_email(self) -> ComposeResult:
        with Horizontal(classes="service-heading", id="service-heading-email"):
            yield Label("▸ Email", classes="service-title", id="service-title-email")
            yield Static(id="email-status", classes="service-status")
        with Vertical(classes="service-details", id="service-details-email"):
            yield Static(Text(EMAIL_INTRO, style="#d7a06a"), classes="service-help")
            yield Static(id="email-reason", classes="service-reason")
            yield Label(EMAIL_APP_HELP, classes="model-settings-label service-help")
            yield Input(
                id="email-client-id",
                placeholder="app ID saved · paste to replace" if self.email.has_client_id else "paste the app ID",
            )
            yield Label(EMAIL_TENANT_HELP, classes="model-settings-label service-help")
            yield Input(value=self.email.tenant, id="email-tenant", placeholder="example.com · empty for personal")
            with Horizontal(classes="service-buttons"):
                yield Button("Save", id="email-save-id")
                yield Button("Sign Cora in", id="email-sign-in")
                yield Button("Sign out", id="email-sign-out")
            yield Static(id="email-code", classes="service-reason")

    def compose_webhooks(self) -> ComposeResult:
        with Horizontal(classes="service-heading", id="service-heading-webhooks"):
            yield Label("▸ Webhooks", classes="service-title", id="service-title-webhooks")
            yield Static(id="webhooks-status", classes="service-status")
            yield Switch(value=self.webhooks.enabled, id="webhooks-enabled")
        with Vertical(classes="service-details", id="service-details-webhooks"):
            yield Static(Text(WEBHOOK_INTRO, style="#d7a06a"), classes="service-help")
            yield Static(id="webhooks-mail", classes="service-reason")
            yield Label(WEBHOOK_URL_HELP, classes="model-settings-label service-help")
            yield Input(value=self.webhooks.public_url, id="webhooks-url", placeholder="https://cora.example.com")
            yield Label("Local port (on 127.0.0.1)", classes="model-settings-label")
            yield Input(value=str(self.webhooks.port), id="webhooks-port", placeholder="8000")
            yield Static(Text(WEBHOOK_SECRET_HELP, style="#8f98a8"), classes="service-help")
            with Horizontal(classes="service-buttons"):
                yield Button("Save", id="webhooks-save")
                yield Button("New secrets", id="webhooks-rotate")

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
        title = {"signal": "Signal", "email": "Email", "webhooks": "Webhooks"}.get(service_id)             or SERVICES[service_id].title
        self.query_one(f"#service-title-{service_id}", Label).update(f"{arrow} {title}")

    async def on_mount(self) -> None:
        if self.services is not None:
            await self.show_services()
            self.set_interval(0.5, self.show_services)

    async def show_signal_clients(self) -> None:
        clients = [(client.name, client.number) for client in self.signal.clients()]
        if clients == self.shown_clients:
            return
        self.shown_clients = clients
        listing = self.query_one("#signal-clients", Vertical)
        # Finish removing the old rows first, so the new ones can reuse their ids.
        await listing.remove_children()
        await listing.mount_all(
            Horizontal(
                Static(f"{name} · {number}", classes="tool-label"),
                Button("Remove", id=f"signal-remove-{index}"),
                classes="tool-row",
            )
            for index, (name, number) in enumerate(clients)
        ) if clients else listing.mount(Static(Text("Nobody added yet.", style="#8f98a8")))

    async def show_signal(self) -> None:
        await self.show_signal_clients()
        status = self.signal.status()
        self.query_one("#signal-status", Static).update(signal_line(status, self.signal.enabled))
        reason = self.query_one("#signal-reason", Static)
        reason.display = bool(status.reason)
        reason.update(Text(status.reason or "", style="#e07a7a"))
        self.query_one("#signal-link", Button).disabled = not (
            status.state == signal_setup.NOT_LINKED and self.signal.person_number
        )
        linking = status.state == signal_setup.LINKING and status.link_uri is not None
        qr = self.query_one("#signal-qr", Static)
        qr.display = linking
        self.query_one("#signal-scan-hint").display = linking
        # Drawn once per code; a new code means a new link attempt.
        if linking and status.link_uri != self.shown_link_uri:
            self.shown_link_uri = status.link_uri
            qr.update(qr_text(status.link_uri))

    def show_email(self) -> None:
        status = self.email.status()
        self.query_one("#email-status", Static).update(email_line(status))
        self.query_one("#email-client-id", Input).placeholder = (
            "app ID saved · paste to replace" if self.email.has_client_id else "paste the app ID"
        )
        reason = self.query_one("#email-reason", Static)
        reason.display = bool(status.reason)
        reason.update(Text(status.reason or "", style="#e07a7a"))
        signing_in = status.state == email_setup.SIGNING_IN
        code = self.query_one("#email-code", Static)
        code.display = signing_in
        if signing_in:
            code.update(email_code_text(status))
        self.query_one("#email-sign-in", Button).disabled = not self.email.has_client_id or signing_in \
            or status.state == email_setup.READY
        self.query_one("#email-sign-out", Button).disabled = status.state not in (
            email_setup.READY, email_setup.SIGNING_IN, email_setup.FAILED)

    def show_webhooks(self) -> None:
        listening, mail = self.webhooks.status()
        line = Text(f"● {listening}", style="#8f98a8") if listening else Text("○ off", style="#8f98a8")
        self.query_one("#webhooks-status", Static).update(line)
        detail = self.query_one("#webhooks-mail", Static)
        detail.display = bool(mail)
        detail.update(Text(mail, style="#8f98a8"))

    async def show_services(self) -> None:
        if self.signal is not None:
            await self.show_signal()
        if self.email is not None:
            self.show_email()
        if self.webhooks is not None:
            self.show_webhooks()
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
        # Read-only tools first, then those that ask the person each time.
        ordered = sorted(tools, key=lambda tool: (not tool.read_only, tool.name))
        # Switch ids use positions: tool names come from the server and may not be valid ids.
        self.tool_switches[service_id] = [tool.name for tool in ordered]
        await listing.mount_all(
            Horizontal(
                Switch(value=tool.name in on, id=f"tool-{service_id}-{index}"),
                Static(tool_label(tool), classes="tool-label"),
                classes="tool-row",
            )
            for index, tool in enumerate(ordered)
        )

    def on_switch_changed(self, event: Switch.Changed) -> None:
        event.stop()
        if event.switch.id == "signal-enabled":
            if event.value != self.signal.enabled:
                self.signal.enabled = event.value
                self.signal.set_enabled(event.value)
            return
        if event.switch.id == "webhooks-enabled":
            if event.value == self.webhooks.enabled:
                return
            if event.value and not self.webhooks.public_url:
                self.notify("Save the public address first.", severity="warning")
                event.switch.value = False
                return
            self.webhooks.enabled = event.value
            self.webhooks.set_enabled(event.value)
            return
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
        if event.input.id == "signal-person":
            self.save_person_number()
        elif event.input.id in ("email-client-id", "email-tenant"):
            self.save_email_app()
        elif event.input.id in ("webhooks-url", "webhooks-port"):
            self.save_webhook_address()
        elif event.input.id in ("signal-client-name", "signal-client-number"):
            self.add_client()
        else:
            self.save_service_token(event.input.id.removeprefix("service-token-"))

    def add_client(self) -> None:
        name_field = self.query_one("#signal-client-name", Input)
        number_field = self.query_one("#signal-client-number", Input)
        if not name_field.value.strip():
            self.notify("Give them a name.", severity="warning")
            return
        try:
            number = normalise_number(number_field.value)
        except ValueError as error:
            self.notify(str(error), severity="warning")
            return
        # The app checks the rest (not yours, not Cora's, not already added) and says if not.
        self.signal.add_client(name_field.value, number)
        name_field.value = number_field.value = ""

    def save_person_number(self) -> None:
        field_ = self.query_one("#signal-person", Input)
        try:
            number = normalise_number(field_.value)
        except ValueError as error:
            self.notify(str(error), severity="warning")
            return
        if number == self.signal.status().account:
            self.notify("That's Cora's own number. Enter yours.", severity="warning")
            return
        field_.value = number
        self.signal.person_number = number
        self.signal.save_number(number)

    def save_email_app(self) -> None:
        field_ = self.query_one("#email-client-id", Input)
        if not field_.value.strip() and not self.email.has_client_id:
            self.notify("Paste the app ID first.", severity="warning")
            return
        # The app checks both and saves them, or says why not.
        self.email.save_app(field_.value, self.query_one("#email-tenant", Input).value)
        field_.value = ""

    def save_webhook_address(self) -> None:
        url = self.query_one("#webhooks-url", Input).value
        if not url.strip():
            self.notify("Give the public HTTPS address first.", severity="warning")
            return
        # The app checks both and saves them, or says why not.
        self.webhooks.save_address(url, self.query_one("#webhooks-port", Input).value)

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
        if button == "signal-save-number":
            self.save_person_number()
        elif button == "signal-link":
            self.signal.link()
        elif button == "email-save-id":
            self.save_email_app()
        elif button == "email-sign-in":
            self.email.sign_in()
        elif button == "email-sign-out":
            self.email.sign_out()
        elif button == "webhooks-save":
            self.save_webhook_address()
        elif button == "webhooks-rotate":
            self.webhooks.rotate()
        elif button == "signal-add-client":
            self.add_client()
        elif button.startswith("signal-remove-"):
            _, number = self.shown_clients[int(button.removeprefix("signal-remove-"))]
            self.signal.remove_client(number)
        elif button.startswith("service-save-"):
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
