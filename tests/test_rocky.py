"""Rocky, her consultant: his thread, her consult_rocky tool, how his answer reaches the person, and
the switch in Settings. The local model is a stand-in here."""

from tool_gates import gates
import asyncio
import base64
from datetime import date
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from PIL import Image
from textual.widgets import Switch, TabbedContent

from app_areas.local_model import (
    NOT_ON_CONSULTS, REACHES_THEM, ROCKY_NOT_HERE, ROCKY_OFF, LocalModelMixin,
)
from app_areas.web_interface import WebInterfaceMixin
from backend_chat import BackendRoute
from local_model.manager import LocalModel
from local_model.ollama import Turn
from local_model.rocky_browsing import (
    BROWSE_SECONDS as ROCKY_BROWSE_SECONDS, MAX_CALLS as ROCKY_MAX_CALLS, MAX_PAGES as ROCKY_MAX_PAGES,
    OUT_OF_ROOM as ROCKY_OUT_OF_ROOM, PAGE_DROPPED as ROCKY_PAGE_DROPPED, TOOLS as BROWSER_TOOLS,
    LOOK_FIRST as ROCKY_LOOK_FIRST, browse_and_answer, browsing_note, doing as rocky_doing, looked_line,
    needs_looking, no_browser_note,
)
from subconscious.interests.browse_exploration import BrowserAccess
from live import typed_content
from local_model.rocky import (
    DIRECT_PERSONA, PERSONA, GroupThread, stop_at, THREAD_EXCHANGES, THREAD_IDLE_SECONDS, RockyThread, RockyUnavailable, answer_text,
    IMAGE_QUESTION, direct_question, rocky_image, shown_so_far,
)
from session_config import BACKEND_MODEL, MODEL_AREAS
from tools.consult_rocky import NAME, ConsultRockyTool, validate_arguments
from tools.route import current_route
from ui.chat_submission import ChatSubmission
from ui.composer import IMAGE_MARKER, VOICE_ON_NOTE, VOICE_ON_TOOLTIP, ChatComposer, ChatModeChip
from ui.chat_widgets import ChatImage
from ui.conversation import ROCKY_ASKED, YOU_TO_ROCKY, Conversation, DirectRockyAnswer, QuietNote, RockyAnswer
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
                                 {"role": "user", "content": "Cora (for the person): sertraline and MDMA?"}])
        self.thread.add("sertraline and MDMA?", "Serotonin syndrome is the risk.")
        follow_up = self.thread.messages("why does it blunt it?", new_topic=False, name="Nick")
        self.assertEqual([message["content"] for message in follow_up[1:]],
                         ["Cora (for Nick): sertraline and MDMA?", "Serotonin syndrome is the risk.",
                          "Cora (for Nick): why does it blunt it?"])
        self.assertIn('questions from Nick, the person she works for', follow_up[0]["content"])
        self.assertEqual(len(self.thread.messages("a funeral joke", new_topic=True)), 2)

    def test_who_is_speaking_is_labelled(self):
        direct = RockyThread(direct=True).messages("how strong is ket?", False, name="Nick")
        self.assertEqual(direct[-1]["content"], "Nick: how strong is ket?")
        self.assertIn('Nick, the person she works for, an adult, is talking to you directly', direct[0]["content"])
        self.assertIn('Their messages start "Nick:"', direct[0]["content"])
        self.assertEqual(RockyThread(direct=True).messages("hi", False)[-1]["content"], "The person: hi")
        # And what he's told about telling facts from guesses, and doubtful premises.
        self.assertIn("Keep apart what you know, what you're guessing and what you don't know", PERSONA)
        self.assertIn("say so and ask, rather than going along with it", DIRECT_PERSONA)

    def test_a_long_pause_starts_afresh_and_only_the_latest_are_kept(self):
        for number in range(THREAD_EXCHANGES + 3):
            self.thread.add(f"q{number}", f"a{number}")
        self.assertEqual(len(self.thread.messages("next", new_topic=False)), 2 + 2 * THREAD_EXCHANGES)
        self.now += THREAD_IDLE_SECONDS + 1
        self.assertEqual(len(self.thread.messages("next", new_topic=False)), 2)

    def test_what_he_said_without_any_thinking(self):
        self.assertEqual(answer_text("<think>hmm, what do I know</think>\n Straight up: don't. "), "Straight up: don't.")
        # As it streams in, thinking still being written isn't shown either.
        self.assertEqual(shown_so_far("<think>hmm, what"), "")
        self.assertEqual(shown_so_far("<think>hmm</think>\nStraight up"), "Straight up")
        self.assertEqual(shown_so_far("Straight up"), "Straight up")

    def test_a_speaker_label_he_copies_is_taken_off(self):
        self.assertEqual(answer_text("Nick: I hear ya loud and clear, mate!", "Nick"), "I hear ya loud and clear, mate!")
        self.assertEqual(answer_text("Rocky: Sure thing."), "Sure thing.")
        self.assertEqual(answer_text("Cora (for Nick): Sure."), "Sure.")
        self.assertEqual(shown_so_far("  nick:  It sure does", "Nick"), "It sure does")
        # Only labels: anything else before a colon stays.
        self.assertEqual(answer_text("Short answer: no."), "Short answer: no.")
        self.assertEqual(answer_text("Nickel: it's a metal.", "Nick"), "Nickel: it's a metal.")
        self.assertIn("don't start yours with any name", RockyThread(direct=True).messages("hi", False, "Nick")[0]["content"])


