"""One MCP connection per service, held open by its own task, and the manager that runs them."""

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from .catalogue import SERVICES, Service

OFF, CONNECTING, CONNECTED, FAILED = "off", "connecting", "connected", "failed"


@dataclass(frozen=True)
class ServiceTool:
    name: str
    description: str
    read_only: bool  # the server's own hint, never a guarantee
    input_schema: dict


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

    def __init__(self, service: Service, token: str, report: Callable, open_client=open_http_client) -> None:
        self.service = service
        self.token = token
        self.report = report
        self.open_client = open_client
        self.status = ServiceStatus(CONNECTING)
        self.stopped = asyncio.Event()

    def stop(self) -> None:
        self.stopped.set()

    async def run(self) -> None:
        # The client must be opened and closed in this same task, so it lives here throughout.
        errors = HttpErrors()
        try:
            async with self.open_client(self.service.url, self.token, errors) as client:
                if self.stopped.is_set():
                    return
                self.set_status(ServiceStatus(CONNECTED, tools=await list_all_tools(client)))
                await self.stopped.wait()
        except Exception as error:
            if not self.stopped.is_set():
                self.set_status(ServiceStatus(FAILED, failure_reason(error, errors)))
            return
        self.set_status(ServiceStatus(OFF))

    def set_status(self, status: ServiceStatus) -> None:
        self.status = status
        self.report(self, status)


class ServiceManager:
    """Starts and stops each service's connection and reports changes; the app schedules the tasks."""

    def __init__(self, schedule, write_activity, open_client=open_http_client) -> None:
        self.schedule = schedule
        self.write_activity = write_activity
        self.open_client = open_client
        self.connections: dict[str, ServiceConnection] = {}

    def status(self, service_id: str) -> ServiceStatus:
        connection = self.connections.get(service_id)
        return connection.status if connection is not None else ServiceStatus()

    def start(self, service_id: str, token: str) -> None:
        """Connect, replacing any earlier connection (such as one that failed with an old token)."""
        self.stop(service_id, quietly=True)
        connection = ServiceConnection(SERVICES[service_id], token, self.report, self.open_client)
        self.connections[service_id] = connection
        self.schedule(connection.run())

    def stop(self, service_id: str, quietly: bool = False) -> None:
        connection = self.connections.pop(service_id, None)
        if connection is None:
            return
        connection.stop()
        if not quietly:
            self.write_activity(f"MCP {connection.service.title}: disconnected.", f"{connection.service.title} off")

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
