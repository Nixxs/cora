"""The local model (Rocky, her consultant) and Settings > Local model: part of CoraApp (see app.py).

Settings chooses the model and how long its container stays up after use, downloads the model and
tests it, and lets her consult Rocky (her consult_rocky tool): he answers in a thread of his own
(local_model/rocky.py), and his answer is shown word for word at the desk and on the web page, or she
reads it out or passes it on where it can't be shown. "@rocky …" is the person asking him
themselves, never through her (ask_rocky_directly). The container is looked after here: stopped once idle, and
when she quits (see local_model/manager.py).
"""

import asyncio
from contextlib import AsyncExitStack
from datetime import date
import json
import re
from time import monotonic
from typing import NamedTuple

from textual import work

from local_model.manager import IDLE_CHOICES, OFF, READY, STARTING, LocalModel
from local_model.ollama import OllamaClient, PullProgress, Turn, local_model_port, model_name, same_model
from local_model.rocky_browsing import (
    MAX_PAGES as ROCKY_MAX_PAGES, OUT_OF_ROOM as ROCKY_OUT_OF_ROOM, browse_and_answer, browsing_note,
    looked_line, no_browser_note,
)
from subconscious.interests.browse_exploration import BrowserAccess, Session
from local_model.rocky import (
    IMAGE_QUESTION, QUESTION_MAX_CHARS, GroupThread, rocky_image, stop_at, RockyThread, RockyUnavailable, answer_text, shown_so_far,
)
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
NO_ROCKY_MODEL = "Rocky isn't set up: choose and download a model in Settings › Local model."
# An image came, but his model can't take images: he says so rather than guessing.
BLIND_NOTE = ("\n\nAn image came with this message, but you can't see images: say so plainly, and don't "
              "guess what's in it.")
# How long he waits for her exploring to wrap up and free the browser, checking this often.
BROWSER_WAIT_SECONDS = 90
BROWSER_WAIT_POLL_SECONDS = 0.5
# Why he can't look things up when she consults him (Rocky 4b-ii).
NOT_ON_CONSULTS = "you can only look things up when the person asks you themselves"