class StandInOllama:
    def __init__(self, reply="Straight up: serotonin syndrome is the risk."):
        self.reply = reply
        self.asked = []
        self.sees = True

    async def version(self):
        return "0.40.16"

    async def can_see(self, model):
        return self.sees

    async def chat(self, model, messages, on_text=None, stop=None):
        self.asked.append((model, messages))
        if isinstance(self.reply, Exception):
            raise self.reply
        if on_text is not None:
            # A word at a time, as Ollama streams.
            words = self.reply.split(" ")
            for count in range(1, len(words) + 1):
                on_text(" ".join(words[:count]))
            self.streamed = True
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
            rocky_direct_thread=RockyThread(direct=True),
            # The browser isn't connected (BrowsingTests connects one).
            rocky_browser=AsyncMock(return_value=(None, "the browser is switched off")),
        )
        self.app.can_consult_rocky = lambda: LocalModelMixin.can_consult_rocky(self.app)
        self.app.rocky_answer = lambda *args, **kwargs: LocalModelMixin.rocky_answer(self.app, *args, **kwargs)
        self.app.rocky_person_name = lambda: LocalModelMixin.rocky_person_name(self.app)

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
        self.assertEqual((model, messages[-1]["content"]),
                         ("huihui_ai/gemma-4-abliterated:12b",
                          "Cora (for the person): I'm on sertraline: MDMA at a festival?"))
        # No browser for her consults yet, so he's told he can't look anything up, and never to pretend.
        self.assertEqual(messages[0]["content"], PERSONA + no_browser_note(date.today(), NOT_ON_CONSULTS))
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

    async def test_he_is_told_the_persons_name_when_she_knows_it(self):
        self.app.live = SimpleNamespace(person_name="  Nick  ")
        await self.consult()
        messages = self.ollama.asked[-1][1]
        self.assertEqual(messages[-1]["content"], "Cora (for Nick): I'm on sertraline: MDMA at a festival?")
        self.assertIn("questions from Nick, the person she works for", messages[0]["content"])

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
                              local_model_name="m", sensibo_ready=lambda: False, **gates())
        app.can_consult_rocky = lambda: LocalModelMixin.can_consult_rocky(app)
        self.assertIn(NAME, WebInterfaceMixin.unavailable_tools(app))
        app.local_model_consult = True
        self.assertNotIn(NAME, WebInterfaceMixin.unavailable_tools(app))


