"""The local model's container (Rocky, her consultant): part of CoraApp (see app.py).

Nothing uses it yet; Settings (download and test) and her consult tool come next. Here it's only
looked after: stopped once idle, and when she quits (see local_model/manager.py).
"""

import asyncio

from textual import work

from local_model.manager import LocalModel
from local_model.ollama import OllamaClient, local_model_port

# How often she looks for it left idle long enough to stop.
IDLE_CHECK_SECONDS = 60
# Never long enough to hold up closing.
STOP_SECONDS = 20


class LocalModelMixin:
    """Looking after the local model's container."""

    def init_local_model(self) -> None:
        self.local_model = LocalModel(OllamaClient(f"http://127.0.0.1:{local_model_port()}"),
                                      write_activity=lambda *line: self.activity.write(*line))
        self.set_interval(IDLE_CHECK_SECONDS, self.check_local_model_idle)

    @work(group="local-model-idle", exclusive=True, exit_on_error=False)
    async def check_local_model_idle(self) -> None:
        if not self.closing:
            await self.local_model.stop_if_idle()

    async def close_local_model(self) -> None:
        try:
            await asyncio.wait_for(self.local_model.shut_down("Cora is closing"), STOP_SECONDS)
        except Exception:
            pass  # Stopped on her next launch instead.
