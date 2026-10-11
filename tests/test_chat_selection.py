import unittest
from unittest.mock import patch

from textual.geometry import Offset
from textual.selection import Selection

from app import CoraApp
from chat_widgets import ChatMarkdown, SelectableRichLog


class ChatSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_blue_code_and_drag_to_copy(self):
        with patch.object(CoraApp, "connect_to_cora", return_value=None), \
                patch("app.sys.platform", "win32"), patch("app.pyperclip.copy") as copy:
            app = CoraApp()
            async with app.run_test(size=(100, 35)) as pilot:
                await pilot.pause()
                code = 'def joke():\n    print("hello")'
                app.log_entries = [ChatMarkdown(f"```python\n{code}\n```")]
                app.redraw_log()
                await pilot.pause()
                log = app.query_one("#chat-output", SelectableRichLog)
                start_row = next(i for i, line in enumerate(log.lines) if "def joke" in line.text)
                end_row = start_row + 1
                backgrounds = {segment.style.bgcolor.get_truecolor().hex
                               for segment in log.lines[start_row]
                               if segment.style and segment.style.bgcolor
                               and not segment.style.meta.get("code_border")}
                self.assertEqual(backgrounds, {"#202838"})
                for row in (start_row - 1, start_row, end_row + 1):
                    borders = [segment for segment in log.render_line(row)
                               if segment.style and segment.style.meta.get("code_border")]
                    self.assertTrue(borders)
                    self.assertTrue(all(segment.style.bgcolor.get_truecolor().hex == "#141820"
                                        for segment in borders))
                self.assertTrue(log.lines[start_row - 1].text.startswith("╭"))
                self.assertTrue(log.lines[end_row + 1].text.startswith("╰"))
                self.assertEqual(len(log.lines), 4)  # Borders + two code rows; no blank bottom row.
                await pilot.mouse_down(log, offset=(0, start_row))
                await pilot.hover(log, offset=(2 + len('    print("hello")'), end_row))
                await pilot.mouse_up(log, offset=(2 + len('    print("hello")'), end_row))
                await pilot.pause()
                self.assertEqual(app.screen.get_selected_text(), code)
                highlight = app.screen.get_component_rich_style("screen--selection").bgcolor
                self.assertTrue(any(segment.style and segment.style.bgcolor == highlight
                                    for segment in log.render_line(start_row)))
                await pilot.press("ctrl+c")
                copy.assert_called_once_with(code)
                self.assertEqual(log.get_selection(Selection(None, None))[0], code)
                self.assertEqual(log.get_selection(Selection(
                    Offset(6, end_row), Offset(11, end_row)
                ))[0], "print")

    async def test_selection_coordinates_after_scrolling(self):
        with patch.object(CoraApp, "connect_to_cora", return_value=None):
            app = CoraApp()
            async with app.run_test(size=(90, 25)) as pilot:
                await pilot.pause()
                app.log_entries = [f"Earlier message {i}" for i in range(40)]
                app.log_entries.append(ChatMarkdown("```python\nprint('copied')\n```"))
                app.redraw_log()
                await pilot.pause()
                log = app.query_one("#chat-output", SelectableRichLog)
                row = next(i for i, line in enumerate(log.lines) if "print('copied')" in line.text)
                self.assertGreater(log.scroll_offset.y, 0)
                visible_row = row - log.scroll_offset.y
                await pilot.mouse_down(log, offset=(0, visible_row))
                await pilot.hover(log, offset=(2 + len("print('copied')"), visible_row))
                await pilot.mouse_up(log, offset=(2 + len("print('copied')"), visible_row))
                await pilot.pause()
                self.assertEqual(app.screen.get_selected_text(), "print('copied')")
                self.assertEqual(log.get_selection(Selection(Offset(0, 999), Offset(1, 999)))[0], "")


if __name__ == "__main__":
    unittest.main()