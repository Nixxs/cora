"""Rocky, her consultant: his thread, her consult_rocky tool, how his answer reaches the person, and
the switch in Settings. The local model is a stand-in here."""

import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock

from textual.widgets import Switch, TabbedContent

from app_areas.local_model import REACHES_THEM, ROCKY_NOT_HERE, ROCKY_OFF, LocalModelMixin
from app_areas.web_interface import WebInterfaceMixin
from backend_chat import BackendRoute
from local_model.manager import LocalModel
from local_model.rocky import PERSONA, THREAD_EXCHANGES, THREAD_IDLE_SECONDS, RockyThread, answer_text
from session_config import BACKEND_MODEL, MODEL_AREAS
from tools.consult_rocky import NAME, ConsultRockyTool, validate_arguments
from tools.route import current_route
from ui.conversation import ROCKY_ASKED, RockyAnswer
from ui.model_settings import ModelSettingsScreen
from web import routes
from web.feed import ROCKY, MessageFeed
import test_keyboard_persistence as keyboard_tests


class ThreadTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.thread = RockyThread(clock=lambda: self.now)

    def test_a_follow_up_carries_the_thread_and_a_new_topic_starts_afresh(self):
        first = self.thread.messages("sertraline and MDMA?", new_topic=False)
        self.assertEqual(first, [{"role": "system", "content": PERSONA},
                                 {"role": "user", "content": "sertraline and MDMA?"}])
        self.thread.add("sertraline and MDMA?", "Serotonin syndrome is the risk.")
        follow_up = self.thread.messages("why does it blunt it?", new_topic=False)
        self.assertEqual([message["content"] for message in follow_up[1:]],
                         ["sertraline and MDMA?", "Serotonin syndrome is the risk.", "why does it blunt it?"])
        self.assertEqual(len(self.thread.messages("a funeral joke", new_topic=True)), 2)

    def test_a_long_pause_starts_afresh_and_only_the_latest_are_kept(self):
        for number in range(THREAD_EXCHANGES + 3):
            self.thread.add(f"q{number}", f"a{number}")
        self.assertEqual(len(self.thread.messages("next", new_topic=False)), 2 + 2 * THREAD_EXCHANGES)
        self.now += THREAD_IDLE_SECONDS + 1
        self.assertEqual(len(self.thread.messages("next", new_topic=False)), 2)

    def test_what_he_said_without_any_thinking(self):
        self.assertEqual(answer_text("<think>hmm, what do I know</think>\n Straight up: don't. "), "Straight up: don't.")


class StandInOllama:
    def __init__(self, reply="Straight up: serotonin syndrome is the risk."):
        self.reply = reply
        self.asked = []

    async def version(self):
        return "0.40.16"

    async def chat(self, model, messages):
        self.asked.append((model, messages))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class ConsultTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ollama = StandInOllama()
        self.app = SimpleNamespace(
            local_model_consult=True, local_model_name="huihui_ai/gemma-4-abliterated:12b",
            local_model=LocalModel(self.ollama, write_activity=lambda *line: None, start=AsyncMock(),
                                   stop=AsyncMock(), poll_seconds=0),
            rocky_thread=RockyThread(), rocky_lock=asyncio.Lock(), activity=MagicMock(),
            conversation=MagicMock(), web_feed=MessageFeed(),
        )
        self.app.can_consult_rocky = lambda: LocalModelMixin.can_consult_rocky(self.app)

    async def consult(self, route=None, question="I'm on sertraline: MDMA at a festival?", new_topic=False):
        token = current_route.set(route)
        try:
            return await LocalModelMixin.consult_rocky(self.app, question, new_topic)
        finally:
            current_route.reset(token)

    async def test_his_answer_is_shown_and_she_is_told_how_it_reaches_them(self):
        result = await self.consult()
        self.assertEqual(result, {"status": "ok", "rocky_says": "Straight up: serotonin syndrome is the risk.",
                                  "how_it_reaches_them": REACHES_THEM["voice"]})
        self.app.conversation.show_rocky.assert_called_once_with(
            "I'm on sertraline: MDMA at a festival?", "Straight up: serotonin syndrome is the risk.")
        model, messages = self.ollama.asked[0]
        self.assertEqual((model, messages[0]["content"], messages[-1]["content"]),
                         ("huihui_ai/gemma-4-abliterated:12b", PERSONA, "I'm on sertraline: MDMA at a festival?"))
        for channel in ("desk", "signal", "web", "email"):
            result = await self.consult(BackendRoute(channel=channel), new_topic=True)
            self.assertEqual(result["how_it_reaches_them"], REACHES_THEM[channel])
        self.assertIn("read his answer out", REACHES_THEM["voice"])
        self.assertIn("don't repeat it", REACHES_THEM["desk"])
        self.assertIn("don't repeat it", REACHES_THEM["web"])
        self.assertIn("can't see it", REACHES_THEM["signal"])

    async def test_the_web_page_shows_his_answer_as_safe_markup_and_her_question_as_text(self):
        self.ollama.reply = "**Straight up:** <script>alert(1)</script> don't."
        await self.consult(BackendRoute(channel="desk"), question="MDMA <b>on</b> sertraline?")
        item = self.app.web_feed.items[-1]
        self.assertEqual((item.kind, item.data["question"]), (ROCKY, "MDMA <b>on</b> sertraline?"))
        event = routes.feed_event(self.app.web_feed, item)
        self.assertTrue(event.startswith(f"id: {self.app.web_feed.epoch}:1\nevent: rocky\n"))
        body = json.loads(event.split("data: ", 1)[1])
        self.assertEqual(body["question"], "MDMA <b>on</b> sertraline?")
        self.assertIn("<strong>Straight up:</strong>", body["html"])
        self.assertNotIn("<script>", body["html"])
        self.assertNotIn("answer", body)

    async def test_a_follow_up_goes_in_the_same_thread(self):
        await self.consult()
        await self.consult(question="Why does sertraline blunt it?")
        self.assertEqual(len(self.ollama.asked[1][1]), 4)

    async def test_not_for_anyone_elses_conversation_nor_while_switched_off(self):
        self.assertEqual(await self.consult(BackendRoute()), {"status": "error", "message": ROCKY_NOT_HERE})
        self.app.local_model_consult = False
        self.assertEqual(await self.consult(), {"status": "error", "message": ROCKY_OFF})
        self.app.local_model_consult, self.app.local_model_name = True, ""
        self.assertEqual(await self.consult(), {"status": "error", "message": ROCKY_OFF})
        self.assertEqual(self.ollama.asked, [])

    async def test_when_he_cant_answer_she_says_so_and_nothing_is_shown(self):
        self.ollama.reply = ConnectionError("no answer")
        result = await self.consult()
        self.assertEqual(result["status"], "error")
        self.assertIn("don't answer as him", result["message"])
        self.app.conversation.show_rocky.assert_not_called()
        self.assertEqual(len(self.app.web_feed.items), 0)
        self.ollama.reply = "<think>…</think>"
        self.assertIn("said nothing", (await self.consult())["message"])
        self.assertEqual(self.app.rocky_thread.exchanges, [])

    def test_offered_only_while_switched_on_with_a_model(self):
        app = SimpleNamespace(maps_key="k", calling_out_problem=lambda: None, claude_code_listen=True,
                              can_run_claude_code=lambda: True, local_model_consult=False,
                              local_model_name="m")
        app.can_consult_rocky = lambda: LocalModelMixin.can_consult_rocky(app)
        self.assertIn(NAME, WebInterfaceMixin.unavailable_tools(app))
        app.local_model_consult = True
        self.assertNotIn(NAME, WebInterfaceMixin.unavailable_tools(app))


