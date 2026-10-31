"""One MCP connection per service, held open by its own task, and the manager that runs them."""

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from os import environ, getenv
from pathlib import Path
import shutil

from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from .catalogue import SERVICES, Service

OFF, CONNECTING, CONNECTED, FAILED = "off", "connecting", "connected", "failed"
# Listing tools may wait on a first-run download of a local server.
LIST_TOOLS_SECONDS = 180
# Passed to a local server as well as the library's defaults (with any DOCKER_ settings).
PASSED_VARIABLES = frozenset({"PROGRAMFILES", "PROGRAMDATA", "PROGRAMW6432", "XDG_RUNTIME_DIR"})


@dataclass(frozen=True)
class ServiceTool:
    name: str
    description: str
    read_only: bool  # the server's own hint, never a guarantee
    input_schema: dict
    # In the catalogue's list of tools that run without asking, though not marked read-only.
    unasked: bool = False

    @property
    def asks_first(self) -> bool:
        """Whether the person approves each call before it reaches the service."""
        return not (self.read_only or self.unasked)


@dataclass(frozen=True)
class ServiceStatus:
    state: str = OFF
    reason: str | None = None
    tools: tuple[ServiceTool, ...] = ()


class HttpErrors:
    """Keeps the last HTTP error reply: the MCP client itself only reports a generic error."""

    def __init__(self) -> None:
        self.last: str | None = None

    async def hook(self, response) -> None:
        if response.status_code >= 400:
            await response.aread()
            lines = response.text.strip().splitlines()
            self.last = f"HTTP {response.status_code}: {(lines[0] if lines else response.reason_phrase)[:200]}"


@asynccontextmanager
async def open_http_client(url: str, token: str, errors: HttpErrors):
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
    http.event_hooks = {"response": [errors.hook]}
    async with http, Client(streamable_http_client(url, http_client=http)) as client:
        yield client