class RockyReply(NamedTuple):
    """His answer, and what he actually did for it (rocky_browsing.looked_line)."""
    answer: str
    looked: str


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
        # And what the person has asked him directly (@rocky), apart from her.
        self.rocky_direct_thread = RockyThread(direct=True)
        # And the group chat's: the conversation as shown is his thread there.
        self.rocky_group_thread = GroupThread(lambda: self.conversation.group_transcript())
        self.rocky_lock = asyncio.Lock()
        # Whether each of his models takes images, once asked (rocky_sees).
        self.rocky_vision: dict[str, bool] = {}
        # While he waits for her exploring to free the browser: it wraps up, and doesn't start again.
        self.rocky_wants_browser = False
        self.set_interval(IDLE_CHECK_SECONDS, self.check_local_model_idle)

    def apply_local_model_settings(self, saved: dict) -> None:
        """Settings > Local model, once at launch."""
        name = saved.get("name")
        self.local_model_name = name if isinstance(name, str) else ""
        idle = saved.get("idle")
        self.local_model.idle_minutes = idle if idle in IDLE_CHOICES else self.local_model.idle_minutes
        self.local_model_consult = saved.get("consult") is True
        # The chat's chip appears once he has a model.
        self.set_chat_mode(self.chat_mode)

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
        try:
            answer = (await self.rocky_answer(self.rocky_thread, question, new_topic, "Asking Rocky.")).answer
        except RockyUnavailable as error:
            return {"status": "error", "message": f"{error} Say so; don't answer as him."}
        # Wherever she asked him: the desk and the web page each show the whole conversation.
        self.conversation.show_rocky(question, answer)
        self.web_feed.publish_rocky(question, answer)
        return {"status": "ok", "rocky_says": answer, "how_it_reaches_them": REACHES_THEM[channel]}

    async def ask_rocky_directly(self, question: str, device: str | None = None, ref: str | None = None,
                                 from_web: bool = False, on_text=None, on_status=None,
                                 image: bytes | None = None) -> str:
        """@rocky …: the person's question straight to Rocky, in a thread of their own with him (his
        thread with Cora is apart). Shown at the desk and on every web page, and never given to her:
        not saved and not in her history, so neither the question nor his answer reaches OpenAI.
        Needs only a model chosen, not her switch. He may browse first when the browser is free;
        what he's doing ("searching for …") shows in place of his answer meanwhile. His answer
        streams into the desk's chat as he writes it, and both go to on_text and on_status (the
        page that asked) if given. image: one sent with it, which he sees himself (never leaving
        this PC either); with no words, he's asked what he makes of it. Returns his answer;
        raises RockyUnavailable with what to tell the person (also shown at the desk)."""
        where = (f"web · {device}" if device else "web") if from_web else ""
        if image is not None and not question:
            question = IMAGE_QUESTION
        draft = self.conversation.ask_rocky_directly(question, where, image=image)

        def streamed(so_far: str) -> None:
            shown = shown_so_far(so_far, self.rocky_person_name())
            draft.update(shown)
            if on_text is not None:
                on_text(shown)

        def looking(what: str) -> None:
            draft.looking(what)
            if on_status is not None:
                on_status(what)

        try:
            if not question:
                raise RockyUnavailable("Ask Rocky something after @rocky.")
            if len(question) > QUESTION_MAX_CHARS:
                raise RockyUnavailable(f"That's too long for Rocky: at most {QUESTION_MAX_CHARS} characters.")
            if not self.local_model_name:
                raise RockyUnavailable(NO_ROCKY_MODEL)
            reply = await self.rocky_answer(self.rocky_direct_thread, question, False,
                                            "Asking Rocky (directly, from you).", on_text=streamed,
                                            on_status=looking, image=image)
        except RockyUnavailable as error:
            draft.fail(str(error))
            raise
        # Under it, what he actually did (from what was run, not what he says), so you can tell a
        # "source" he really read from one he made up.
        answer = f"{reply.answer}\n\n*{reply.looked}*"
        draft.finish(answer)
        self.web_feed.publish_rocky(f"[image] {question}" if image is not None else question, answer,
                                    asker="you", ref=ref, device=device)
        return answer

    async def ask_rocky_in_group(self, question: str, device: str | None = None, ref: str | None = None,
                                 on_text=None, on_status=None, image: bytes | None = None) -> str | None:
        """Rocky's turn in a group chat, before Cora's: he sees the conversation as shown (theirs,
        hers and his own lines; the person's message, already shown, last), answers it, browsing
        first if he chooses, and his answer streams into the desk's chat under it (and to on_text
        and on_status: the page that asked, if any). Unlike @rocky it's part of her conversation,
        so it goes to every web page and her next turn is given it. Returns his answer with the
        line under it, for her turn; None (with why, shown) if he hasn't one. Typed only, and in
        Standby or on the web page: while her voice is on, the chat is hers alone (set_chat_mode).
        image: one sent with the message, which he sees himself (she has her own look at it)."""
        if image is not None and not question:
            question = IMAGE_QUESTION
        draft = self.conversation.rocky_in_group()
        name = self.rocky_person_name()

        def streamed(so_far: str) -> None:
            shown = shown_so_far(so_far, name)
            draft.update(shown)
            if on_text is not None:
                on_text(shown)

        def looking(what: str) -> None:
            draft.looking(what)
            if on_status is not None:
                on_status(what)

        try:
            if not self.local_model_name:
                raise RockyUnavailable(NO_ROCKY_MODEL)
            reply = await self.rocky_answer(self.rocky_group_thread, question, False, "Asking Rocky (group chat).",
                                            on_text=streamed, on_status=looking, image=image)
        except RockyUnavailable as error:
            draft.fail(f"{error} Cora's answering on her own.")
            return None
        answer = f"{reply.answer}\n\n*{reply.looked}*"
        draft.finish(answer)
        self.web_feed.publish_rocky(question, answer, asker="group", ref=ref, device=device)
        return answer

    @work(group="rocky-direct", exit_on_error=False)
    async def ask_rocky_at_desk(self, question: str, image: bytes | None = None) -> None:
        """@rocky typed at the desk, or the chat on Rocky (see ask_rocky_directly), with an image
        pasted or chosen with +img, if any."""
        try:
            await self.ask_rocky_directly(question, image=image)
        except RockyUnavailable:
            pass  # Why is shown in the chat.

    async def rocky_answer(self, thread: RockyThread, question: str, new_topic: bool, asking: str,
                           on_text=None, on_status=None, image: bytes | None = None) -> RockyReply:
        """His answer in thread (and what he looked at for it), one question at a time; raises
        RockyUnavailable if there isn't one. The thread keeps the answer alone.
        on_text, if given, gets his answer so far as it streams in. With on_status he may browse
        first, if the browser is free (see rocky_browser), and on_status hears what he's doing.
        image: one sent with the question, which he sees himself (his own copy, rocky_image), if
        his model takes images; if it doesn't, he's told so and says so, rather than guessing."""
        images = None
        if image is not None:
            try:
                images = [await asyncio.to_thread(rocky_image, image)]
            except Exception:
                raise RockyUnavailable("That image couldn't be read for Rocky.") from None
        async with self.rocky_lock, AsyncExitStack() as holding:
            name = self.local_model_name
            began = monotonic()
            browser, why = (None, NOT_ON_CONSULTS) if on_status is None else await self.rocky_browser(on_status)
            if browser is not None:
                # Held from now (it's free, so this doesn't wait): her browser jobs wait their turn
                # and exploring doesn't start again.
                await holding.enter_async_context(self.browser_helper.lock)
            elif on_status is not None:
                # In place of his answer until he writes, so you know he hasn't looked.
                on_status(f"can't browse ({why})")
            self.activity.write(f"{asking} He can browse." if browser else f"{asking} No browser: {why}.", "Rocky")
            streaming = {} if on_text is None else {"on_text": on_text}
            blind = False
            try:
                async with self.local_model.use() as ollama:
                    if images and not await self.rocky_sees(ollama, name):
                        images, blind = None, True
                    # Named, so he knows who's who (he's local: the name stays on this PC).
                    messages = thread.messages(question, new_topic, self.rocky_person_name(), images=images)
                    if blind:
                        messages[0] = {**messages[0], "content": messages[0]["content"] + BLIND_NOTE}
                    if browser is None:
                        messages[0] = {**messages[0], "content": messages[0]["content"] + no_browser_note(
                            date.today(), why)}
                        # Stopped as he starts anyone else's turn ("\nCora:"), as a group chat invites.
                        reply = await ollama.chat(name, messages, stop=stop_at(self.rocky_person_name()),
                                                  **streaming)
                        looked = looked_line([], [], why)
                    else:
                        reply, looked = await self.rocky_browses(ollama, name, messages, browser, on_text,
                                                                 on_status, question)
            except Exception as error:
                self.activity.write(f"Rocky couldn't answer ({str(error)[:200] or type(error).__name__}).",
                                    "Rocky error")
                raise RockyUnavailable(
                    "Rocky couldn't answer just now (the local model didn't start or answer).") from None
            # Without a speaker label he copied, so his thread doesn't teach him the habit.
            answer = answer_text(reply, self.rocky_person_name())
            if not answer:
                raise RockyUnavailable("Rocky said nothing.")
            thread.add(question, answer, images)
        self.activity.write(f"Rocky answered ({monotonic() - began:.0f} s).", "Rocky")
        if blind:
            looked += " He couldn't see the image: his model doesn't take images."
        return RockyReply(answer, looked)

    async def rocky_sees(self, ollama, model: str) -> bool:
        """Whether his model takes images, asked of Ollama once per model (if it can't say, he's
        given the image anyway). Remembered for the chat's +img (rocky_takes_images)."""
        if model not in self.rocky_vision:
            try:
                self.rocky_vision[model] = await ollama.can_see(model)
            except Exception as error:
                self.activity.write(f"Couldn't ask whether Rocky's model takes images ({type(error).__name__}).",
                                    "Rocky")
                return True
            if not self.rocky_vision[model]:
                # The chip's Rocky mode stops offering +img.
                self.set_chat_mode(self.chat_mode)
        return self.rocky_vision[model]

    def rocky_takes_images(self) -> bool:
        """Whether +img is offered for him: unless his model is known not to take images (the
        chat's chips ask as they're drawn, which can be before his settings load)."""
        return getattr(self, "rocky_vision", {}).get(getattr(self, "local_model_name", "")) is not False

    def rocky_person_name(self) -> str:
        """The person's name as Cora knows it, or "" (then he's told "the person")."""
        name = getattr(getattr(self, "live", None), "person_name", "")
        return " ".join(name.split())[:60] if isinstance(name, str) else ""

    def exploring(self) -> bool:
        return self.exploration_worker is not None and not self.exploration_worker.is_finished

    async def rocky_browser(self, on_status) -> tuple[BrowserAccess | None, str]:
        """(the browser, "") for Rocky, or (None, why not). His when the Browser service is connected
        and nothing else is using it. Her exploring makes way: asked to wrap up, it's waited for
        (up to BROWSER_WAIT_SECONDS, "waiting for the browser" meanwhile). A sign-up or a job of
        her browser helper's isn't: he goes without."""
        if self.closing:
            return None, "Cora is closing"
        if self.signing_up() or self.browser_helper.lock.locked():
            return None, "Cora is using the browser for a job of hers"
        if self.exploring():
            on_status("waiting for the browser")
            self.rocky_wants_browser = True
            try:
                deadline = monotonic() + BROWSER_WAIT_SECONDS
                while self.exploring() and monotonic() < deadline and not self.closing:
                    await asyncio.sleep(BROWSER_WAIT_POLL_SECONDS)
            finally:
                self.rocky_wants_browser = False
            if self.exploring():
                return None, "Cora is still using the browser"
        browser = self.exploration_browser(asyncio.get_running_loop(), keep_going=lambda: not self.closing,
                                           title="Rocky")
        return (browser, "") if browser is not None else (None, "the browser is switched off")

    async def rocky_browses(self, ollama, name: str, messages: list[dict], browser: BrowserAccess,
                            on_text, on_status, question: str) -> tuple[str, str]:
        """(his reply, what he actually looked at), browsing first as he chooses
        (local_model/rocky_browsing.py), with the browser his (rocky_answer holds it). Its tab, and
        anything the pages stored, go when he's done."""
        messages[0] = {**messages[0], "content": messages[0]["content"] + browsing_note(date.today())}
        session = Session(browser, max_pages=ROCKY_MAX_PAGES, out_of_room_text=ROCKY_OUT_OF_ROOM)
        queries: list[str] = []

        async def run_tool(tool: str, arguments: str) -> str:
            if tool == "search":
                queries.append(" ".join(str(json.loads(arguments).get("query", "")).split()))
            try:
                return await asyncio.to_thread(session.run, tool, arguments)
            except Exception as error:
                self.activity.write(f"Rocky's browser didn't respond ({type(error).__name__}).", "Rocky")
                return "The browser didn't respond. Answer with what you know, and say you couldn't check."

        stop = stop_at(self.rocky_person_name())

        async def turn(sent: list[dict], tools, streamed) -> Turn:
            return await ollama.turn(name, sent, tools, streamed, stop=stop)

        def close_tab() -> None:
            try:
                browser.call("browser_close", {})
            except Exception:
                pass

        try:
            reply = await browse_and_answer(turn, messages, run_tool, lambda: session.out_of_room,
                                            on_text or (lambda so_far: None), on_status, question=question)
        finally:
            await asyncio.to_thread(close_tab)
        return reply, looked_line(queries, session.opened)

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
        self.set_chat_mode(self.chat_mode)
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
