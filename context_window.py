"""What Cora's two models hold in context, item by item, for the dashboard."""

from dataclasses import dataclass
import json
import math

from session_config import LIVE_INSTRUCTIONS, RECONNECT_INSTRUCTIONS


VOICE = "voice"
BACKEND = "backend"
# Startup sections that get their own line, in the order they are sent; the name joins the instructions.
OWN_SECTIONS = ("self_image", "identity", "relationship", "people", "tasks", "memories", "interests")


def estimate_tokens(text: str) -> int:
    """About four UTF-8 bytes a token: good for comparing sizes, not an exact count."""
    return math.ceil(len(text.encode("utf-8")) / 4)


def short_tokens(tokens: int) -> str:
    """'850' under a thousand, else '4.1k'."""
    return f"{tokens / 1000:.1f}k" if tokens >= 1000 else str(tokens)


@dataclass(frozen=True)
class ContextItem:
    name: str
    side: str  # VOICE or BACKEND
    text: str  # exactly what was sent, or a readable rendering of it
    tokens: int


def render_history(items: list[dict]) -> str:
    """Reconnect history as 'You: …' and 'Cora: …' lines."""
    return "\n\n".join(
        f"{'You' if item['role'] == 'user' else 'Cora'}: {item['content'][0]['text']}" for item in items
    )


DELEGATED_NOTE = (
    "Voice requests Cora delegates are passed on by the Live service, which doesn't send their "
    "wording back, so they are counted here but can't be shown. The size is the last backend "
    "response's measured input, less the estimated instructions and tools."
)


class ContextTracker:
    """Records what each Live connection starts with and adds; the dashboard reads it."""

    def __init__(self) -> None:
        self.startup: tuple[ContextItem, ...] = ()
        # False once the connection closes; its numbers stay until the next one starts.
        self.active = False
        # What was said and noted to the voice model since the connection started.
        self.conversation = ""
        self.speaker = None
        self.typed_messages: list[str] = []
        # Measured by the API: the voice model's share of its limit, and the backend's last input.
        self.voice_ratio: float | None = None
        self.backend_tokens: int | None = None
        self.backend_model: str | None = None

    def start(self, config: dict, sections: dict[str, str]) -> None:
        """Note the payload a new connection starts with, replacing the last one's."""
        self.active = True
        self.conversation = ""
        self.speaker = None
        self.typed_messages = []
        self.voice_ratio = None
        self.backend_tokens = None
        items = [ContextItem(
            "instructions", VOICE, LIVE_INSTRUCTIONS + sections.get("name", ""),
            estimate_tokens(LIVE_INSTRUCTIONS + sections.get("name", "")),
        )]
        items += [
            ContextItem(name, VOICE, sections[name], estimate_tokens(sections[name]))
            for name in OWN_SECTIONS if name in sections
        ]
        if config.get("input"):
            # The reconnect note is part of the instructions, but only there because of the history.
            history = render_history(config["input"]) + RECONNECT_INSTRUCTIONS
            items.append(ContextItem("history", VOICE, history, estimate_tokens(history)))
        backend = config["delegation"]["responses"]
        self.backend_model = backend["model"]
        items.append(ContextItem(
            "instructions", BACKEND, backend["instructions"], estimate_tokens(backend["instructions"]),
        ))
        # Counted compact, as sent; shown indented so it can be read.
        items.append(ContextItem(
            "tools", BACKEND, json.dumps(backend["tools"], indent=2, ensure_ascii=False),
            estimate_tokens(json.dumps(backend["tools"], separators=(",", ":"), ensure_ascii=False)),
        ))
        self.startup = tuple(items)

    def end(self) -> None:
        self.active = False

    def observe(self, event) -> None:
        """Take what the context holds from a Live server event: speech, and the API's measurements."""
        if event.type in ("session.input_transcript.delta", "session.output_transcript.delta"):
            if event.delta:
                self.transcript("You" if event.type == "session.input_transcript.delta" else "Cora", event.delta)
        elif event.type == "session.usage.updated":
            # Omitted while the API doesn't know the limit.
            if event.context_window is not None:
                self.voice_ratio = event.context_window.usage_ratio
        elif event.type == "response.event" and event.event.get("type") == "response.completed":
            usage = (event.event.get("response") or {}).get("usage") or {}
            if usage.get("input_tokens") is not None:
                self.backend_tokens = usage["input_tokens"]

    def transcript(self, speaker: str, delta: str) -> None:
        if speaker != self.speaker:
            self.conversation += ("\n\n" if self.conversation else "") + f"{speaker}: "
            self.speaker = speaker
        self.conversation += delta

    def note(self, text: str) -> None:
        """Something the app added to the voice model's context, like a greeting or a typed mirror."""
        self.conversation += ("\n\n" if self.conversation else "") + f"[note] {text}"
        self.speaker = None

    def typed(self, text: str) -> None:
        """A typed message sent straight to the backend."""
        self.typed_messages.append(text)

    def items(self, side: str) -> tuple[ContextItem, ...]:
        items = tuple(item for item in self.startup if item.side == side)
        if side == VOICE and self.conversation:
            items += (ContextItem("conversation", VOICE, self.conversation, estimate_tokens(self.conversation)),)
        if side == BACKEND and self.backend_tokens is not None:
            typed = "\n\n".join(f"You: {message}" for message in self.typed_messages) or "No typed messages yet."
            items += (ContextItem(
                "delegated turns", BACKEND, f"{typed}\n\n{DELEGATED_NOTE}",
                max(self.backend_tokens - sum(item.tokens for item in items), 0),
            ),)
        return items
