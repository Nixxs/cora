"""The services Cora knows how to connect to. Each is an entry here, mostly data rather than code."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Service:
    id: str
    title: str
    url: str  # a hosted MCP server, reached over Streamable HTTP
    token_help: str


SERVICES: dict[str, Service] = {service.id: service for service in (
    Service(
        "github", "GitHub", "https://api.githubcopilot.com/mcp/",
        "A fine-grained personal access token, read-only, for the repositories Cora may see.",
    ),
)}
