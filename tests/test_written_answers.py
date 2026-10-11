import unittest
from unittest.mock import patch

from openai.types.live import ResponseEvent
from openai.types.responses import ResponseOutputItemDoneEvent
from rich.markdown import Markdown
from textual.widgets import RichLog

from app import CoraApp


CODE = "```python\ndef even_numbers(nums):\n    return [n for n in nums if n % 2 == 0]\n```"


def output_event(item_id, text, *, phase="final_answer", status="completed", delegation="voice"):
    # Validate the granular payload against the installed SDK, not an invented
    # response.completed snapshot containing output (Live clears that field).
    backend = ResponseOutputItemDoneEvent.model_validate({
        "type": "response.output_item.done",
        "output_index": 0,
        "sequence_number": 1,
        "item": {
            "id": item_id,
            "type": "message",
            "role": "assistant",
            "status": status,
            "phase": phase,
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        },
    })
    return ResponseEvent.model_validate({
        "type": "response.event",
        "event_id": f"event_{item_id}",
        "delegation_id": delegation,
        "event": backend.model_dump(),
    })


class WrittenAnswerTests(unittest.IsolatedAsyncioTestCase):
    async def test_code_arrives_in_item_not_completion_snapshot(self):
        with patch.object(CoraApp, "connect_to_cora", return_value=None):
            app = CoraApp()
            async with app.run_test(size=(100, 35)) as pilot:
                await pilot.pause()
                app.append_transcript("Cora", "Here is the example.")
                active_index = app.active_transcript_index
                timer = app.transcript_idle_timer
                answer = output_event("msg_code", CODE)
                app.handle_live_event(answer)
                app.handle_live_event(answer)
                app.handle_live_event(ResponseEvent.model_validate({
                    "type": "response.event", "event_id": "event_complete",
                    "delegation_id": "voice",
                    "event": {
                        "type": "response.completed", "sequence_number": 2,
                        "response": {"id": "resp_code", "output": [], "tools": [], "instructions": None},
                    },
                }))
                self.assertEqual(app.active_transcript_index, active_index)
                self.assertIs(app.transcript_idle_timer, timer)
                answers = [entry for entry in app.log_entries if isinstance(entry, Markdown)]
                self.assertEqual(len(answers), 1)
                self.assertEqual(answers[0].markup, CODE)
                app.append_transcript("Cora", " It returns the even numbers.")
                app.flush_transcript()
                await pilot.pause()
                log = app.query_one("#chat-output", RichLog)
                rendered = "\n".join(strip.text for strip in log.lines)
                self.assertIn("def even_numbers(nums):", rendered)
                self.assertIn("    return [n for n in nums", rendered)
                self.assertNotIn("```", rendered)
                self.assertEqual(rendered.count("Cora · on screen:"), 1)
                styles = {str(segment.style) for strip in log.lines
                          if "def even_numbers" in strip.text
                          for segment in strip if segment.text.strip()}
                self.assertGreater(len(styles), 1)
                self.assertTrue(any("Written answer displayed" in entry for entry in app.activity_entries))
                app.append_transcript("You", "Thanks.")
                await pilot.pause()
                bottom = next(i for i, line in enumerate(log.lines) if line.text.startswith("╰"))
                next_message = next(i for i, line in enumerate(log.lines) if "You: Thanks." in line.text)
                # One intentional chat separator, not two after transcript flush.
                self.assertEqual(next_message - bottom, 2)
                self.assertEqual(log.lines[bottom + 1].text.strip(), "")

    async def test_typed_and_voice_items_are_independent(self):
        with patch.object(CoraApp, "connect_to_cora", return_value=None):
            app = CoraApp()
            async with app.run_test(size=(100, 35)):
                app.handle_live_event(output_event("commentary", "Not final", phase="commentary"))
                app.handle_live_event(output_event("incomplete", "Partial code", status="incomplete"))
                app.handle_live_event(output_event("empty", ""))
                app.handle_live_event(output_event("typed", CODE, phase=None, delegation=None))
                app.handle_live_event(output_event("voice", "Another answer", delegation="voice_2"))
                self.assertEqual(app.written_item_ids, {"typed", "voice"})
                self.assertEqual(sum(isinstance(entry, Markdown) for entry in app.log_entries), 2)
                self.assertTrue(any("no written text" in entry for entry in app.activity_entries))


if __name__ == "__main__":
    unittest.main()