class DirectTests(unittest.IsolatedAsyncioTestCase):
    """@rocky: the person asking him themselves, never through Cora."""

    def setUp(self):
        ConsultTests.setUp(self)
        self.recorded = MagicMock()
        self.app.conversation = Conversation(MagicMock(), MagicMock(), MagicMock(), record_message=self.recorded)
        self.app.local_model_consult = False  # Her switch isn't needed for this.

    async def ask(self, question="Is it safe to mix MDMA and sertraline?", **where):
        return await LocalModelMixin.ask_rocky_directly(self.app, question, **where)

    def test_what_counts_as_asking_him_directly(self):
        self.assertEqual(direct_question("@rocky how strong is ket?"), "how strong is ket?")
        self.assertEqual(direct_question("  @Rocky: hi\nthere "), "hi\nthere")
        self.assertEqual(direct_question("@ROCKY"), "")
        for other in ("@rockyroad recipe", "Ask @rocky", "rocky?", "@rock hi"):
            self.assertIsNone(direct_question(other))

    async def test_his_answer_is_shown_everywhere_and_never_reaches_her(self):
        shown = ("Straight up: serotonin syndrome is the risk.\n\n"
                 "*From memory: he couldn't look anything up (the browser is switched off).*")
        self.assertEqual(await self.ask(), shown)
        entries = self.app.conversation.log_entries
        self.assertIn(f"{YOU_TO_ROCKY}: Is it safe to mix MDMA and sertraline?", entries)
        self.assertIn("Rocky:", entries)
        [answer] = [entry for entry in entries if isinstance(entry, DirectRockyAnswer)]
        self.assertEqual(answer.markup, shown)
        # His thread keeps what he said, without the line under it.
        self.assertEqual(self.app.rocky_direct_thread.exchanges[0][1], "Straight up: serotonin syndrome is the risk.")
        self.assertEqual(self.app.conversation.get_reconnect_history(), [])
        self.recorded.assert_not_called()
        # His own thread with the person, apart from Cora's.
        model, messages = self.ollama.asked[0]
        # No browser here: he's told why, so he says he can't check rather than make something up.
        self.assertEqual(messages[0]["content"],
                         DIRECT_PERSONA + no_browser_note(date.today(), "the browser is switched off"))
        self.assertIn("Never say you looked something up", messages[0]["content"])
        self.assertEqual((len(self.app.rocky_direct_thread.exchanges), self.app.rocky_thread.exchanges), (1, []))
        item = self.app.web_feed.items[-1]
        self.assertEqual((item.kind, item.data["asker"], item.ref), (ROCKY, "you", None))

    async def test_his_answer_streams_in_and_stays_out_of_her_history_meanwhile(self):
        seen, histories = [], []

        def on_text(so_far):
            seen.append(so_far)
            # Shown at the desk by now, word by word.
            [shown] = [entry for entry in self.app.conversation.log_entries if isinstance(entry, DirectRockyAnswer)]
            self.assertEqual(shown.markup, so_far)
            histories.append(self.app.conversation.get_reconnect_history())

        await self.ask(on_text=on_text)
        self.assertEqual((seen[0], seen[-1]), ("Straight", "Straight up: serotonin syndrome is the risk."))
        self.assertEqual(len(seen), 7)
        self.assertEqual(histories, [[]] * 7)

    async def test_from_the_web_page_its_device_and_mark_go_with_it(self):
        await self.ask(device="iPad", ref="r1", from_web=True)
        self.assertIn(f"{YOU_TO_ROCKY} · web · iPad: Is it safe to mix MDMA and sertraline?",
                      self.app.conversation.log_entries)
        self.assertEqual(self.app.conversation.get_reconnect_history(), [])
        body = json.loads(routes.feed_event(self.app.web_feed, self.app.web_feed.items[-1]).split("data: ", 1)[1])
        self.assertEqual((body["asker"], body["ref"], body["device"]), ("you", "r1", "iPad"))

    async def test_when_he_cant_answer_the_chat_says_why(self):
        self.app.local_model_name = ""
        with self.assertRaises(RockyUnavailable) as raised:
            await self.ask()
        self.assertIn("choose and download a model", str(raised.exception))
        self.app.local_model_name = "m"
        self.ollama.reply = ConnectionError("down")
        with self.assertRaises(RockyUnavailable):
            await self.ask()
        with self.assertRaises(RockyUnavailable):
            await self.ask("")
        notes = [entry for entry in self.app.conversation.log_entries if isinstance(entry, QuietNote)]
        self.assertEqual(len(notes), 3)
        self.assertNotIn("Rocky:", self.app.conversation.log_entries)
        self.assertEqual(len(self.app.web_feed.items), 0)


def searched(query="mdma sertraline"):
    return Turn("", [{"function": {"name": "search", "arguments": {"query": query}}}])


