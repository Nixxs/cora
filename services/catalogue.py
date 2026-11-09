"""The services Cora knows how to connect to. Each is an entry here, mostly data rather than code."""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
import re

# The project folder, holding compose.yaml and .env.database.
ROOT = Path(__file__).resolve().parent.parent

# Clicking and typing in the browser run without asking, so these limits go with them. Filling in a
# form only puts it on the page; sending it is the click below, which asks the person first.
TYPING_RULE = (
    "Fine for searches, dates and filters, and for filling in a form the person asked you to complete "
    "for them, with their own details (name, address, email, phone, date of birth) from what you know "
    "or they told you; ask them for anything you don't know rather than guessing. Never enter payment "
    "or card details or passwords. Press Enter or use submit only for a search: send a form with its "
    "button. To sign up somewhere for yourself, use request_sign_up, never these tools."
)
CLICK_RULE = (
    "Clicking to submit, send, book, buy, pay, post or sign up asks the person first: name the control "
    "plainly in element (such as 'Submit application button') so they know what they're approving, and "
    "only send a form they asked you to complete. Never pay or enter card details "
    "(for an account of your own, use request_sign_up)."
)
# A click on a control named like one of these sends something, so it asks first (a nav link that
# matches just asks too).
SENDING_WORDS = re.compile(
    r"\b(submit|send|book|buy|order|pay|purchase|checkout|check out|confirm|apply|register|sign up|"
    r"reserve|place|complete|finish|post|continue to payment)\b",
    re.IGNORECASE,
)


def sends_something(arguments: dict) -> bool:
    """Whether a browser click looks like it sends a form, from how Cora named the control."""
    return bool(SENDING_WORDS.search(str(arguments.get("element") or "")))


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
    # Tools that run without asking except for calls these say yes to, which ask the person first.
    asks_when: dict[str, Callable[[dict], bool]] = field(default_factory=dict, hash=False)
    # Tools used by a helper on a model of their own (use_browser) rather than offered to her directly.
    behind_helper: bool = False

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
        "It keeps no logins, cookies or files between runs. Opening, reading, clicking and filling in "
        "forms run freely; sending a form (submit, book, apply…) and uploads ask you first.",
        # The browser service in compose.yaml (image, limits, own network), run for as long as it's on.
        command=(
            "docker", "compose", "-f", str(ROOT / "compose.yaml"), "--env-file", str(ROOT / ".env.database"),
            "--profile", "browser", "run", "--rm", "-T", "browser",
        ),
        # Moving around, clicking and filling in fields stay inside a browser with no logins or cookies;
        # a click that sends something asks first (asks_when), and what she may not enter is in the
        # notes below. Uploads and the rest still ask.
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
        asks_when={"browser_click": sends_something},
        # She asks use_browser for a job; its helper uses these on the Browser model.
        behind_helper=True,
    ),
)}
