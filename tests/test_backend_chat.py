"""Chats without Live: tool results routed back, the backend turn loop, and typing in Standby. Mocked APIs."""

import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from backend_chat import (
    MAX_ROUNDS, OUT_OF_ROUNDS, STANDBY_EXCLUDED, TOOL_NO_RESULT, TOOL_REFUSED, BackendChat, BackendRoute,
)
from session_config import BACKEND_INSTRUCTIONS, TEXT_CHAT_INSTRUCTIONS
from tools.registry import ToolRegistry
from tools.route import routed
from ui.chat_widgets import ChatMarkdown
from ui.composer import ChatComposer
import test_keyboard_persistence as keyboard_tests

SCHEMA = {"type": "function", "name": "echo", "parameters": {"type": "object"}, "strict": False}


class EchoTool:
    """Replies like every Cora tool: through whatever connection it is given."""

    def __init__(self, get_connection):
        self.get_connection = get_connection

    async def handle(self, call_id, arguments):
        connection = self.get_connection()
        await connection.response.item.create(item={
            "type": "function_call_output", "call_id": call_id, "output": json.dumps(arguments),
        })
        await connection.response.create()


def registry_with_echo(live_connection=None):
    registry = ToolRegistry(MagicMock(), lambda: False, lambda run: asyncio.ensure_future(run))
    tool = EchoTool(routed(lambda: live_connection))
    registry.register(SCHEMA, lambda arguments: None, tool.handle)
    registry.register({**SCHEMA, "name": "close_app"}, lambda arguments: None, tool.handle)
    return registry


def item(**fields):
    return SimpleNamespace(**fields, model_dump=lambda exclude_none=True: dict(fields))


def message(text):
    return item(type="message", role="assistant", content=[SimpleNamespace(type="output_text", text=text)])


def call(name="echo", arguments='{"said": "hi"}', call_id="call_1"):
    return item(type="function_call", name=name, arguments=arguments, call_id=call_id)


class FakeClient:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.requests = []
        self.responses = SimpleNamespace(create=self.create)

    async def create(self, stream=False, **request):
        self.requests.append(json.loads(json.dumps(request, default=str)))
        output = self.outputs.pop(0)
        return FakeStream(output) if stream else SimpleNamespace(output=output)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def text_delta(delta, item_id="msg_1", content_index=0):
    return SimpleNamespace(type="response.output_text.delta", delta=delta, item_id=item_id, content_index=content_index)


class FakeStream:
    """A streamed response: given events, or each message's text as deltas, then completed."""

    def __init__(self, output):
        if output and isinstance(output[0], SimpleNamespace) and output[0].type.startswith("response."):
            self.events = output
        else:
            self.events = [
                text_delta(part.text, item_id=f"msg_{index}")
                for index, item in enumerate(output) if item.type == "message" for part in item.content
            ] + [SimpleNamespace(type="response.completed", response=SimpleNamespace(output=output))]
        self.seen = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return False

    async def __aiter__(self):
        for event in self.events:
            self.seen.append(event)
            yield event


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_routed_call_replies_to_its_route_and_others_to_live(self):
        live = MagicMock()
        live.response.item.create = AsyncMock()
        live.response.create = AsyncMock()
        registry = registry_with_echo(live)
        route = BackendRoute()
        self.assertTrue(registry.handle_function_call(
            {"name": "echo", "call_id": "a", "arguments": '{"x": 1}', "status": "completed"}, route=route,
        ))
        self.assertEqual(await route.wait_for("a", 1), '{"x": 1}')
        live.response.item.create.assert_not_called()
        registry.handle_function_call({"name": "echo", "call_id": "b", "arguments": "{}", "status": "completed"})
        await asyncio.sleep(0.05)
        live.response.item.create.assert_awaited_once()
        live.response.create.assert_awaited_once()

    async def test_a_route_refuses_its_excluded_tools(self):
        registry = registry_with_echo()
        route = BackendRoute(excluded=STANDBY_EXCLUDED)
        self.assertFalse(registry.handle_function_call(
            {"name": "close_app", "call_id": "a", "arguments": "{}", "status": "completed"}, route=route,
        ))
        self.assertEqual([s["name"] for s in registry.schemas(excluding=route.excluded)], ["echo"])

    async def test_a_tool_that_never_answers_times_out(self):
        self.assertEqual(await BackendRoute().wait_for("missing", 0.05), TOOL_NO_RESULT)