class BrowsingLoopTests(unittest.IsolatedAsyncioTestCase):
    """His browsing on the way to an answer, with a stand-in model and browser."""

    def setUp(self):
        self.turns, self.offered, self.ran, self.status, self.shown = [], [], [], [], []
        self.pages = 0

    async def turn(self, messages, tools, on_text):
        self.offered.append(tools)
        reply = self.turns.pop(0)
        if reply.content:
            on_text(reply.content)
        return reply

    async def run_tool(self, name, arguments):
        self.ran.append((name, json.loads(arguments)))
        self.pages += 1
        return f"Page {self.pages}"

    async def browse(self, messages=None, clock=lambda: 0.0):
        messages = messages if messages is not None else [{"role": "system", "content": "Rocky"}]
        answer = await browse_and_answer(self.turn, messages, self.run_tool, lambda: self.pages >= ROCKY_MAX_PAGES,
                                         self.shown.append, self.status.append, clock=clock)
        return answer, messages

    async def test_he_searches_reads_then_answers(self):
        self.turns = [searched(), Turn("", [{"function": {"name": "open_page",
                                                          "arguments": '{"url": "https://www.drugsand.me/x"}'}}]),
                      Turn("Mate, it's risky. (drugsand.me)", [])]
        answer, messages = await self.browse()
        self.assertEqual(answer, "Mate, it's risky. (drugsand.me)")
        self.assertEqual(self.ran, [("search", {"query": "mdma sertraline"}),
                                    ("open_page", {"url": "https://www.drugsand.me/x"})])
        self.assertEqual(self.status, ['searching for "mdma sertraline"', "reading drugsand.me"])
        self.assertEqual(self.offered, [BROWSER_TOOLS] * 3)
        self.assertEqual([(m["role"], m.get("content")) for m in messages if m["role"] == "tool"],
                         [("tool", "Page 1"), ("tool", "Page 2")])

    async def test_his_limits_then_he_must_answer(self):
        self.turns = [searched(str(n)) for n in range(ROCKY_MAX_CALLS)] + [Turn("What I found.", [])]
        # Pages never run out here: only the call limit stops him.
        messages = [{"role": "system", "content": "Rocky"}]
        answer = await browse_and_answer(self.turn, messages, self.run_tool, lambda: False,
                                         self.shown.append, self.status.append)
        self.assertEqual(answer, "What I found.")
        self.assertIsNone(self.offered[-1])
        self.assertEqual(len(self.ran), ROCKY_MAX_CALLS)
        self.assertEqual(messages[-1], {"role": "user", "content": ROCKY_OUT_OF_ROOM})
        # Only the newest pages stay in full.
        kept = [m["content"] for m in messages if m["role"] == "tool"]
        self.assertEqual(kept[-2:], [f"Page {ROCKY_MAX_CALLS - 1}", f"Page {ROCKY_MAX_CALLS}"])
        self.assertTrue(all(text == ROCKY_PAGE_DROPPED for text in kept[:-2]))

    async def test_out_of_time_or_pages_ends_browsing(self):
        times = iter((0.0, 1.0, ROCKY_BROWSE_SECONDS + 1))
        self.turns = [searched(), Turn("Quick answer.", [])]
        answer, _ = await self.browse(clock=lambda: next(times))
        self.assertEqual((answer, self.offered[-1]), ("Quick answer.", None))

    async def test_news_answered_from_memory_is_set_aside_once_for_a_search(self):
        self.turns = [Turn("Jensen's big news is Blackwell.", []), searched("jensen huang news"),
                      Turn("Here's what's out there.", [])]
        messages = [{"role": "system", "content": "Rocky"}, {"role": "user", "content": "latest on Jensen?"}]
        answer = await browse_and_answer(self.turn, messages, self.run_tool, lambda: False, self.shown.append,
                                         self.status.append, question="What's the latest news on Jensen?")
        self.assertEqual(answer, "Here's what's out there.")
        self.assertEqual(self.status, ["looking it up first", 'searching for "jensen huang news"'])
        # His answer from memory isn't kept; the nudge is.
        self.assertEqual(messages[2], {"role": "user", "content": ROCKY_LOOK_FIRST})

    async def test_no_nudge_for_what_he_knows_nor_twice(self):
        self.turns = [Turn("Water and sleep.", [])]
        self.assertEqual((await self.browse())[0], "Water and sleep.")
        self.turns = [Turn("From memory.", []), Turn("Still from memory.", [])]
        answer = await browse_and_answer(self.turn, [{"role": "system", "content": "Rocky"}], self.run_tool,
                                         lambda: False, self.shown.append, self.status.append,
                                         question="any news today?")
        self.assertEqual(answer, "Still from memory.")
        self.assertTrue(needs_looking("How much is a PS5 right now?"))
        self.assertTrue(needs_looking("can you look it up"))
        self.assertTrue(needs_looking("who is alice huang to jensen anyway?"))
        self.assertTrue(needs_looking("Is it true he's stepping down?"))
        self.assertFalse(needs_looking("a good hangover cure?"))

    def test_the_line_under_his_answer_says_what_he_really_did(self):
        self.assertEqual(looked_line([], [], "the browser is switched off"),
                         "From memory: he couldn't look anything up (the browser is switched off).")
        self.assertEqual(looked_line([], []), "From memory: he didn't look anything up.")
        self.assertEqual(looked_line(["jensen news"], ["https://www.theguardian.com/a", "https://www.theguardian.com/b",
                                                       "https://cnn.com/x"]),
                         'He searched "jensen news" · read theguardian.com, cnn.com.')

    def test_what_he_is_doing_reads_plainly(self):
        self.assertEqual(rocky_doing("search", {"query": "x " * 60}), f'searching for "{("x " * 30).strip()[:59]}…"')
        self.assertEqual(rocky_doing("open_page", {"url": "/relative"}), "opening a page")
        self.assertEqual(rocky_doing("go_back", {}), "going back")


class BrowsingTests(unittest.IsolatedAsyncioTestCase):
    """The app's side: he browses on the direct line only while the browser is free, holding it."""

    def setUp(self):
        ConsultTests.setUp(self)
        self.app.conversation = Conversation(MagicMock(), MagicMock(), MagicMock(), record_message=MagicMock())
        self.app.browser_helper = SimpleNamespace(lock=asyncio.Lock())
        self.calls, self.locked = [], []

        def call(tool, arguments):
            self.calls.append(tool)
            # Checked after: whether her browser jobs would wait meanwhile.
            self.locked.append(self.app.browser_helper.lock.locked())
            if tool == "browser_snapshot":
                return "Page URL: https://www.drugsand.me/x\n- paragraph: Serotonin syndrome is the risk.", False
            return "", False

        self.browser = BrowserAccess(call, refuse=lambda url: None)
        self.app.rocky_browser = AsyncMock(return_value=(self.browser, ""))
        self.app.rocky_browses = lambda *args: LocalModelMixin.rocky_browses(self.app, *args)
        self.ollama.turns = [searched(), Turn("It's risky, mate (drugsand.me).", [])]

        async def turn(model, messages, tools=None, on_text=None, stop=None):
            self.ollama.asked.append((model, [dict(message) for message in messages]))
            reply = self.ollama.turns.pop(0)
            if on_text and reply.content:
                on_text(reply.content)
            return reply

        self.ollama.turn = turn

    async def test_on_the_direct_line_he_browses_then_answers(self):
        statuses = []
        answer = await LocalModelMixin.ask_rocky_directly(self.app, "MDMA on sertraline?", on_status=statuses.append)
        # What he really did goes under his answer.
        self.assertEqual(answer, "It's risky, mate (drugsand.me).\n\n*He searched \"mdma sertraline\".*")
        self.assertEqual(statuses, ['searching for "mdma sertraline"'])
        self.assertEqual(self.calls[0], "browser_navigate")
        self.assertEqual(self.calls[-1], "browser_close")
        self.assertIn("browser_snapshot", self.calls)
        self.assertTrue(all(self.locked))
        self.assertFalse(self.app.browser_helper.lock.locked())
        system = self.ollama.asked[0][1][0]["content"]
        self.assertEqual(system, DIRECT_PERSONA + browsing_note(date.today()))
        self.assertIn(f"Today is {date.today():%A}", system)
        self.assertIn("Always search first", system)
        # His thread keeps the question and answer only, not the pages.
        self.assertEqual(self.app.rocky_direct_thread.exchanges,
                         [("MDMA on sertraline?", "It's risky, mate (drugsand.me).")])
        self.assertEqual(self.app.conversation.get_reconnect_history(), [])

    async def test_her_consults_dont_browse_yet(self):
        await self.consult(BackendRoute(channel="desk"))
        self.assertEqual(self.calls, [])

    async def consult(self, route):
        return await ConsultTests.consult(self, route)

    async def test_without_the_browser_you_see_why_where_his_answer_goes(self):
        self.app.rocky_browser = AsyncMock(return_value=(None, "the browser is switched off"))
        statuses = []
        await LocalModelMixin.ask_rocky_directly(self.app, "Latest on Jensen Huang?", on_status=statuses.append)
        self.assertEqual(statuses, ["can't browse (the browser is switched off)"])
        self.assertEqual(self.calls, [])


