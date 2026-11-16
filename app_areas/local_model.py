"""The local model (Rocky, her consultant) and Settings > Local model: part of CoraApp (see app.py).

Settings chooses the model and how long its container stays up after use, downloads the model and
tests it, and lets her consult Rocky (her consult_rocky tool): he answers in a thread of his own
(local_model/rocky.py), and his answer is shown word for word at the desk and on the web page, or she
reads it out or passes it on where it can't be shown. The container is looked after here: stopped once idle, and
when she quits (see local_model/manager.py).
"""

import asyncio
import re
from time import monotonic

from textual import work

from local_model.manager import IDLE_CHOICES, OFF, READY, STARTING, LocalModel
from local_model.ollama import OllamaClient, PullProgress, local_model_port, model_name, same_model
from local_model.rocky import RockyThread, answer_text
from tools.route import current_route

# How often she looks for it left idle long enough to stop.
IDLE_CHECK_SECONDS = 60
# Never long enough to hold up closing.
STOP_SECONDS = 20
TEST_QUESTION = "In one short sentence, say hello and tell me who you are."
SHOWN_REPLY_CHARS = 300
PROGRESS_SHOWN_BYTES = 50_000_000
THINKING = re.compile(r"<think>.*?</think>", re.DOTALL)
# How Rocky's answer reaches the person, by the conversation she asked him in, and what that leaves her
# to do. By voice (Live, or a call) it's shown at the desk too, but they may not be looking.
REACHES_THEM = {
    "voice": ("You're talking: read his answer out to them in his words (shorten a long one, keeping what "
              "matters), then give your own view. It's also shown in the chat at the desk and on the web page."),
    "desk": ("It's shown to them in the chat word for word, just above your reply: don't repeat it; give "
             "your own view, and a caution only if it really matters."),
    "web": ("It's shown to them on the web page word for word, just above your reply: don't repeat it; give "
            "your own view, and a caution only if it really matters."),
    "signal": ("They're on Signal and can't see it: pass on what he said, in his words where you can and "
               "leaving out nothing that matters, then give your own view."),
    "email": ("They're reading your email and can't see it: pass on what he said, in his words where you "
              "can and leaving out nothing that matters, then give your own view."),
}
ROCKY_OFF = ("Rocky isn't switched on: the person can let you consult him in Settings > Local model, once "
             "a model is chosen and downloaded.")
ROCKY_NOT_HERE = "Rocky is only for the person's own conversations, not this one."


def gigabytes(size: int) -> str:
    return f"{size / 1e9:.1f} GB"


