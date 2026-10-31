"""The services Cora knows how to connect to. Each is an entry here, mostly data rather than code."""

from dataclasses import dataclass, field
from pathlib import Path

# The project folder, holding compose.yaml and .env.database.
ROOT = Path(__file__).resolve().parent.parent

# Clicking and typing in the browser run without asking, so these limits go with them.
TYPING_RULE = (
    "Fine for searches, dates and filters. Never enter the person's personal details (name, address, "
    "email, phone, date of birth), payment details or passwords, and never fill in a booking, order, "
    "payment or sign-up: stop and tell the person what it needs instead."
)
CLICK_RULE = (
    "Never click to book, buy, pay, send, post or sign up: stop and tell the person what it would do instead."
)


@dataclass(frozen=True)
class Service:
    id: str
    title: str
    url: str  # a hosted MCP server, reached over Streamable HTTP; empty for a local one
    token_help: str
    # A local MCP server, started on this PC and reached over stdio, instead of a url.
    command: tuple[str, ...] = ()
    # Tools that run without asking the person first, though the server doesn't mark them read-only.
    unasked_tools: frozenset[str] = frozenset()
    # Tools never offered to Cora nor listed in Settings.
    hidden_tools: frozenset[str] = frozenset()
    # Tools whose "url" argument opens a page: checked first, so only public web pages are reached.
    page_tools: frozenset[str] = frozenset()
    # Cora's own notes on using a tool, added to the server's description.
    tool_notes: dict[str, str] = field(default_factory=dict, hash=False)

    @property
    def needs_token(self) -> bool:
        # A local server runs as Cora herself; only a hosted one needs a token.
        return not self.command


SERVICES: dict[str, Service] = {service.id: service for service in (
    Service(
        "github", "GitHub", "https://api.githubcopilot.com/mcp/",
        "A fine-grained personal access token, read-only, for the repositories Cora may see.",
    ),
    Service(
        "playwright", "Browser", "",
        "A private, headless browser in its own Docker container (Playwright), started while this is on. "
        "It keeps no logins, cookies or files between runs. Opening, reading and clicking run freely; "
        "typing, forms, uploads and running code ask you first.",
        # The browser service in compose.yaml (image, limits, own network), run for as long as it's on.
        command=(
            "docker", "compose", "-f", str(ROOT / "compose.yaml"), "--env-file", str(ROOT / ".env.database"),
            "--profile", "browser", "run", "--rm", "-T", "browser",
        ),
        # Moving around, clicking and filling in searches stay inside a browser with no logins or cookies,
        # so they can't buy, post or act as the person; what she may not enter is in the notes below.
        # Uploads and the rest still ask.
        unasked_tools=frozenset({
            "browser_navigate", "browser_navigate_back", "browser_tabs", "browser_close", "browser_click",
            "browser_hover", "browser_type", "browser_fill_form", "browser_select_option", "browser_press_key",
        }),
        # Running her own JavaScript in the page: the tools above cover what she needs, and code is
        # too hard to judge in a pop-up.
        hidden_tools=frozenset({"browser_run_code_unsafe", "browser_evaluate"}),
        page_tools=frozenset({"browser_navigate"}),
        tool_notes={
            # Its reply only links a snapshot file, which Cora can't open.
            "browser_navigate": "Its reply doesn't include the page's text: call browser_snapshot next to read it.",
            "browser_click": CLICK_RULE,
            **dict.fromkeys(("browser_type", "browser_fill_form", "browser_select_option"), TYPING_RULE),
        },
    ),
)}