class BrowserSharingTests(unittest.IsolatedAsyncioTestCase):
    """Whether the browser is his: free, busy with a job of hers, or her exploring making way."""

    def setUp(self):
        self.statuses = []
        self.app = SimpleNamespace(closing=False, exploration_worker=None, signing_up=lambda: False,
                                   browser_helper=SimpleNamespace(lock=asyncio.Lock()), rocky_wants_browser=False,
                                   exploration_browser=lambda loop, **options: options)
        self.app.exploring = lambda: LocalModelMixin.exploring(self.app)

    async def browser(self):
        return await LocalModelMixin.rocky_browser(self.app, self.statuses.append)

    async def test_his_when_free_and_not_while_she_has_a_job_or_its_off(self):
        browser, why = await self.browser()
        self.assertEqual((browser["title"], why), ("Rocky", ""))
        self.app.signing_up = lambda: True
        self.assertEqual(await self.browser(), (None, "Cora is using the browser for a job of hers"))
        self.app.signing_up = lambda: False
        async with self.app.browser_helper.lock:
            self.assertEqual((await self.browser())[0], None)
        self.app.exploration_browser = lambda loop, **options: None
        self.assertEqual(await self.browser(), (None, "the browser is switched off"))

    async def test_her_exploring_wraps_up_for_him(self):
        worker = SimpleNamespace(is_finished=False)
        self.app.exploration_worker = worker
        wanted = []

        async def she_wraps_up():
            await asyncio.sleep(0.05)
            wanted.append(self.app.rocky_wants_browser)
            worker.is_finished = True

        with patch("app_areas.local_model.BROWSER_WAIT_POLL_SECONDS", 0.01):
            _, (browser, why) = await asyncio.gather(she_wraps_up(), self.browser())
        self.assertEqual((browser["title"], why), ("Rocky", ""))
        self.assertEqual((wanted, self.app.rocky_wants_browser), ([True], False))
        self.assertEqual(self.statuses, ["waiting for the browser"])

    async def test_if_she_takes_too_long_he_goes_without(self):
        self.app.exploration_worker = SimpleNamespace(is_finished=False)
        with patch("app_areas.local_model.BROWSER_WAIT_SECONDS", 0.05), \
                patch("app_areas.local_model.BROWSER_WAIT_POLL_SECONDS", 0.01):
            self.assertEqual(await self.browser(), (None, "Cora is still using the browser"))
        self.assertFalse(self.app.rocky_wants_browser)