class LocalModelMixin:
    """The local model's container, and Settings > Local model."""

    def init_local_model(self) -> None:
        self.local_model = LocalModel(OllamaClient(f"http://127.0.0.1:{local_model_port()}"),
                                      write_activity=lambda *line: self.activity.write(*line))
        # Settings > Local model, as loaded at launch: "" until a model is chosen.
        self.local_model_name = ""
        # Settings' line for a download or a test: how it's going, or how it went.
        self.local_model_task = ""
        self.local_model_busy = False
        # The models downloaded, once the container has been up this launch (None until then).
        self.local_model_installed: list[str] | None = None
        # Whether she may consult Rocky, and what she's asked him lately (one question at a time).
        self.local_model_consult = False
        self.rocky_thread = RockyThread()
        self.rocky_lock = asyncio.Lock()
        self.set_interval(IDLE_CHECK_SECONDS, self.check_local_model_idle)

    def apply_local_model_settings(self, saved: dict) -> None:
        """Settings > Local model, once at launch."""
        name = saved.get("name")
        self.local_model_name = name if isinstance(name, str) else ""
        idle = saved.get("idle")
        self.local_model.idle_minutes = idle if idle in IDLE_CHOICES else self.local_model.idle_minutes
        self.local_model_consult = saved.get("consult") is True

    def can_consult_rocky(self) -> bool:
        """Whether her consult_rocky tool is offered: switched on, with a model chosen."""
        return self.local_model_consult and bool(self.local_model_name)

    async def consult_rocky(self, question: str, new_topic: bool) -> dict:
        """consult_rocky: his answer for her, shown to the person, with how it reaches them here."""
        route = current_route.get()
        channel = "voice" if route is None else getattr(route, "channel", "")
        if channel not in REACHES_THEM:
            return {"status": "error", "message": ROCKY_NOT_HERE}
        if not self.can_consult_rocky():
            return {"status": "error", "message": ROCKY_OFF}
        async with self.rocky_lock:
            name = self.local_model_name
            self.activity.write("Asking Rocky.", "Rocky")
            began = monotonic()
            try:
                async with self.local_model.use() as ollama:
                    reply = await ollama.chat(name, self.rocky_thread.messages(question, new_topic))
            except Exception as error:
                self.activity.write(f"Rocky couldn't answer ({str(error)[:200] or type(error).__name__}).",
                                    "Rocky error")
                return {"status": "error", "message": (
                    "Rocky couldn't answer just now (the local model didn't start or answer). Say so; "
                    "don't answer as him.")}
            answer = answer_text(reply)
            if not answer:
                return {"status": "error", "message": "Rocky said nothing. Say so; don't answer as him."}
            self.rocky_thread.add(question, answer)
        self.activity.write(f"Rocky answered ({monotonic() - began:.0f} s).", "Rocky")
        # Wherever she asked him: the desk and the web page each show the whole conversation.
        self.conversation.show_rocky(question, answer)
        self.web_feed.publish_rocky(question, answer)
        return {"status": "ok", "rocky_says": answer, "how_it_reaches_them": REACHES_THEM[channel]}

    @work(group="model-settings-save")
    async def save_local_model_consult(self, on: bool) -> None:
        self.local_model_consult = on
        self.activity.write(f"Consulting Rocky {'on' if on else 'off'}: in writing now, by voice from her "
                            "next connect.", "Settings changed")
        try:
            await self.conversation_persistence.call_database(self.persistence.save_local_model_consult, on)
        except Exception as error:
            self.activity.write(f"Local model setting save failed ({type(error).__name__}).", "Settings error")
            self.notify("Could not save consulting Rocky; it applies until Cora closes.", severity="error")

    @work(group="local-model-idle", exclusive=True, exit_on_error=False)
    async def check_local_model_idle(self) -> None:
        if not self.closing:
            await self.local_model.stop_if_idle()

    async def close_local_model(self) -> None:
        try:
            await asyncio.wait_for(self.local_model.shut_down("Cora is closing"), STOP_SECONDS)
        except Exception:
            pass  # Stopped on her next launch instead.

    def local_model_status(self) -> str:
        """Settings' line for the container, and what's downloaded once that's known."""
        state, idle = self.local_model.state, self.local_model.idle_minutes
        line = {OFF: f"○ Off · starts when needed, stops after {idle} minutes unused",
                STARTING: "◌ Starting…", READY: f"● Up · stops after {idle} minutes unused"}.get(state, "◌ Stopping…")
        if self.local_model_installed is not None:
            line += "\nDownloaded: " + (", ".join(self.local_model_installed) or "nothing yet")
        return line

    @work(group="model-settings-save")
    async def save_local_model_name(self, text: str) -> None:
        try:
            name = model_name(text)
        except ValueError as error:
            self.notify(str(error), severity="warning")
            return
        self.local_model_name = name
        try:
            await self.conversation_persistence.call_database(self.persistence.save_local_model_name, name)
        except Exception as error:
            self.activity.write(f"Local model setting save failed ({type(error).__name__}).", "Settings error")
            self.notify("Could not save the model; it applies until Cora closes.", severity="error")
            return
        self.notify(f"Rocky's model is {name}. Download it if you haven't.")

    @work(group="model-settings-save")
    async def save_local_model_idle(self, minutes: int) -> None:
        if minutes not in IDLE_CHOICES:
            return
        self.local_model.idle_minutes = minutes
        try:
            await self.conversation_persistence.call_database(self.persistence.save_local_model_idle, minutes)
        except Exception as error:
            self.activity.write(f"Local model setting save failed ({type(error).__name__}).", "Settings error")
            self.notify("Could not save how long it stays up; it applies until Cora closes.", severity="error")

    def start_local_model_task(self, task) -> None:
        """A download or a test, one at a time, once a model is chosen."""
        if self.local_model_busy:
            self.notify("The local model is busy with a download or test; wait for it to finish.",
                        severity="warning")
            return
        if not self.local_model_name:
            self.notify("Choose a model and save it first.", severity="warning")
            return
        self.local_model_busy = True
        self.run_worker(self.local_model_task_run(task), group="local-model-task", exit_on_error=False)

    def download_local_model(self) -> None:
        self.start_local_model_task(self.download_model)

    def test_local_model(self) -> None:
        self.start_local_model_task(self.test_model)

    async def local_model_task_run(self, task) -> None:
        name = self.local_model_name
        try:
            self.local_model_task = "◌ Starting the local model…"
            async with self.local_model.use() as ollama:
                self.local_model_installed = await ollama.installed()
                self.local_model_task = await task(ollama, name)
                self.local_model_installed = await ollama.installed()
        except Exception as error:
            self.local_model_task = f"× {str(error)[:200] or type(error).__name__}"
            self.activity.write(f"Local model: {name} {self.local_model_task[2:]}", "Local model error")
        finally:
            self.local_model_busy = False

    async def download_model(self, ollama, name: str) -> str:
        self.local_model_task = f"◌ Downloading {name}…"
        began = monotonic()

        def progress(step: PullProgress) -> None:
            # The model itself is the big part; the small ones (its settings, licence) say their step.
            if step.total >= PROGRESS_SHOWN_BYTES:
                self.local_model_task = (f"◌ Downloading {name}: {step.completed / step.total:.0%} of "
                                         f"{gigabytes(step.total)}")
            elif step.status:
                self.local_model_task = f"◌ {name}: {step.status}"

        await ollama.pull(name, progress)
        self.activity.write(f"Downloaded the local model {name} ({monotonic() - began:.0f} s).", "Local model")
        return f"● Downloaded {name}. Test it to see how it answers."

    async def test_model(self, ollama, name: str) -> str:
        if not any(same_model(installed, name) for installed in self.local_model_installed or []):
            return f"○ {name} isn't downloaded yet: download it first."
        self.local_model_task = f"◌ Asking {name} (loading it onto the GPU first)…"
        began = monotonic()
        reply = await ollama.chat(name, [{"role": "user", "content": TEST_QUESTION}])
        seconds = monotonic() - began
        reply = " ".join(THINKING.sub("", reply).split())
        if len(reply) > SHOWN_REPLY_CHARS:
            reply = reply[:SHOWN_REPLY_CHARS - 1] + "…"
        loaded = next((model for model in await ollama.loaded() if same_model(model.name, name)), None)
        where = f", {loaded.on_gpu:.0%} on the GPU ({gigabytes(loaded.size)})" if loaded is not None else ""
        self.activity.write(f"Tested the local model {name}: answered in {seconds:.1f} s{where}.", "Local model")
        return f"● Answered in {seconds:.1f} s{where}:\n{reply or '(nothing)'}"
