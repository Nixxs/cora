import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image

from tools import create_tool_registry
from tools.look_at_screen import (
    ANSWER_MAX_CHARS, DISABLED_RESULT, ERROR_RESULT, SCHEMA, LookAtScreenTool, look, validate_arguments,
)


class LookAtScreenToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.connection = MagicMock()
        self.connection.response.item.create = AsyncMock()
        self.connection.response.create = AsyncMock()
        self.current = self.connection
        self.closing = False
        self.enabled = True
        self.log = MagicMock()
        self.tool = LookAtScreenTool(lambda: self.current, lambda: self.closing, lambda: self.enabled, self.log)

    def items(self):
        return [entry.kwargs["item"] for entry in self.connection.response.item.create.await_args_list]

    def test_registered_with_a_strict_question_schema(self):
        registry = create_tool_registry(
            get_connection=lambda: None, is_closing=lambda: False, shutdown=AsyncMock(),
            request_standby=MagicMock(), write_debug=MagicMock(), schedule=MagicMock(),
        )
        self.assertIn(SCHEMA, registry.schemas())
        validate_arguments({"question": "What error is shown?"})
        for arguments in ({}, {"question": " "}, {"question": "x" * 501}, {"question": 3},
                          {"question": "q", "monitor": 2}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                validate_arguments(arguments)

    def test_look_sends_a_downscaled_jpeg_in_a_separate_unstored_request(self):
        client = MagicMock()
        client.__enter__.return_value = client
        client.responses.create.return_value = MagicMock(status="completed", output_text=" A dialog. " + "x" * 2000)
        with patch("tools.look_at_screen.OpenAI", return_value=client) as openai, \
                patch("tools.look_at_screen.ImageGrab.grab", return_value=Image.new("RGB", (5120, 1440))):
            from tools.look_at_screen import capture_jpeg
            answer = look("What is open?", capture_jpeg())
        openai.assert_called_once_with(max_retries=0)
        request = client.responses.create.call_args.kwargs
        self.assertFalse(request["store"])
        question, image = request["input"][0]["content"]
        self.assertEqual(question, {"type": "input_text", "text": "What is open?"})
        self.assertTrue(image["image_url"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(len(answer), ANSWER_MAX_CHARS)
        self.assertTrue(answer.startswith("A dialog."))

    async def test_only_the_text_answer_enters_the_session_then_continues_once(self):
        with patch("tools.look_at_screen.capture_jpeg", return_value=b"jpeg"), \
                patch("tools.look_at_screen.look", return_value="A terminal with a KeyError.") as seen:
            await self.tool.handle("call_screen", {"question": "  What error?  "})
        seen.assert_called_once_with("What error?", b"jpeg")
        (output,) = self.items()
        self.assertEqual((output["type"], output["call_id"]), ("function_call_output", "call_screen"))
        result = json.loads(output["output"])
        self.assertEqual((result["status"], result["seen"]), ("ok", "A terminal with a KeyError."))
        self.assertIn("untrusted", result["note"])
        self.connection.response.create.assert_awaited_once_with()
        self.log.assert_called_once_with("Screen captured for Cora: call_id=call_screen", "Screen captured")

    async def test_turned_off_in_settings_captures_nothing_and_says_so(self):
        self.enabled = False
        with patch("tools.look_at_screen.capture_jpeg") as capture, patch("tools.look_at_screen.look") as seen:
            await self.tool.handle("call_screen", {"question": "q"})
        capture.assert_not_called()
        seen.assert_not_called()
        self.assertEqual(json.loads(self.items()[0]["output"]), DISABLED_RESULT)
        self.connection.response.create.assert_awaited_once_with()
        self.log.assert_called_once_with(
            "Screen look refused (off in Settings): call_id=call_screen", "Screen looking off",
        )

    async def test_capture_or_look_failure_reports_an_error_and_logs_only_the_type(self):
        for boundary in ("capture", "look"):
            with self.subTest(boundary=boundary):
                self.connection.response.item.create.reset_mock()
                self.connection.response.create.reset_mock()
                self.log.reset_mock()
                failure = RuntimeError("PRIVATE screen text")
                with patch("tools.look_at_screen.capture_jpeg",
                           side_effect=failure if boundary == "capture" else None, return_value=b"jpeg"), \
                        patch("tools.look_at_screen.look", side_effect=failure):
                    await self.tool.handle("call_screen", {"question": "q"})
                self.assertEqual(json.loads(self.items()[0]["output"]), ERROR_RESULT)
                self.connection.response.create.assert_awaited_once_with()
                self.log.assert_any_call("Screen look failed (RuntimeError)", "Tool error")
                self.assertNotIn("PRIVATE", str(self.log.call_args_list))

    async def test_closing_or_a_new_connection_sends_nothing(self):
        for change in ("closing", "connection"):
            with self.subTest(change=change):
                self.closing, self.current = False, self.connection

                def changed(*args):
                    if change == "closing":
                        self.closing = True
                    else:
                        self.current = object()
                    return "seen"

                with patch("tools.look_at_screen.capture_jpeg", return_value=b"jpeg"), \
                        patch("tools.look_at_screen.look", side_effect=changed):
                    await self.tool.handle("call_screen", {"question": "q"})
        self.connection.response.item.create.assert_not_called()
        self.connection.response.create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