def local_log_path(service_id: str) -> Path:
    """Where a local server's own error output goes, for when it fails to start."""
    # %LOCALAPPDATA% on Windows; the XDG state folder elsewhere.
    base = getenv("LOCALAPPDATA") or getenv("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "cora" / "services" / f"{service_id}.log"


def passed_environment() -> dict[str, str]:
    """What a local server gets beyond the MCP library's short safe list: Docker's own settings.

    Without ProgramFiles and ProgramData, Docker on Windows can't find its compose plugin.
    """
    return {
        name: value for name, value in environ.items()
        if name.upper() in PASSED_VARIABLES or name.upper().startswith("DOCKER_")
    }


@asynccontextmanager
async def open_stdio_client(service: Service, errors: HttpErrors):
    """Start a local server (such as a container) and talk to it over stdio; closing stops it."""
    if shutil.which(service.command[0]) is None:
        raise FileNotFoundError(f"{service.command[0]} not found; install Docker and start it")
    path = local_log_path(service.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    # A real file: inside the app, sys.stderr is Textual's capture, which has no file handle.
    with path.open("w", encoding="utf-8") as log:
        parameters = StdioServerParameters(
            command=service.command[0], args=list(service.command[1:]), env=passed_environment(),
        )
        async with Client(stdio_client(parameters, errlog=log)) as client:
            yield client


async def list_all_tools(client) -> tuple[ServiceTool, ...]:
    tools, cursor = [], None
    while True:
        page = await client.list_tools(cursor=cursor)
        tools += page.tools
        cursor = page.next_cursor
        if cursor is None:
            break
    return tuple(sorted((
        ServiceTool(
            tool.name, (tool.description or "").strip(),
            bool(tool.annotations and tool.annotations.read_only_hint), dict(tool.input_schema),
        )
        for tool in tools
    ), key=lambda tool: tool.name))


def apply_catalogue(tools: tuple[ServiceTool, ...], service: Service) -> tuple[ServiceTool, ...]:
    """Drop the tools the catalogue hides, and mark those it lets run without asking.

    The server's own read-only hint is kept as it is.
    """
    return tuple(
        replace(tool, unasked=True) if tool.name in service.unasked_tools else tool
        for tool in tools if tool.name not in service.hidden_tools
    )


def failure_reason(error: BaseException, errors: HttpErrors) -> str:
    """A short reason for Activity and Settings, built from replies and errors, never the token."""
    if errors.last:
        return errors.last
    while isinstance(error, BaseExceptionGroup) and len(error.exceptions) == 1:
        error = error.exceptions[0]
    message = str(error).strip()
    return f"{type(error).__name__}: {message[:200]}" if message else type(error).__name__


class ServiceConnection:
    """Connects, lists the tools, then stays open until stopped or the server drops it."""

    def __init__(
        self, service: Service, token: str | None, report: Callable, open_client=open_http_client,
        open_local=open_stdio_client,
    ) -> None:
        self.service = service
        self.token = token
        self.report = report
        self.open_client = open_client
        self.open_local = open_local
        self.status = ServiceStatus(CONNECTING)
        self.stopped = asyncio.Event()
        # Set only while connected, for calling the server's tools.
        self.client = None

    def stop(self) -> None:
        self.stopped.set()

    async def run(self) -> None:
        # The client must be opened and closed in this same task, so it lives here throughout.
        errors = HttpErrors()
        try:
            opened = (
                self.open_local(self.service, errors) if self.service.command
                else self.open_client(self.service.url, self.token, errors)
            )
            async with opened as client:
                if self.stopped.is_set():
                    return
                tools = apply_catalogue(
                    await asyncio.wait_for(list_all_tools(client), LIST_TOOLS_SECONDS), self.service,
                )
                self.client = client
                self.set_status(ServiceStatus(CONNECTED, tools=tools))
                await self.stopped.wait()
        except Exception as error:
            self.client = None
            if not self.stopped.is_set():
                reason = failure_reason(error, errors)
                if self.service.command:
                    reason += f"; its own output is in {local_log_path(self.service.id)}"
                self.set_status(ServiceStatus(FAILED, reason))
            return
        self.client = None
        self.set_status(ServiceStatus(OFF))

    def set_status(self, status: ServiceStatus) -> None:
        self.status = status
        self.report(self, status)


class ServiceManager:
    """Starts and stops each service's connection and reports changes; the app schedules the tasks."""

    def __init__(
        self, schedule, write_activity, open_client=open_http_client, on_change=lambda: None,
        open_local=open_stdio_client,
    ) -> None:
        self.schedule = schedule
        self.write_activity = write_activity
        self.open_client = open_client
        self.open_local = open_local
        # Called whenever a service connects, fails or stops, so the app can update Cora's tools.
        self.on_change = on_change
        self.connections: dict[str, ServiceConnection] = {}

    async def call_tool(self, service_id: str, tool: str, arguments: dict, timeout: float = 60):
        """Call one tool on a connected service; raises if it isn't connected or takes too long."""
        connection = self.connections.get(service_id)
        client = connection.client if connection is not None else None
        if client is None:
            raise ConnectionError(f"{SERVICES[service_id].title} is not connected")
        return await asyncio.wait_for(client.call_tool(tool, arguments), timeout)

    async def wait_settled(self, timeout: float) -> None:
        """Wait, up to timeout, until no service is still connecting."""
        deadline = asyncio.get_running_loop().time() + timeout
        while any(c.status.state == CONNECTING for c in self.connections.values()):
            if asyncio.get_running_loop().time() >= deadline:
                return
            await asyncio.sleep(0.1)

    def status(self, service_id: str) -> ServiceStatus:
        connection = self.connections.get(service_id)
        return connection.status if connection is not None else ServiceStatus()

    def start(self, service_id: str, token: str | None = None) -> None:
        """Connect, replacing any earlier connection (such as one that failed with an old token)."""
        self.stop(service_id, quietly=True)
        connection = ServiceConnection(
            SERVICES[service_id], token, self.report, self.open_client, self.open_local,
        )
        self.connections[service_id] = connection
        self.schedule(connection.run())

    def stop(self, service_id: str, quietly: bool = False) -> None:
        connection = self.connections.pop(service_id, None)
        if connection is None:
            return
        connection.stop()
        if not quietly:
            self.write_activity(f"MCP {connection.service.title}: disconnected.", f"{connection.service.title} off")
        self.on_change()

    def stop_all(self) -> None:
        for service_id in list(self.connections):
            self.stop(service_id, quietly=True)

    def report(self, connection: ServiceConnection, status: ServiceStatus) -> None:
        # A replaced or stopped connection may still be closing; only the current one reports.
        if self.connections.get(connection.service.id) is not connection:
            return
        title = connection.service.title
        if status.state == CONNECTED:
            read_only = sum(tool.read_only for tool in status.tools)
            self.write_activity(
                f"MCP {title}: connected, {len(status.tools)} tools ({read_only} marked read-only): "
                + ", ".join(tool.name for tool in status.tools),
                f"{title} connected · {len(status.tools)} tools",
            )
        elif status.state == FAILED:
            self.write_activity(f"MCP {title}: connection failed ({status.reason}).", f"{title} connection failed")
        self.on_change()
