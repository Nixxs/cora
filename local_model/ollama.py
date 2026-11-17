"""Ollama's own API, on 127.0.0.1 only (compose.yaml's local-model service).

Its chat replies come back whole, or streamed piece by piece for someone watching; the first can
take a while, since a model is loaded onto the GPU on first use.
"""

from collections.abc import Callable
from dataclasses import dataclass
import json
import re

from dotenv import dotenv_values
import httpx2

from messaging.container import ENV_FILE

DEFAULT_PORT = 11434
# Loading a model onto the GPU, then answering, on a 12 GB card.
CHAT_SECONDS = 300
CALL_SECONDS = 10
# A download goes quiet between its parts while Ollama checks them.
PULL_QUIET_SECONDS = 300
# His working memory, the same on every call (a different size reloads the model): room for a
# conversation and the couple of pages he reads when he browses. On Gemma 4 12B it costs about
# nothing over Ollama's 4096 (7.8 GB on the GPU either way).
CONTEXT_TOKENS = 16384
OPTIONS = {"num_ctx": CONTEXT_TOKENS}


def options(stop: list[str] | None = None) -> dict:
    """Ollama's options for a call: the context size, and where to stop if given (sampling only, so
    it never reloads the model)."""
    return {**OPTIONS, "stop": stop} if stop else OPTIONS
# An Ollama model: a library name (gemma3), a publisher's (huihui_ai/gemma-4-abliterated) or one
# from Hugging Face (hf.co/owner/repo), with a tag after a colon (12b, 31B-q4_K).
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*){0,2}(:[A-Za-z0-9][A-Za-z0-9._-]*)?")
MODEL_NAME_MAX_CHARS = 200


class OllamaError(Exception):
    """What Ollama said went wrong."""


@dataclass(frozen=True)
class PullProgress:
    """Where a download is: Ollama's own step ("pulling 6a0746a1ec1a", "verifying sha256 digest",
    "success"), with the bytes of the part it's on."""
    status: str
    completed: int = 0
    total: int = 0


def model_name(text: str) -> str:
    """A model name as typed, checked; ValueError if it isn't one."""
    name = text.strip()
    if len(name) > MODEL_NAME_MAX_CHARS or not MODEL_NAME.fullmatch(name):
        raise ValueError("That isn't an Ollama model name, such as huihui_ai/gemma-4-abliterated:12b.")
    return name


def same_model(installed: str, name: str) -> bool:
    """Ollama lists a model without a tag as :latest."""
    with_tag = name if ":" in name.rsplit("/", 1)[-1] else f"{name}:latest"
    return installed.casefold() == with_tag.casefold()


def local_model_port() -> int:
    """The local port Ollama is published on; CORA_LOCAL_MODEL_PORT in .env.database changes it."""
    value = dotenv_values(ENV_FILE).get("CORA_LOCAL_MODEL_PORT") if ENV_FILE.exists() else None
    return int(value) if value else DEFAULT_PORT


@dataclass(frozen=True)
class Loaded:
    """A model in memory: how big it is, and how much of it is on the GPU."""
    name: str
    size: int
    size_vram: int

    @property
    def on_gpu(self) -> float:
        return self.size_vram / self.size if self.size else 0.0