class GroupChatTests(unittest.IsolatedAsyncioTestCase):
    """A group chat: the person's message, Rocky's turn (seeing the conversation), then Cora's."""

    def setUp(self):
        self.conversation = Conversation(MagicMock(), MagicMock(), MagicMock(), record_message=MagicMock())

    def test_he_sees_the_conversation_labelled_and_not_whats_private(self):
        c = self.conversation
        c.write_log("You: is ket safe with weed?")
        c.show_written_reply("Mostly, but go slow.", label="Cora", channel="keyboard")
        c.ask_rocky_directly("a private one", "").finish("private answer")
        c.show_note("Cora checked where you are.")
        draft = c.rocky_in_group()
        draft.finish("Go slow, mate.")
        c.write_log("You · web · iPad: and with booze?")
        self.assertEqual(c.group_transcript(), [
            ("person", "is ket safe with weed?"), ("cora", "Mostly, but go slow."),
            ("rocky", "Go slow, mate."), ("person", "and with booze?")])
        messages = GroupThread(c.group_transcript).messages("and with booze?", False, "Nick")
        self.assertIn("in a group chat with Nick and Cora", messages[0]["content"])
        self.assertEqual([(m["role"], m["content"]) for m in messages[1:]], [
            ("user", "Nick: is ket safe with weed?\n\nCora: Mostly, but go slow."),
            ("assistant", "Go slow, mate."), ("user", "Nick: and with booze?")])
        # His group answer is part of her conversation; his private one isn't.
        said = [item["content"][0]["text"] for item in c.get_reconnect_history()]
        self.assertIn("(Rocky, in our group chat, said: Go slow, mate.)", said)
        self.assertNotIn("private answer", " ".join(said))

    def test_he_writes_only_his_own_turn(self):
        said = ("Love the new setup, mate!\n\nCora: Just bear in mind I'm the brains.\n\n"
                "*From memory: he didn't look anything up.*")
        self.assertEqual(answer_text(said, "Nick"), "Love the new setup, mate!")
        self.assertEqual(answer_text("Sure.\n**Cora:** me too", "Nick"), "Sure.")
        self.assertEqual(answer_text("Ha.\nNick: what?", "Nick"), "Ha.")
        self.assertEqual(shown_so_far("Go slow.\n\nCo", "Nick"), "Go slow.\n\nCo")
        self.assertEqual(shown_so_far("Go slow.\n\nCora: and", "Nick"), "Go slow.\n\n")
        # A name mid-sentence is just a name.
        self.assertEqual(answer_text("Ask Cora: she knows.", "Nick"), "Ask Cora: she knows.")
        self.assertIn("\nCora:", stop_at("Nick"))
        self.assertIn("\nNick:", stop_at("Nick"))
        self.assertIn("never write Cora's reply", GroupThread(lambda: []).messages("hi", False)[0]["content"])

    def test_he_isnt_shown_the_line_under_his_answers(self):
        self.conversation.write_log("You: hi")
        self.conversation.rocky_in_group().finish("Hey mate!\n\n*From memory: he didn't look anything up.*")
        self.assertEqual(self.conversation.group_transcript()[-1], ("rocky", "Hey mate!"))

    def test_her_turn_is_given_his_answer(self):
        content = typed_content("and with booze?", group=True, rocky_said="Don't, mate.")
        self.assertEqual(content[0]["text"], "and with booze?")
        self.assertIn("His answer: Don't, mate.", content[1]["text"])
        self.assertIn("Don't repeat what he said", content[1]["text"])
        self.assertIn("he couldn't answer this time", typed_content("x", group=True)[1]["text"])
        self.assertEqual(len(typed_content("x")), 1)

    async def test_his_turn_is_shown_published_and_returned_for_hers(self):
        ConsultTests.setUp(self)
        self.app.conversation = self.conversation
        self.app.rocky_group_thread = GroupThread(self.conversation.group_transcript)
        self.conversation.write_log("You: is ket safe with weed?")
        said = await LocalModelMixin.ask_rocky_in_group(self.app, "is ket safe with weed?")
        self.assertTrue(said.startswith("Straight up: serotonin syndrome is the risk.\n\n*From memory"))
        item = self.app.web_feed.items[-1]
        self.assertEqual((item.kind, item.data["asker"]), (ROCKY, "group"))
        self.assertEqual(self.ollama.asked[0][1][-1]["content"], "The person: is ket safe with weed?")
        # No model: why, in his place, and her turn goes ahead without him.
        self.app.local_model_name = ""
        self.assertIsNone(await LocalModelMixin.ask_rocky_in_group(self.app, "again?"))
        self.assertIn("Cora's answering on her own", self.conversation.log_entries[-3])


