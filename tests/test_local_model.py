"""The local model's container: started when something needs it, stopped once idle or when she quits."""

import asyncio
import json
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx2 as httpx
from textual.widgets import Button, Input, Select, Static, TabbedContent

from app_areas.local_model import LocalModelMixin
from local_model.manager import OFF, READY, LocalModel, LocalModelError
from local_model.ollama import Loaded, OllamaClient, OllamaError, PullProgress, model_name, same_model
from messaging.container import compose
from session_config import BACKEND_MODEL, MODEL_AREAS
from ui.model_settings import ModelSettingsScreen
import test_keyboard_persistence as keyboard_tests


class FakeOllama:
    def __init__(self, ready_after=0):
        # Answers once it's been asked this many times (Ollama starting up).
        self.ready_after = ready_after
        self.asked = 0

    async def version(self):
        self.asked += 1
        if self.asked <= self.ready_after:
            raise ConnectionError("not up yet")
        return "0.40.16"


class LocalModelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 1000.0
        self.lines = []
        self.start, self.stop = AsyncMock(), AsyncMock()
        self.ollama = FakeOllama(ready_after=2)
        self.model = self.make()

    def make(self, **options):
        return LocalModel(self.ollama, write_activity=lambda *line: self.lines.append(line), start=self.start,
                          stop=self.stop, clock=lambda: self.now, poll_seconds=0, **options)

    async def test_started_once_when_needed_and_up_while_held(self):
        async def use():
            async with self.model.use() as ollama:
                self.assertIs(ollama, self.ollama)
                self.assertEqual(self.model.state, READY)
                await asyncio.sleep(0)

        await asyncio.gather(use(), use())
        self.start.assert_awaited_once()
        self.assertEqual(self.model.in_use, 0)
        self.assertEqual([line[0] for line in self.lines][0], "Starting the local model.")
        self.assertIn("is up (Ollama 0.40.16", self.lines[-1][0])

    async def test_one_that_wont_start_says_why_and_is_stopped_again(self):
        self.start.side_effect = RuntimeError("no GPU")
        with self.assertRaises(LocalModelError):
            async with self.model.use():
                pass
        self.assertEqual(self.model.state, OFF)
        self.stop.assert_awaited()
        self.assertIn("didn't start (no GPU)", self.lines[-1][0])
        self.assertEqual(self.model.in_use, 0)

    async def test_ollama_not_answering_in_time_is_a_failed_start(self):
        self.ollama.ready_after = 10 ** 6
        self.model = self.make(ready_seconds=0)
        with self.assertRaises(LocalModelError):
            await self.model.ensure_up()
        self.assertIn("didn't answer within", self.lines[-1][0])

    async def test_stopped_once_idle_but_never_while_in_use(self):
        await self.model.stop_if_idle()  # the first check: one left running before is stopped
        self.stop.assert_awaited_once()
        self.stop.reset_mock()
        async with self.model.use():
            self.now += 60 * 60
            await self.model.stop_if_idle()
            self.stop.assert_not_awaited()
        self.now += 14 * 60
        await self.model.stop_if_idle()
        self.stop.assert_not_awaited()
        self.now += 60
        await self.model.stop_if_idle()
        self.stop.assert_awaited_once()
        self.assertEqual(self.model.state, OFF)
        self.assertIn("idle for 15 minutes", self.lines[-1][0])
        # Needed again: started again.
        async with self.model.use():
            self.assertEqual(self.start.await_count, 2)

    async def test_stopped_when_she_quits_and_not_when_it_was_never_started(self):
        await self.model.shut_down("Cora is closing")
        self.stop.assert_not_awaited()
        await self.model.ensure_up()
        await self.model.shut_down("Cora is closing")
        self.stop.assert_awaited_once()
        self.assertEqual(self.model.state, OFF)


class ComposeTests(unittest.TestCase):
    def test_each_service_uses_its_own_profile(self):
        done = subprocess.CompletedProcess([], 0, stdout="ok", stderr="")
        with patch("messaging.container.ENV_FILE") as env, patch("subprocess.run", return_value=done) as run:
            env.exists.return_value = True
            self.assertEqual(compose("local-model", "up", "-d", "local-model"), "ok")
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--profile") + 1], "local-model")
        self.assertEqual(command[-3:], ["up", "-d", "local-model"])

    def test_how_much_of_a_model_is_on_the_gpu(self):
        self.assertEqual(Loaded("m", 8_000, 6_000).on_gpu, 0.75)
        self.assertEqual(Loaded("m", 0, 0).on_gpu, 0.0)