class BackendChatTests(unittest.IsolatedAsyncioTestCase):
    async def reply(self, outputs, registry=None, route=None, on_text=None):
        self.client = FakeClient(outputs)
        chat = BackendChat(registry or registry_with_echo(), MagicMock(), client_factory=lambda: self.client,
                           tool_seconds=1)
        return await chat.reply(
            content=[{"type": "input_text", "text": "hello"}],
            history=[{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "earlier"}]}],
            sections={"name": "\n\nThe person's name is \"Nick\"."}, model="gpt-6.1-sol",
            route=route or BackendRoute(excluded=STANDBY_EXCLUDED), on_text=on_text,
        )

    async def test_streamed_text_arrives_part_by_part_and_the_reply_is_the_final_text(self):
        shown = []
        final = SimpleNamespace(output=[message("Hi Nick, all good.")])
        text = await self.reply([[
            text_delta("Hi Nick"), text_delta(", all"), text_delta(" good."),
            SimpleNamespace(type="response.completed", response=final),
        ]], on_text=shown.append)
        self.assertEqual(shown, ["Hi Nick", "Hi Nick, all", "Hi Nick, all good."])
        self.assertEqual(text, "Hi Nick, all good.")
        self.assertEqual(self.client.requests[0]["model"], "gpt-6.1-sol")

    async def test_separate_messages_stream_as_one_text_like_the_final_reply(self):
        shown = []
        text = await self.reply([[message("First."), message("Second.")]], on_text=shown.append)
        self.assertEqual(shown[-1], "First.\n\nSecond.")
        self.assertEqual(text, "First.\n\nSecond.")

    async def test_a_streamed_tool_round_continues_into_the_streamed_answer(self):
        shown = []
        text = await self.reply([[call()], [message("You said hi.")]], on_text=shown.append)
        self.assertEqual(text, "You said hi.")
        self.assertEqual(shown, ["You said hi."])
        self.assertEqual(self.client.requests[1]["input"][-1]["call_id"], "call_1")

    async def test_a_failed_stream_raises_after_showing_what_arrived(self):
        shown = []
        with self.assertRaises(RuntimeError):
            await self.reply([[text_delta("Partly"), SimpleNamespace(type="response.failed")]], on_text=shown.append)
        self.assertEqual(shown, ["Partly"])
        with self.assertRaises(RuntimeError):
            await self.reply([[text_delta("Cut off")]], on_text=shown.append)

    async def test_a_plain_answer_is_one_request_without_storage(self):
        text = await self.reply([[message("Hi Nick!")]])
        self.assertEqual(text, "Hi Nick!")
        request = self.client.requests[0]
        self.assertFalse(request["store"])
        self.assertEqual(request["model"], "gpt-6.1-sol")
        self.assertTrue(request["instructions"].startswith(BACKEND_INSTRUCTIONS))
        self.assertIn('"Nick"', request["instructions"])
        self.assertTrue(request["instructions"].endswith(TEXT_CHAT_INSTRUCTIONS))
        self.assertEqual([t.get("name", t["type"]) for t in request["tools"]], ["web_search", "echo"])
        self.assertEqual(request["input"][0]["content"][0]["text"], "earlier")
        self.assertEqual(request["input"][-1], {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}],
        })

    async def test_tool_results_go_into_the_next_request(self):
        text = await self.reply([[call()], [message("You said hi.")]])
        self.assertEqual(text, "You said hi.")
        second = self.client.requests[1]["input"]
        self.assertEqual(second[-2]["type"], "function_call")
        self.assertEqual(second[-1], {"type": "function_call_output", "call_id": "call_1", "output": '{"said": "hi"}'})

    async def test_a_refused_tool_gets_an_error_result_rather_than_a_wait(self):
        await self.reply([[call(name="close_app")], [message("I can't do that here.")]])
        self.assertEqual(self.client.requests[1]["input"][-1]["output"], TOOL_REFUSED)

    async def test_out_of_tool_rounds_she_answers_with_tools_off(self):
        outputs = [[call(call_id=f"call_{n}")] for n in range(MAX_ROUNDS)]
        text = await self.reply([*outputs, [message("Here's what I found so far.")]])
        self.assertEqual(text, "Here's what I found so far.")
        self.assertEqual(len(self.client.requests), MAX_ROUNDS + 1)
        self.assertEqual({request["tool_choice"] for request in self.client.requests[:-1]}, {"auto"})
        final = self.client.requests[-1]
        self.assertEqual(final["tool_choice"], "none")
        self.assertEqual(final["input"][-1], {"type": "message", "role": "developer", "content": OUT_OF_ROUNDS})

    async def test_a_tool_call_even_with_tools_off_gives_up(self):
        outputs = [[call(call_id=f"call_{n}")] for n in range(MAX_ROUNDS + 1)]
        with self.assertRaises(RuntimeError):
            await self.reply(outputs)