class OllamaClient:
    def __init__(self, base_url: str, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        # A stand-in for Ollama, in tests.
        self.transport = transport

    def client(self, timeout) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(timeout=timeout, transport=self.transport)

    async def get(self, path: str) -> dict:
        async with self.client(CALL_SECONDS) as client:
            response = await client.get(f"{self.base_url}{path}")
            response.raise_for_status()
            return response.json()

    async def version(self) -> str:
        """Ollama's version; raises while it isn't up yet."""
        return str((await self.get("/api/version")).get("version", ""))

    async def installed(self) -> list[str]:
        """The models downloaded so far."""
        return [str(model.get("name", "")) for model in (await self.get("/api/tags")).get("models", [])]

    async def loaded(self) -> list[Loaded]:
        """The models in memory now."""
        return [Loaded(str(model.get("name", "")), int(model.get("size") or 0), int(model.get("size_vram") or 0))
                for model in (await self.get("/api/ps")).get("models", [])]

    async def can_see(self, model: str) -> bool:
        """Whether a downloaded model takes images (Ollama lists "vision" among its capabilities)."""
        async with self.client(CALL_SECONDS) as client:
            response = await client.post(f"{self.base_url}/api/show", json={"model": model})
            response.raise_for_status()
            return "vision" in (response.json().get("capabilities") or [])

    async def pull(self, model: str, on_progress: Callable[[PullProgress], None]) -> None:
        """Download a model (or what's new in it), telling on_progress as it goes; OllamaError if
        Ollama can't (no such model, no space)."""
        timeout = httpx2.Timeout(PULL_QUIET_SECONDS, connect=CALL_SECONDS)
        async with self.client(timeout) as client:
            async with client.stream("POST", f"{self.base_url}/api/pull",
                                     json={"model": model, "stream": True}) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise OllamaError(error_text(response.text) or f"HTTP {response.status_code}")
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    step = json.loads(line)
                    if step.get("error"):
                        raise OllamaError(str(step["error"])[:200])
                    on_progress(PullProgress(str(step.get("status", "")), int(step.get("completed") or 0),
                                             int(step.get("total") or 0)))

    async def chat(self, model: str, messages: list[dict],
                   on_text: Callable[[str], None] | None = None, stop: list[str] | None = None) -> str:
        """The model's reply to a conversation ([{"role", "content"}, ...]), straight away: a model
        that can think first (Gemma 4, Qwen 3, DeepSeek R1) is asked not to, which takes it from
        minutes to seconds. One that can't is asked again without saying so. on_text, if given,
        gets the reply so far as it streams in; stop: where it stops writing (such as "\\nCora:")."""
        if on_text is not None:
            return (await self.turn(model, messages, on_text=on_text, stop=stop)).content
        request = {"model": model, "messages": messages, "stream": False, "think": False,
                   "options": options(stop)}
        async with self.client(CHAT_SECONDS) as client:
            response = await client.post(f"{self.base_url}/api/chat", json=request)
            if response.status_code >= 400 and "thinking" in error_text(response.text):
                del request["think"]
                response = await client.post(f"{self.base_url}/api/chat", json=request)
            if response.status_code >= 400:
                raise OllamaError(error_text(response.text) or f"HTTP {response.status_code}")
            return str(response.json().get("message", {}).get("content", ""))

    async def turn(self, model: str, messages: list[dict], tools: list[dict] | None = None,
                   on_text: Callable[[str], None] | None = None, stop: list[str] | None = None) -> "Turn":
        """One reply, streamed (one JSON object a line, each with the next piece): what it said,
        told to on_text as it comes, and any tools (in Ollama's form) it called."""
        request = {"model": model, "messages": messages, "stream": True, "think": False, "options": options(stop)}
        if tools:
            request["tools"] = tools
        # However long the whole reply takes, as long as the pieces keep coming.
        timeout = httpx2.Timeout(CHAT_SECONDS, connect=CALL_SECONDS)
        async with self.client(timeout) as client:
            for _ in range(2):
                async with client.stream("POST", f"{self.base_url}/api/chat", json=request) as response:
                    if response.status_code >= 400:
                        await response.aread()
                        error = error_text(response.text)
                        if "thinking" in error and "think" in request:
                            del request["think"]
                            continue
                        raise OllamaError(error or f"HTTP {response.status_code}")
                    said, calls = "", []
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        piece = json.loads(line)
                        if piece.get("error"):
                            raise OllamaError(str(piece["error"])[:200])
                        message = piece.get("message") or {}
                        if text := str(message.get("content") or ""):
                            said += text
                            if on_text is not None:
                                on_text(said)
                        calls += [call for call in message.get("tool_calls") or [] if isinstance(call, dict)]
                    return Turn(said, calls)
        raise OllamaError("the model wouldn't answer")


@dataclass(frozen=True)
class Turn:
    """A reply: what it said, and the tools it called ([{"function": {"name", "arguments"}}])."""
    content: str
    tool_calls: list[dict]


def error_text(body: str) -> str:
    """Ollama's {"error": "..."}, or the body as it is."""
    try:
        return str(json.loads(body).get("error", ""))[:200]
    except (ValueError, AttributeError):
        return body.strip()[:200]
