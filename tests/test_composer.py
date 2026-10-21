import io
import threading
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image
from textual import events
from textual.document._document import Selection

from app import CoraApp
from tools.look_at_screen import PASTE_INSTRUCTIONS, PASTE_QUESTION
from ui.chat_widgets import ChatImage
from ui.composer import IMAGE_MARKER, SENDING_IMAGE, ChatComposer


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

    async def test_pasted_image_is_marked_sent_alone_or_with_text_then_cleared(self):
        screenshot = Image.new("RGB", (3000, 1500), "white")
        with patch.object(CoraApp, "connect_to_cora", return_value=None), \
                patch("ui.composer.ImageGrab.grabclipboard", return_value=screenshot), \
                patch("ui.chat_submission.look", return_value="A white page.") as look, \
                patch("ui.composer.pyperclip.paste") as paste:
            app = CoraApp()
            async with app.run_test(size=(90, 35)) as pilot:
                await pilot.pause()
                composer = app.query_one("#chat-input", ChatComposer)
                frame = app.query_one("#input-frame")
                persistence = app.submission.persistence
                persistence.call_database = AsyncMock()
                app.live.send_text = AsyncMock()
                await pilot.press("ctrl+v")
                paste.assert_not_called()
                self.assertEqual(composer.text, "")
                self.assertEqual(frame.border_subtitle, IMAGE_MARKER)
                self.assertEqual(frame.styles.border_subtitle_align, "left")
                # Downscaled to fit the backend's 2048 limit before it is kept.
                self.assertEqual(Image.open(io.BytesIO(composer.image_png)).size, (2048, 1024))
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertEqual(app.live.send_text.await_args.args, ("",))
                # A separate look reads the PNG; only its text goes to Live.
                self.assertEqual(look.call_args.args[0], PASTE_QUESTION)
                self.assertTrue(look.call_args.args[1].startswith(b"\x89PNG"))
                self.assertEqual(look.call_args.kwargs, {"mime": "image/png", "instructions": PASTE_INSTRUCTIONS})
                self.assertEqual(app.live.send_text.await_args.kwargs, {"image_seen": "A white page."})
                # Only the marker is saved, never the image bytes.
                self.assertEqual(persistence.call_database.await_args_list[0].args[2], "[image]")
                self.assertIsNone(composer.image_png)
                self.assertEqual(frame.border_subtitle, "")
                persistence.call_database.reset_mock()
                await pilot.press("ctrl+v", "w", "h", "y", "enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertEqual(app.live.send_text.await_args.args, ("why",))
                self.assertEqual(look.call_args.args[0], "why")
                self.assertEqual(persistence.call_database.await_args_list[0].args[2], "[image] why")
                entries = app.conversation.log_entries
                self.assertEqual([entries[i] for i in (0, 2, 3, 5)], ["You: [image]", "", "You: [image] why", ""])
                # Each sent image shows as a clickable thumbnail under its message.
                self.assertEqual([type(entries[i]) for i in (1, 4)], [ChatImage, ChatImage])
                self.assertEqual([entries[i].caption for i in (1, 4)], ["Pasted image", "why"])
                self.assertEqual(entries[1].image.size, (1024, 512))
                self.assertEqual(len(entries), 6)
                self.assertIsNone(composer.image_png)
                self.assertEqual(frame.border_subtitle, "")

    async def test_box_stays_usable_while_a_pasted_image_is_read_but_next_send_waits(self):
        release = threading.Event()

        def slow_look(*args, **kwargs):
            release.wait(5)
            return "A page."

        with patch.object(CoraApp, "connect_to_cora", return_value=None), \
                patch("ui.composer.ImageGrab.grabclipboard", return_value=Image.new("RGB", (4, 4))), \
                patch("ui.chat_submission.look", side_effect=slow_look):
            app = CoraApp()
            async with app.run_test(size=(90, 35)) as pilot:
                try:
                    await pilot.pause()
                    composer = app.query_one("#chat-input", ChatComposer)
                    frame = app.query_one("#input-frame")
                    app.submission.persistence.call_database = AsyncMock()
                    app.live.send_text = AsyncMock()
                    await pilot.press("ctrl+v", "w", "h", "y", "enter")
                    await pilot.pause()
                    self.assertFalse(composer.disabled)
                    self.assertEqual(composer.text, "")
                    self.assertIsNone(composer.image_png)
                    self.assertEqual(frame.border_subtitle, SENDING_IMAGE)
                    with patch.object(app, "notify") as notify:
                        await pilot.press("n", "e", "x", "t", "enter")
                        await pilot.pause()
                        notify.assert_called_once()
                    self.assertEqual(composer.text, "next")
                    app.live.send_text.assert_not_awaited()
                finally:
                    release.set()
                await app.workers.wait_for_complete()
                await pilot.pause()
                app.live.send_text.assert_awaited_once_with("why", image_seen="A page.")
                self.assertEqual(composer.text, "next")
                self.assertEqual(frame.border_subtitle, "")
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
                app.live.send_text.assert_awaited_with("next")
                self.assertEqual(composer.text, "")

    async def test_failed_image_look_saves_and_sends_nothing_and_keeps_the_draft(self):
        with patch.object(CoraApp, "connect_to_cora", return_value=None), \
                patch("ui.composer.ImageGrab.grabclipboard", return_value=Image.new("RGB", (4, 4))), \
                patch("ui.chat_submission.look", side_effect=RuntimeError("private request")):
            app = CoraApp()
            async with app.run_test(size=(90, 35)) as pilot:
                await pilot.pause()
                composer = app.query_one("#chat-input", ChatComposer)
                persistence = app.submission.persistence
                persistence.call_database = AsyncMock()
                app.live.send_text = AsyncMock()
                with patch.object(app, "notify") as notify:
                    await pilot.press("ctrl+v", "w", "h", "y", "enter")
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                    notify.assert_called_once()
                persistence.call_database.assert_not_awaited()
                app.live.send_text.assert_not_awaited()
                self.assertEqual(composer.text, "why")
                self.assertIsNotNone(composer.image_png)
                self.assertEqual(app.query_one("#input-frame").border_subtitle, IMAGE_MARKER)
                activity = "\n".join(app.activity.entries)
                self.assertIn("Pasted image look failed (RuntimeError).", activity)
                self.assertNotIn("private request", activity)

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