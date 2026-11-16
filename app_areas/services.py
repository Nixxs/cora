"""MCP services and approval pop-ups: part of CoraApp (see app.py)."""

import asyncio

from textual import work

from services.catalogue import SERVICES
from services.connection import CONNECTED
from ui.confirm_view import ConfirmScreen
from services.tools import PAGE_ARGUMENT, ServiceToolHandler, tool_name, tool_schema
from services.tools import validate_arguments as validate_service_arguments
from services.credentials import forget_token, get_token, save_token
from tools.use_browser import SCHEMA as USE_BROWSER_SCHEMA, validate_arguments as validate_use_browser_arguments


# At launch, how long Live waits for services to connect so their tools are in the first session.
SERVICE_STARTUP_WAIT_SECONDS = 10


class ServicesMixin:
    """MCP services and approval pop-ups."""

    def show_confirmation(self, request) -> None:
        self.push_screen(ConfirmScreen(request, self.confirmations.answer))
        self.tell_cora_approval_waiting(request.title, request.tool, on_behalf=request.on_behalf)

    def remind_cora_approval_waiting(self, request) -> None:
        """Her own requests only: one on someone else's behalf isn't hers to chase."""
        if request.on_behalf is None:
            self.tell_cora_approval_waiting(request.title, request.tool, reminder=True)

    @work(group="approval-notes")
    async def tell_cora_approval_waiting(self, service: str, tool: str, reminder: bool = False,
                                         on_signal: bool = False, on_behalf: str | None = None) -> None:
        """Let Cora know her request waits on the pop-up (or, on a call, in the Signal chat), so she
        can tell the person."""
        try:
            if on_signal:
                await self.live.send_approval_waiting(service, tool, on_signal=True, on_behalf=on_behalf)
            else:
                await self.live.send_approval_waiting(service, tool, reminder=reminder, on_behalf=on_behalf)
        except Exception as error:
            # The pop-up still works without her mentioning it.
            self.activity.write(f"Approval note to Cora failed ({type(error).__name__}).", "Approval note failed")

    def approval_ended(self, request, outcome: str) -> None:
        """How a request ended: a quiet line in the chat (and her later history), and for one she
        didn't make herself, a note to her now, so she isn't left thinking it still waits."""
        self.conversation.show_approval_outcome(f"{request.title} · {request.tool}", outcome)
        if request.on_behalf is not None and self.live.connection is not None and self.live.ready:
            self.tell_cora_approval_outcome(request.tool, outcome, request.on_behalf)

    @work(group="approval-notes")
    async def tell_cora_approval_outcome(self, tool: str, outcome: str, on_behalf: str) -> None:
        try:
            await self.live.send_approval_outcome(tool, outcome, on_behalf)
        except Exception as error:
            self.activity.write(f"Approval note to Cora failed ({type(error).__name__}).", "Approval note failed")

    def close_confirmation(self, request) -> None:
        """A request ended without the person answering (timed out or closing): take its pop-up away."""
        for screen in self.screen_stack:
            if isinstance(screen, ConfirmScreen) and screen.request is request:
                screen.dismiss()

    @work(group="services-save")
    async def set_service_tool(self, service_id: str, tool: str, enabled: bool) -> None:
        title = SERVICES[service_id].title
        try:
            await self.conversation_persistence.call_database(
                self.persistence.save_service_tool, service_id, tool, enabled,
            )
        except Exception as error:
            self.activity.write(f"{title} tool setting save failed ({type(error).__name__}).", "Services error")
            self.notify(f"Could not save the {title} tool setting. Check Activity.", severity="error")
            return
        chosen = self.service_tool_choices.setdefault(service_id, set())
        if enabled:
            chosen.add(tool)
        else:
            chosen.discard(tool)
        self.activity.write(
            f"{title} tool {tool} {'on' if enabled else 'off'}; Cora gets the change at her next connect.",
            f"{title} · {tool} {'on' if enabled else 'off'}",
        )
        self.sync_service_tools()

    async def wait_for_services(self) -> None:
        """At launch, give services a short while to connect so their tools are in the first session.

        A slow service joins at the next connect; this never stops Cora connecting.
        """
        try:
            await self.start_enabled_services().wait()
            await self.services.wait_settled(SERVICE_STARTUP_WAIT_SECONDS)
        except Exception as error:
            self.activity.write(f"Services startup failed ({type(error).__name__}).", "Services error")

    def service_tool_handler(self, service_id: str, tool) -> ServiceToolHandler:
        sometimes = SERVICES[service_id].asks_when
        return ServiceToolHandler(
            service_id, tool.name, self.services.call_tool, self.tool_connection,
            lambda: self.closing or self.reconnecting, self.activity.write,
            cache=self.service_results,
            paged=PAGE_ARGUMENT not in tool.input_schema.get("properties", {}),
            # Anything the server doesn't mark read-only asks the person first, unless the
            # catalogue lets it run without asking; some of those still ask for some calls.
            confirm=self.confirmations.ask if tool.asks_first or tool.name in sometimes else None,
            asks_when=None if tool.asks_first else sometimes.get(tool.name),
        )

    def sync_service_tools(self) -> None:
        """Register each connected service's switched-on tools as Cora's; the next connect sends them.

        A service behind a helper (the browser) gives her one tool instead, use_browser, and its own
        tools go to the helper.
        """
        wanted, helper_tools = {}, {}
        for service_id in SERVICES:
            status = self.services.status(service_id)
            if status.state != CONNECTED:
                continue
            chosen = self.service_tool_choices.get(service_id, set())
            for tool in status.tools:
                schema = tool_schema(service_id, tool) if tool.name in chosen else None
                if schema is None:
                    continue
                if SERVICES[service_id].behind_helper:
                    helper_tools[schema["name"]] = (schema, self.service_tool_handler(service_id, tool).result)
                else:
                    wanted[schema["name"]] = (service_id, tool, schema)
        self.browser_tools = helper_tools
        current = {
            name for name in self.tools.names()
            if any(name.startswith(tool_name(service_id, "")) for service_id in SERVICES)
        }
        for name in current - set(wanted):
            self.tools.unregister(name)
        for name in set(wanted) - current:
            service_id, tool, schema = wanted[name]
            self.tools.register(schema, validate_service_arguments,
                                self.service_tool_handler(service_id, tool).handle)
        had_browser = USE_BROWSER_SCHEMA["name"] in self.tools.names()
        if helper_tools and not had_browser:
            self.tools.register(USE_BROWSER_SCHEMA, validate_use_browser_arguments, self.use_browser.handle)
        elif not helper_tools and had_browser:
            self.tools.unregister(USE_BROWSER_SCHEMA["name"])
        if current != set(wanted) or had_browser != bool(helper_tools):
            self.service_tools_changed = True

    async def browser_model(self) -> str:
        """The model for her browser helper, from Settings > Models."""
        models = await self.conversation_persistence.call_database(self.persistence.load_models)
        return models["browser"]

    def details_for_forms(self) -> str:
        """The person's name and key details, for a form her browser helper fills in for them."""
        name = f"Name: {self.live.person_name}" if self.live.person_name else ""
        # Kept as JSON of label to value (KeyDetailsStore.get_startup_context).
        details = f"Other details: {self.live.key_details_context.strip()}" if self.live.key_details_context.strip() else ""
        return "\n".join(part for part in (name, details) if part)

    @work(group="services-startup")
    async def start_enabled_services(self) -> None:
        """At launch, connect each service left switched on."""
        try:
            enabled = await self.conversation_persistence.call_database(self.persistence.load_enabled_services)
            self.service_tool_choices = await self.conversation_persistence.call_database(
                self.persistence.load_service_tools,
            )
            tokens = {
                service_id: await asyncio.to_thread(get_token, service_id)
                for service_id in enabled if SERVICES[service_id].needs_token
            }
        except Exception as error:
            self.activity.write(f"Services load failed ({type(error).__name__}).", "Services error")
            return
        for service_id in sorted(enabled):
            if self.closing:
                return
            if not SERVICES[service_id].needs_token:
                self.services.start(service_id)
            elif tokens[service_id]:
                self.services.start(service_id, tokens[service_id])
            else:
                title = SERVICES[service_id].title
                self.activity.write(f"{title} is switched on but has no saved token.", "Services error")

    @work(group="services-save")
    async def set_service_enabled(self, service_id: str, enabled: bool) -> None:
        title = SERVICES[service_id].title
        try:
            await self.conversation_persistence.call_database(
                self.persistence.save_service_enabled, service_id, enabled,
            )
        except Exception as error:
            self.activity.write(f"{title} setting save failed ({type(error).__name__}).", "Services error")
            self.notify(f"Could not save the {title} setting. Check Activity.", severity="error")
            return
        if not enabled:
            self.services.stop(service_id)
            return
        if not SERVICES[service_id].needs_token:
            if not self.closing:
                self.services.start(service_id)
            return
        try:
            token = await asyncio.to_thread(get_token, service_id)
        except Exception as error:
            self.activity.write(f"{title} token load failed ({type(error).__name__}).", "Services error")
            return
        if token and not self.closing:
            self.services.start(service_id, token)

    @work(group="services-save")
    async def save_service_token(self, service_id: str, token: str) -> None:
        title = SERVICES[service_id].title
        try:
            await asyncio.to_thread(save_token, service_id, token)
        except Exception as error:
            self.activity.write(f"{title} token save failed ({type(error).__name__}).", "Services error")
            self.notify(f"Could not save the {title} token. Check Activity.", severity="error")
            return
        self.activity.write(f"{title} token saved to the system credential store.", f"{title} token saved")
        # A connection already running, or failed with the old token, retries with the new one.
        if service_id in self.services.connections and not self.closing:
            self.services.start(service_id, token)

    @work(group="services-save")
    async def forget_service_token(self, service_id: str) -> None:
        title = SERVICES[service_id].title
        try:
            await asyncio.to_thread(forget_token, service_id)
        except Exception as error:
            self.activity.write(f"{title} token removal failed ({type(error).__name__}).", "Services error")
            return
        self.activity.write(f"{title} token removed from the system credential store.", f"{title} token removed")