class ToolTests(unittest.IsolatedAsyncioTestCase):
    def test_its_arguments(self):
        validate_arguments({"question": "MDMA?", "new_topic": False})
        for bad in ({"question": " ", "new_topic": False}, {"question": "x" * 4001, "new_topic": True},
                    {"question": "x", "new_topic": "yes"}, {"question": "x"}):
            with self.assertRaises(ValueError):
                validate_arguments(bad)

    async def test_the_answer_goes_back_to_her(self):
        connection = SimpleNamespace(response=SimpleNamespace(item=SimpleNamespace(create=AsyncMock()),
                                                              create=AsyncMock()))
        consult = AsyncMock(return_value={"status": "ok", "rocky_says": "Mate."})
        tool = ConsultRockyTool(lambda: connection, lambda: False, consult, MagicMock())
        await tool.handle("c1", {"question": "  a joke?  ", "new_topic": True})
        consult.assert_awaited_once_with("a joke?", True)
        item = connection.response.item.create.await_args.kwargs["item"]
        self.assertEqual((item["call_id"], json.loads(item["output"])["rocky_says"]), ("c1", "Mate."))
        connection.response.create.assert_awaited_once()


class DeskTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_exchange_is_shown_word_for_word_and_kept_for_her_history(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.conversation.show_rocky("A dark funeral joke?", "My grandad's last words: **still holding the ladder?**")
            await pilot.pause()
            entries = app.conversation.log_entries
            self.assertIn(f"{ROCKY_ASKED}A dark funeral joke?", entries)
            self.assertIn("Rocky:", entries)
            [answer] = [entry for entry in entries if isinstance(entry, RockyAnswer)]
            self.assertEqual(answer.markup, "My grandad's last words: **still holding the ladder?**")
            history = app.conversation.get_reconnect_history()
            said = history[-1]["content"][0]["text"]
            self.assertEqual(history[-1]["role"], "assistant")
            self.assertIn("I asked Rocky, my consultant: A dark funeral joke?", said)
            self.assertIn("He said: My grandad's last words", said)

    async def test_the_switch_in_settings(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.persistence.load_models.return_value = {area: BACKEND_MODEL for area in MODEL_AREAS}
            await pilot.click("#settings-button")
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            await pilot.pause()
            self.assertIsInstance(app.screen, ModelSettingsScreen)
            app.screen.query_one(TabbedContent).active = "settings-local-model"
            await asyncio.wait_for(app.screen.all_built.wait(), 5)
            await pilot.pause()
            switch = app.screen.query_one("#local-model-consult", Switch)
            self.assertFalse(switch.value)
            switch.value = True
            await pilot.pause()
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            app.persistence.save_local_model_consult.assert_called_once_with(True)
            self.assertTrue(app.local_model_consult)


if __name__ == "__main__":
    unittest.main()