def stand_in_ollama(installed=(), pull_lines=None, reply="Hello, I'm Rocky.", pull_status=200):
    """Ollama's API as far as Settings uses it."""
    asked = []

    def handle(request):
        asked.append((request.method, request.url.path, json.loads(request.content or b"{}")))
        path = request.url.path
        if path == "/api/version":
            return httpx.Response(200, json={"version": "0.40.16"})
        if path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": name} for name in installed]})
        if path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": name, "size": 8_000_000_000,
                                                        "size_vram": 8_000_000_000} for name in installed]})
        if path == "/api/pull":
            if pull_status != 200:
                return httpx.Response(pull_status, json={"error": "pull model manifest: file does not exist"})
            lines = pull_lines or [{"status": "pulling manifest"},
                                   {"status": "pulling 6a07", "total": 7_600_000_000, "completed": 3_800_000_000},
                                   {"status": "success"}]
            return httpx.Response(200, content="\n".join(json.dumps(line) for line in lines).encode())
        if path == "/api/chat":
            return httpx.Response(200, json={"message": {"role": "assistant", "content": reply}})
        return httpx.Response(404)

    return OllamaClient("http://ollama.test", transport=httpx.MockTransport(handle)), asked


class OllamaClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_download_reports_its_progress(self):
        ollama, asked = stand_in_ollama()
        steps = []
        await ollama.pull("huihui_ai/gemma-4-abliterated:12b", steps.append)
        self.assertEqual(asked[0][2], {"model": "huihui_ai/gemma-4-abliterated:12b", "stream": True})
        self.assertEqual(steps[1], PullProgress("pulling 6a07", 3_800_000_000, 7_600_000_000))
        self.assertEqual(steps[-1].status, "success")

    async def test_ollama_saying_no_is_an_error_with_its_reason(self):
        ollama, _ = stand_in_ollama(pull_lines=[{"status": "pulling manifest"}, {"error": "no space left on device"}])
        with self.assertRaisesRegex(OllamaError, "no space left"):
            await ollama.pull("x", lambda step: None)
        ollama, _ = stand_in_ollama(pull_status=404)
        with self.assertRaisesRegex(OllamaError, "file does not exist"):
            await ollama.pull("nobody/nothing", lambda step: None)

    async def test_what_is_downloaded_loaded_and_said(self):
        ollama, asked = stand_in_ollama(installed=["huihui_ai/gemma-4-abliterated:12b"])
        self.assertEqual(await ollama.installed(), ["huihui_ai/gemma-4-abliterated:12b"])
        self.assertEqual((await ollama.loaded())[0].on_gpu, 1.0)
        self.assertEqual(await ollama.chat("m", [{"role": "user", "content": "hi"}]), "Hello, I'm Rocky.")
        self.assertEqual(asked[-1][2]["stream"], False)

    async def test_answers_straight_away_without_thinking_first(self):
        asked = []

        def handle(request):
            sent = json.loads(request.content)
            asked.append(sent)
            if sent.get("think") is False and sent["model"] == "old-model":
                return httpx.Response(400, json={"error": "\"old-model\" does not support thinking"})
            return httpx.Response(200, json={"message": {"content": "Hi."}})

        ollama = OllamaClient("http://ollama.test", transport=httpx.MockTransport(handle))
        self.assertEqual(await ollama.chat("gemma", [{"role": "user", "content": "hi"}]), "Hi.")
        self.assertIs(asked[-1]["think"], False)
        # A model that can't think is asked again without it.
        self.assertEqual(await ollama.chat("old-model", [{"role": "user", "content": "hi"}]), "Hi.")
        self.assertNotIn("think", asked[-1])

    async def test_a_streamed_answer_comes_piece_by_piece(self):
        asked = []

        def handle(request):
            sent = json.loads(request.content)
            asked.append(sent)
            if sent.get("think") is False and sent["model"] == "old-model":
                return httpx.Response(400, json={"error": "\"old-model\" does not support thinking"})
            pieces = [{"message": {"content": "Hey "}}, {"message": {"content": "mate."}}, {"done": True}]
            return httpx.Response(200, content="\n".join(json.dumps(piece) for piece in pieces).encode())

        ollama = OllamaClient("http://ollama.test", transport=httpx.MockTransport(handle))
        for model in ("gemma", "old-model"):
            seen = []
            self.assertEqual(await ollama.chat(model, [{"role": "user", "content": "hi"}], on_text=seen.append),
                             "Hey mate.")
            self.assertEqual(seen, ["Hey ", "Hey mate."])
            self.assertIs(asked[-1]["stream"], True)
        self.assertNotIn("think", asked[-1])
        # Where to stop goes with the options, beside the context size (which must never change).
        await ollama.chat("gemma", [], on_text=seen.append, stop=["\nCora:"])
        self.assertEqual(asked[-1]["options"], {"num_ctx": 16384, "stop": ["\nCora:"]})

        def broken(request):
            return httpx.Response(200, content=b'{"message": {"content": "Hey"}}\n{"error": "out of memory"}\n')

        ollama = OllamaClient("http://ollama.test", transport=httpx.MockTransport(broken))
        with self.assertRaisesRegex(OllamaError, "out of memory"):
            await ollama.chat("gemma", [], on_text=lambda so_far: None)

    async def test_whether_a_model_takes_images(self):
        for capabilities, sees in ((["completion", "vision", "tools"], True), (["completion"], False), (None, False)):
            def handle(request, capabilities=capabilities):
                self.assertEqual(json.loads(request.content), {"model": "gemma"})
                return httpx.Response(200, json={"capabilities": capabilities})

            ollama = OllamaClient("http://ollama.test", transport=httpx.MockTransport(handle))
            self.assertIs(await ollama.can_see("gemma"), sees)

    def test_model_names(self):
        for name in ("gemma3", "huihui_ai/gemma-4-abliterated:12b", "hf.co/owner/repo:Q4_K_M", "x:31B-q4_K"):
            self.assertEqual(model_name(f"  {name} "), name)
        for bad in ("", "a b", "../x", "x:", "-x", "a/b/c/d", "x;rm", "x" * 201):
            with self.assertRaises(ValueError):
                model_name(bad)
        self.assertTrue(same_model("gemma3:latest", "gemma3"))
        self.assertTrue(same_model("huihui_ai/Gemma:12b", "huihui_ai/gemma:12b"))
        self.assertFalse(same_model("gemma3:12b", "gemma3"))


