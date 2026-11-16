"""Ollama's own API, on 127.0.0.1 only (compose.yaml's local-model service).

Its chat replies come back whole (no streaming); they can take a while, since a model is loaded
onto the GPU on first use.
"""

from dataclasses import dataclass

from dotenv import dotenv_values
import httpx2

from messaging.container import ENV_FILE

DEFAULT_PORT = 11434
# Loading a model onto the GPU, then answering, on a 12 GB card.
CHAT_SECONDS = 300
CALL_SECONDS = 10


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
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    async def get(self, path: str) -> dict:
        async with httpx2.AsyncClient(timeout=CALL_SECONDS) as client:
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

    async def chat(self, model: str, messages: list[dict]) -> str:
        """The model's reply to a conversation ([{"role", "content"}, ...])."""
        async with httpx2.AsyncClient(timeout=CHAT_SECONDS) as client:
            response = await client.post(f"{self.base_url}/api/chat",
                                         json={"model": model, "messages": messages, "stream": False})
            response.raise_for_status()
            return str(response.json().get("message", {}).get("content", ""))
