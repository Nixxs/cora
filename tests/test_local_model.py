"""The local model's container: started when something needs it, stopped once idle or when she quits."""

import asyncio
import subprocess
import unittest
from unittest.mock import AsyncMock, patch

from local_model.manager import OFF, READY, LocalModel, LocalModelError
from local_model.ollama import Loaded
from messaging.container import compose


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


if __name__ == "__main__":
    unittest.main()