class VoiceOnTests(unittest.IsolatedAsyncioTestCase):
    """While her voice is on the chat is hers alone: the chip stays on Cora, dimmed, saying why."""

    async def test_voice_on_puts_the_chat_on_cora_and_standby_lets_it_change_again(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.local_model_name = "huihui_ai/gemma-4-abliterated:12b"
            chip = app.query_one("#chat-mode", ChatModeChip)
            app.standby = True
            app.set_chat_mode("group")
            self.assertEqual(app.chat_mode, "group")
            # Her voice comes on: Cora, dimmed, with why.
            app.standby = False
            app.set_connection_state("active")
            await pilot.pause()
            self.assertEqual((app.chat_mode, str(chip.label)), ("cora", "Cora ›"))
            self.assertTrue(chip.has_class("voice-on"))
            self.assertEqual(chip.tooltip, VOICE_ON_TOOLTIP)
            app.notify = MagicMock()
            await pilot.click("#chat-mode")
            await pilot.pause()
            self.assertEqual(app.chat_mode, "cora")
            app.notify.assert_called_once_with(VOICE_ON_NOTE)
            # Back in standby: still Cora until it's clicked, then the others again.
            app.standby = True
            app.set_connection_state("standby")
            await pilot.pause()
            self.assertFalse(chip.has_class("voice-on"))
            self.assertEqual(app.chat_mode, "cora")
            await pilot.click("#chat-mode")
            await pilot.pause()
            self.assertEqual(app.chat_mode, "rocky")


class GroupSubmissionTests(unittest.IsolatedAsyncioTestCase):
    """At the desk: shown, then his turn, then hers with his answer, live or in Standby."""

    async def submit(self, standby: bool, image=None):
        order = []
        conversation = MagicMock()
        conversation.get_reconnect_history.side_effect = lambda: order.append("history") or ["earlier"]
        conversation.write_log.side_effect = lambda line: order.append(line) if line else None

        async def ask_group(message):
            order.append(f"rocky: {message}")
            return "Don't, mate."

        live = MagicMock()
        live.send_text = AsyncMock(side_effect=lambda *a, **k: order.append(("live", a, k)))
        send_without_live = MagicMock(side_effect=lambda *a, **k: order.append(("written", a, k)))
        persistence = MagicMock()
        persistence.call_database = AsyncMock()
        submission = ChatSubmission(
            persistence=persistence, conversation=conversation, live=live, backend_work=MagicMock(),
            is_closing=lambda: False, is_reconnecting=lambda: False, is_standby=lambda: standby,
            schedule=MagicMock(), record_activity=MagicMock(), write_activity=MagicMock(), notify=MagicMock(),
            trace=MagicMock(), send_without_live=send_without_live, ask_rocky=MagicMock(),
            chat_mode=lambda: "group", ask_group=ask_group,
        )
        composer = MagicMock(image_png=image, disabled=True)
        await submission.submit_keyboard_message("and with booze?", uuid4(), composer)
        return order, conversation

    async def test_in_standby_she_writes_after_him_with_his_answer(self):
        order, conversation = await self.submit(standby=True)
        self.assertEqual(order[:3], ["history", "You: and with booze?", "rocky: and with booze?"])
        self.assertEqual(order[3], ("written", ("and with booze?", None),
                                    {"group": True, "rocky_said": "Don't, mate.", "history": ["earlier"]}))
        conversation.announce.assert_called_once_with("user", "and with booze?", "keyboard")

    async def test_with_her_voice_on_its_hers_alone(self):
        order, _ = await self.submit(standby=False)
        self.assertNotIn("rocky: and with booze?", order)
        self.assertEqual(order, [("live", ("and with booze?",), {}), "You: and with booze?"])


def png(size=(1600, 900), colour="red") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


class ImageTests(unittest.IsolatedAsyncioTestCase):
    """He sees images himself (his model takes them): his own small copy, never leaving this PC."""

    def setUp(self):
        ConsultTests.setUp(self)
        self.app.conversation = Conversation(MagicMock(), MagicMock(), MagicMock(), record_message=MagicMock())
        self.app.rocky_vision = {}
        self.app.rocky_sees = lambda ollama, model: LocalModelMixin.rocky_sees(self.app, ollama, model)
        self.app.set_chat_mode = MagicMock()
        self.app.chat_mode = "rocky"

    def test_his_copy_is_a_small_jpeg(self):
        copy = base64.b64decode(rocky_image(png()))
        with Image.open(io.BytesIO(copy)) as image:
            self.assertEqual((image.format, max(image.size)), ("JPEG", 1024))
        with self.assertRaises(Exception):
            rocky_image(b"not an image")

    def test_his_thread_keeps_each_image_with_its_question(self):
        thread = RockyThread(direct=True)
        first = thread.messages("what's this pill?", False, "Nick", images=["abc"])
        self.assertEqual(first[-1], {"role": "user", "content": "Nick: what's this pill?", "images": ["abc"]})
        thread.add("what's this pill?", "Looks like an E, mate.", ["abc"])
        follow_up = thread.messages("is it strong?", False, "Nick")
        self.assertEqual(follow_up[1]["images"], ["abc"])
        self.assertNotIn("images", follow_up[-1])
        group = GroupThread(lambda: [("person", "[image] this?")]).messages("this?", False, "Nick", images=["xyz"])
        self.assertEqual(group[-1]["images"], ["xyz"])

    async def test_asked_directly_he_sees_the_image_and_the_chat_shows_it(self):
        answer = await LocalModelMixin.ask_rocky_directly(self.app, "", image=png())
        _, messages = self.ollama.asked[0]
        self.assertEqual(messages[-1]["content"], f"The person: {IMAGE_QUESTION}")
        self.assertEqual(len(messages[-1]["images"]), 1)
        entries = self.app.conversation.log_entries
        self.assertIn(f"{YOU_TO_ROCKY}: [image] {IMAGE_QUESTION}", entries)
        self.assertTrue(any(isinstance(entry, ChatImage) for entry in entries))
        self.assertEqual(self.app.web_feed.items[-1].data["question"], f"[image] {IMAGE_QUESTION}")
        self.assertNotIn("couldn't see", answer)
        # Still nothing for her: an image in the chat is never part of her history.
        self.assertEqual(self.app.conversation.get_reconnect_history(), [])

    async def test_a_model_that_cant_see_says_so_rather_than_guessing(self):
        self.ollama.sees = False
        answer = await LocalModelMixin.ask_rocky_directly(self.app, "what's this pill?", image=png())
        _, messages = self.ollama.asked[0]
        self.assertNotIn("images", messages[-1])
        self.assertIn("you can't see images", messages[0]["content"])
        self.assertIn("He couldn't see the image", answer)
        self.assertIs(self.app.rocky_vision["huihui_ai/gemma-4-abliterated:12b"], False)
        self.app.set_chat_mode.assert_called_once_with("rocky")

    async def test_in_a_group_chat_he_sees_it_too(self):
        self.app.rocky_group_thread = GroupThread(self.app.conversation.group_transcript)
        self.app.conversation.write_log("You: [image] what's this pill?")
        await LocalModelMixin.ask_rocky_in_group(self.app, "what's this pill?", image=png())
        self.assertEqual(len(self.ollama.asked[0][1][-1]["images"]), 1)


class DeskSubmissionTests(unittest.TestCase):
    def setUp(self):
        self.asked, self.notify, self.schedule = MagicMock(), MagicMock(), MagicMock()
        self.submission = ChatSubmission(
            persistence=MagicMock(), conversation=MagicMock(), live=MagicMock(), backend_work=MagicMock(),
            is_closing=lambda: False, is_reconnecting=lambda: False, is_standby=lambda: False,
            schedule=self.schedule, record_activity=MagicMock(), write_activity=MagicMock(),
            notify=self.notify, trace=MagicMock(), ask_rocky=self.asked,
        )

    def submit(self, text, image=None):
        composer = MagicMock(image_png=image, disabled=False)
        self.submission.submitted(SimpleNamespace(text=text, composer=composer))
        return composer

    def test_at_rocky_goes_to_him_alone(self):
        composer = self.submit("@Rocky: what's in a speedball?")
        self.asked.assert_called_once_with("what's in a speedball?")
        composer.clear.assert_called_once()
        self.schedule.assert_not_called()
        self.submit("Hi Cora")
        self.schedule.assert_called_once()

    def test_with_the_chat_on_rocky_every_message_goes_to_him(self):
        mode = ["rocky"]
        self.submission.chat_mode = lambda: mode[0]
        self.submit("how strong is ket?")
        self.asked.assert_called_once_with("how strong is ket?")
        self.schedule.assert_not_called()
        mode[0] = "cora"
        self.submit("Hi Cora")
        self.schedule.assert_called_once()

    def test_an_image_goes_to_him_too(self):
        composer = self.submit("@rocky what is this pill?", image=b"png")
        self.asked.assert_called_once_with("what is this pill?", b"png")
        self.schedule.assert_not_called()
        composer.clear.assert_called_once()
        composer.remove_image.assert_called_once()


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

    async def test_the_chip_switches_who_messages_go_to_on_both_chat_boxes(self):
        async with keyboard_tests.running_app() as (app, pilot):
            chip, composer = app.query_one("#chat-mode", ChatModeChip), app.query_one("#chat-input", ChatComposer)
            # In standby (with her voice on, the chat is Cora's alone: VoiceOnTests).
            app.standby = True
            # No model for Rocky: no chip, and messages go to Cora whatever was chosen.
            self.assertFalse(chip.display)
            app.set_chat_mode("rocky")
            self.assertEqual(app.chat_mode, "cora")
            app.local_model_name = "huihui_ai/gemma-4-abliterated:12b"
            app.set_chat_mode("cora")
            await pilot.pause()
            self.assertTrue(chip.display)
            self.assertEqual((app.chat_mode, str(chip.label)), ("cora", "Cora ›"))
            await pilot.click("#chat-mode")
            await pilot.pause()
            self.assertEqual((app.chat_mode, str(chip.label)), ("rocky", "Rocky ›"))
            self.assertTrue(chip.has_class("mode-rocky"))
            self.assertIn("Rocky (private", composer.placeholder)
            self.assertIs(app.focused, composer)
            # The dashboard's chip shows the same, and moves both on.
            await pilot.click("#view-button")
            await pilot.pause()
            dashboard_chip = app.screen.query_one("#dashboard-chat-mode", ChatModeChip)
            self.assertEqual(str(dashboard_chip.label), "Rocky ›")
            self.assertIn("Rocky (private", app.screen.query_one("#dashboard-input", ChatComposer).placeholder)
            await pilot.click("#dashboard-chat-mode")
            await pilot.pause()
            self.assertEqual((app.chat_mode, str(dashboard_chip.label), str(chip.label)), ("group", "Group ›", "Group ›"))
            self.assertIn("Cora and Rocky", app.screen.query_one("#dashboard-input", ChatComposer).placeholder)
            await pilot.click("#dashboard-chat-mode")
            await pilot.pause()
            self.assertEqual((app.chat_mode, str(dashboard_chip.label), str(chip.label)), ("cora", "Cora ›", "Cora ›"))
            # A pasted image still marks the box's border, past the row it shares with the chip.
            composer.attach_image(b"png")
            self.assertEqual(app.query_one("#input-frame").border_subtitle, IMAGE_MARKER)

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