class StandbyTypingTests(unittest.IsolatedAsyncioTestCase):
    async def test_typing_in_standby_is_answered_in_writing_without_waking_live(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.standby = True
            app.persistence.save_message.reset_mock()
            app.backend_chat.reply = AsyncMock(return_value="All good here.")
            app.conversation.log_entries.append("Cora: Earlier answer")
            composer = app.query_one("#chat-input", ChatComposer)
            composer.load_text("Are you there?")
            await pilot.press("enter")
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            await pilot.pause()
            app.persistence.save_message.assert_called_once()
            app.live.send_text.assert_not_called()
            kwargs = app.backend_chat.reply.await_args.kwargs
            self.assertEqual(kwargs["content"], [{"type": "input_text", "text": "Are you there?"}])
            # History is taken before the new message joins the chat, so it isn't sent twice.
            self.assertEqual([h["content"][0]["text"] for h in kwargs["history"]], ["Earlier answer"])
            # Maps only with a Google Maps key (none here).
            self.assertEqual(kwargs["route"].excluded, STANDBY_EXCLUDED | {"show_map", "call_someone"})
            self.assertIn("Cora · written:", app.conversation.log_entries)
            self.assertIn("You: Are you there?", app.conversation.log_entries)
            self.assertTrue(app.standby)

    async def test_each_written_reply_reloads_what_she_knows_and_keeps_the_conversation_open(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.standby = True
            app.persistence.load_startup_memory_context.return_value = "Fresh memory."
            app.persistence.load_person_name.return_value = "Nick"
            app.live.memory_context = "Stale memory."
            old_timer = MagicMock()
            app.conversation_end_timer = old_timer
            app.backend_chat.reply = AsyncMock(return_value="Hi Nick.")
            app.query_one("#chat-input", ChatComposer).load_text("Hello?")
            await pilot.press("enter")
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            sections = app.backend_chat.reply.await_args.kwargs["sections"]
            self.assertIn("Fresh memory.", sections["memories"])
            self.assertIn('"Nick"', sections["name"])
            # The long-standby clock restarts, so the conversation ends only after a real quiet.
            old_timer.stop.assert_called_once()
            self.assertIsNotNone(app.conversation_end_timer)
            self.assertIsNot(app.conversation_end_timer, old_timer)
            app.stop_conversation_end_timer()

    async def test_a_written_reply_shows_as_it_streams_then_ends_as_one_message(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.standby = True
            app.conversation.record_message = MagicMock()
            release = asyncio.Event()

            async def reply(**kwargs):
                kwargs["on_text"]("Here's the")
                kwargs["on_text"]("Here's the **plan**")
                await release.wait()
                return "Here's the **plan**."

            app.backend_chat.reply = reply
            app.query_one("#chat-input", ChatComposer).load_text("Plan?")
            await pilot.press("enter")
            await pilot.pause(0.2)
            entries = app.conversation.log_entries
            # Before it finishes: one message, labelled as still being written.
            self.assertIn("Cora · writing…:", entries)
            self.assertNotIn("Cora · written:", entries)
            chat = "\n".join(line.text for line in app.query_one("#chat-output").lines)
            self.assertIn("Here's the plan", chat)
            release.set()
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            await pilot.pause()
            self.assertNotIn("Cora · writing…:", entries)
            self.assertEqual(entries.count("Cora · written:"), 1)
            bodies = [entry for entry in entries if isinstance(entry, ChatMarkdown)]
            self.assertEqual([body.markup for body in bodies], ["Here's the **plan**."])
            # Saved once, as the finished reply.
            app.conversation.record_message.assert_called_once_with("assistant", "Here's the **plan**.", "screen")

    async def test_a_written_reply_that_fails_midway_keeps_what_was_shown(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.standby = True

            async def reply(**kwargs):
                kwargs["on_text"]("Half an ans")
                raise TimeoutError("private")

            app.backend_chat.reply = reply
            app.query_one("#chat-input", ChatComposer).load_text("Hello?")
            with patch.object(app, "notify") as notify:
                await pilot.press("enter")
                await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            await pilot.pause()
            entries = app.conversation.log_entries
            self.assertIn("Cora · written · incomplete:", entries)
            self.assertNotIn("Cora · writing…:", entries)
            self.assertEqual([e.markup for e in entries if isinstance(e, ChatMarkdown)], ["Half an ans"])
            self.assertIn("couldn't reply in writing", notify.call_args.args[0])

    async def test_a_failed_written_reply_says_so(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.standby = True
            app.backend_chat.reply = AsyncMock(side_effect=TimeoutError("private"))
            app.query_one("#chat-input", ChatComposer).load_text("Hello?")
            with patch.object(app, "notify") as notify:
                await pilot.press("enter")
                await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            self.assertIn("couldn't reply in writing", notify.call_args.args[0])
            entries = "\n".join(app.activity.entries)
            self.assertIn("Written reply failed (TimeoutError)", entries)
            self.assertNotIn("private", entries)

    async def test_in_the_app_a_real_tool_replies_to_the_route_not_to_live(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.live.connection = MagicMock()
            route = BackendRoute(excluded=STANDBY_EXCLUDED)
            self.assertTrue(app.tools.handle_function_call({
                "name": "random_number", "call_id": "r1", "status": "completed",
                "arguments": '{"minimum": 4, "maximum": 4}',
            }, route=route))
            self.assertEqual(json.loads(await route.wait_for("r1", 3)), {"status": "ok", "random_number": 4})
            app.live.connection.response.item.create.assert_not_called()
