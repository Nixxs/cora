import io
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image
from textual import events
from textual.document._document import Selection

from app import CoraApp
from ui.composer import IMAGE_MARKER, ChatComposer


class ComposerTests(unittest.IsolatedAsyncioTestCase):
    async def test_ctrl_a_selects_entire_draft_for_deletion(self):
        with patch.object(CoraApp, "connect_to_cora", return_value=None):
            app = CoraApp()
            async with app.run_test(size=(90, 35)) as pilot:
                await pilot.pause()
                composer = app.query_one("#chat-input", ChatComposer)
                draft = "First line\nSecond line\n" + "word " * 100
                for delete_key in ("backspace", "delete"):
                    composer.load_text(draft)
                    composer.move_cursor((1, 3))
                    await pilot.press("ctrl+a")
                    self.assertEqual(composer.selected_text, draft)
                    await pilot.press(delete_key)
                    await pilot.pause()
                    self.assertEqual(composer.text, "")
                    self.assertEqual(composer.size.height, 1)

    async def test_wrap_grow_cap_scroll_and_shrink(self):
        with patch.object(CoraApp, "connect_to_cora", return_value=None):
            app = CoraApp()
            async with app.run_test(size=(90, 35)) as pilot:
                await pilot.pause()
                composer = app.query_one("#chat-input", ChatComposer)
                self.assertEqual(composer.size.height, 1)
                composer.load_text("word " * 20)
                await pilot.pause()
                self.assertEqual(composer.size.height, 2)
                composer.load_text("word " * 150)
                await pilot.pause()
                self.assertEqual(composer.size.height, 4)
                self.assertGreater(composer.virtual_size.height, 4)
                self.assertTrue(composer.show_vertical_scrollbar)
                composer.move_cursor(composer.document.end)
                await pilot.pause()
                self.assertGreater(composer.scroll_offset.y, 0)
                await pilot.resize_terminal(60, 35)
                self.assertEqual(composer.size.height, 4)
                composer.clear()
                await pilot.pause()
                self.assertEqual(composer.size.height, 1)
                self.assertEqual(app.query_one("#input-frame").region.height, 3)

    async def test_newlines_send_and_failure_retains_draft(self):
        with patch.object(CoraApp, "connect_to_cora", return_value=None):
            app = CoraApp()
            async with app.run_test(size=(90, 35)) as pilot:
                await pilot.pause()
                composer = app.query_one("#chat-input", ChatComposer)
                app.persistence = MagicMock()
                app.live.send_text = AsyncMock()
                await pilot.press("h", "i", "shift+enter", "x")
                self.assertEqual(composer.text, "hi\nx")
                app.live.send_text.assert_not_called()
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
                app.live.send_text.assert_awaited_once_with("hi\nx")
                self.assertEqual(composer.text, "")
                self.assertEqual(app.conversation.log_entries, ["You: hi\nx", ""])
                await pilot.press("enter")
                self.assertEqual(app.live.send_text.await_count, 1)
                app.live.send_text = AsyncMock(side_effect=RuntimeError("Offline"))
                composer.load_text("keep\nthis draft")
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertEqual(composer.text, "keep\nthis draft")
                self.assertFalse(composer.disabled)
                self.assertTrue(composer.has_focus)
                self.assertEqual(app.conversation.log_entries, ["You: hi\nx", ""])

    async def test_native_and_windows_clipboard_paste_do_not_send(self):
        with patch.object(CoraApp, "connect_to_cora", return_value=None), \
                patch("ui.composer.sys.platform", "win32"), \
                patch("ui.composer.ImageGrab.grabclipboard", return_value=None), \
                patch("ui.composer.pyperclip.paste", return_value="one\r\ntwo\rthree") as paste:
            app = CoraApp()
            async with app.run_test(size=(90, 35)) as pilot:
                await pilot.pause()
                composer = app.query_one("#chat-input", ChatComposer)
                app.persistence = MagicMock()
                app.live.send_text = AsyncMock()
                await pilot.press("ctrl+v")
                await pilot.pause()
                paste.assert_called_once()
                self.assertEqual(composer.text, "one\ntwo\nthree")
                composer.selection = Selection((0, 0), composer.document.end)
                app.post_message(events.Paste("replacement\n    indented"))
                await pilot.pause()
                self.assertEqual(composer.text, "replacement\n    indented")
                app.live.send_text.assert_not_called()
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
                app.live.send_text.assert_awaited_once_with("replacement\n    indented")

    async def test_pasted_image_is_marked_sent_with_the_message_then_cleared(self):
        screenshot = Image.new("RGB", (3000, 1500), "white")
        with patch.object(CoraApp, "connect_to_cora", return_value=None), \
                patch("ui.composer.ImageGrab.grabclipboard", return_value=screenshot), \
                patch("ui.composer.pyperclip.paste") as paste:
            app = CoraApp()
            async with app.run_test(size=(90, 35)) as pilot:
                await pilot.pause()
                composer = app.query_one("#chat-input", ChatComposer)
                frame = app.query_one("#input-frame")
                app.persistence = MagicMock()
                app.live.send_text = AsyncMock()
                await pilot.press("ctrl+v")
                paste.assert_not_called()
                self.assertEqual(composer.text, "")
                self.assertEqual(frame.border_subtitle, IMAGE_MARKER)
                # Downscaled to fit the backend's 2048 limit before it is kept.
                self.assertEqual(Image.open(io.BytesIO(composer.image_png)).size, (2048, 1024))
                await pilot.press("enter")
                await pilot.pause()
                app.live.send_text.assert_not_called()
                await pilot.press("w", "h", "y", "enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
                app.live.send_text.assert_awaited_once()
                self.assertEqual(app.live.send_text.await_args.args, ("why",))
                self.assertTrue(app.live.send_text.await_args.kwargs["image_png"].startswith(b"\x89PNG"))
                self.assertIsNone(composer.image_png)
                self.assertEqual(frame.border_subtitle, "")

    async def test_escape_removes_an_attached_image(self):
        with patch.object(CoraApp, "connect_to_cora", return_value=None), \
                patch("ui.composer.ImageGrab.grabclipboard", return_value=Image.new("RGB", (4, 4))):
            app = CoraApp()
            async with app.run_test(size=(90, 35)) as pilot:
                await pilot.pause()
                composer = app.query_one("#chat-input", ChatComposer)
                await pilot.press("ctrl+v")
                self.assertIsNotNone(composer.image_png)
                await pilot.press("escape")
                self.assertIsNone(composer.image_png)
                self.assertEqual(app.query_one("#input-frame").border_subtitle, "")


if __name__ == "__main__":
    unittest.main()