class SettingsTaskTests(unittest.IsolatedAsyncioTestCase):
    """Settings > Local model's Download and Test, as the app runs them."""

    def setUp(self):
        self.lines = []
        self.app = SimpleNamespace(local_model_name="huihui_ai/gemma-4-abliterated:12b", local_model_task="",
                                   local_model_busy=True, local_model_installed=None,
                                   activity=SimpleNamespace(write=lambda *line: self.lines.append(line)))
        for method in ("download_model", "test_model"):
            setattr(self.app, method, getattr(LocalModelMixin, method).__get__(self.app))

    def use(self, ollama):
        self.app.local_model = LocalModel(ollama, write_activity=lambda *line: None, start=AsyncMock(),
                                          stop=AsyncMock(), poll_seconds=0)

    async def run_task(self, task):
        await LocalModelMixin.local_model_task_run(self.app, task)
        return self.app.local_model_task

    async def test_download_shows_progress_and_then_whats_downloaded(self):
        ollama, _ = stand_in_ollama()
        shown = []
        real_pull = ollama.pull

        async def pull(model, on_progress):
            await real_pull(model, lambda step: (on_progress(step), shown.append(self.app.local_model_task)))

        ollama.pull = pull
        self.use(ollama)
        self.assertEqual(await self.run_task(self.app.download_model),
                         "● Downloaded huihui_ai/gemma-4-abliterated:12b. Test it to see how it answers.")
        self.assertIn("◌ Downloading huihui_ai/gemma-4-abliterated:12b: 50% of 7.6 GB", shown)
        self.assertFalse(self.app.local_model_busy)
        self.assertIn("Downloaded the local model", self.lines[-1][0])

    async def test_a_test_says_how_long_it_took_and_what_it_said(self):
        ollama, _ = stand_in_ollama(installed=["huihui_ai/gemma-4-abliterated:12b"],
                                    reply="<think>who am I</think> Hello,\n I'm Rocky.")
        self.use(ollama)
        task = await self.run_task(self.app.test_model)
        self.assertTrue(task.startswith("● Answered in "))
        self.assertIn("100% on the GPU (8.0 GB)", task)
        self.assertTrue(task.endswith(":\nHello, I'm Rocky."))

    async def test_a_model_not_downloaded_or_not_starting_says_so(self):
        ollama, _ = stand_in_ollama(installed=["gemma3:latest"])
        self.use(ollama)
        self.assertIn("isn't downloaded yet", await self.run_task(self.app.test_model))
        await self.app.local_model.shut_down("test")
        self.app.local_model.start.side_effect = RuntimeError("no GPU")
        self.assertEqual(await self.run_task(self.app.test_model), "× no GPU")
        self.assertFalse(self.app.local_model_busy)


class SettingsTabTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_tab_saves_the_model_and_how_long_it_stays_up(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.persistence.load_models.return_value = {area: BACKEND_MODEL for area in MODEL_AREAS}
            await pilot.click("#settings-button")
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            await pilot.pause()
            self.assertIsInstance(app.screen, ModelSettingsScreen)
            app.screen.query_one(TabbedContent).active = "settings-local-model"
            await asyncio.wait_for(app.screen.all_built.wait(), 5)
            await pilot.pause(0.6)
            status = str(app.screen.query_one("#local-model-status", Static).render())
            self.assertIn("○ Off · starts when needed, stops after 15 minutes unused", status)
            app.screen.query_one("#local-model-name", Input).value = "not a model!"
            app.screen.query_one("#local-model-name-save", Button).press()
            await pilot.pause()
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            app.persistence.save_local_model_name.assert_not_called()
            app.screen.query_one("#local-model-name", Input).value = " huihui_ai/gemma-4-abliterated:12b "
            app.screen.query_one("#local-model-name-save", Button).press()
            await pilot.pause()
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            app.persistence.save_local_model_name.assert_called_once_with("huihui_ai/gemma-4-abliterated:12b")
            self.assertEqual(app.local_model_name, "huihui_ai/gemma-4-abliterated:12b")
            app.screen.query_one("#local-model-idle", Select).value = 60
            await pilot.pause()
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            app.persistence.save_local_model_idle.assert_called_once_with(60)
            self.assertEqual(app.local_model.idle_minutes, 60)


if __name__ == "__main__":
    unittest.main()
