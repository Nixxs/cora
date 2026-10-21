import io
import json
import socket
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image
from textual.selection import Selection

from app import CoraApp
from tools import create_tool_registry
from tools.show_web_image import (
    ERROR_RESULT, SCHEMA, SHOWN_RESULT, CheckedRedirects, ShowWebImageTool, check_public,
    fetch_preview_image, validate_arguments,
)
from ui.chat_widgets import SelectableRichLog
from ui.image_view import ImageScreen

PAGE = "https://example.com/places/cafe"


def png(width, height):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "red").save(buffer, format="PNG")
    return buffer.getvalue()


def resolving_to(address):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    return patch("tools.show_web_image.socket.getaddrinfo",
                 return_value=[(family, socket.SOCK_STREAM, 6, "", (address, 443))])


class ShowWebImageFetchTests(unittest.TestCase):
    def test_registered_last_with_a_strict_link_and_caption_schema(self):
        registry = create_tool_registry(
            get_connection=lambda: None, is_closing=lambda: False, shutdown=AsyncMock(),
            request_standby=MagicMock(), write_debug=MagicMock(), schedule=MagicMock(),
        )
        self.assertEqual(registry.schemas()[-1], SCHEMA)
        validate_arguments({"page_url": PAGE, "caption": "The cafe"})
        for arguments in ({}, {"page_url": PAGE}, {"page_url": "http://example.com", "caption": "c"},
                          {"page_url": "file:///C:/secret.png", "caption": "c"},
                          {"page_url": PAGE, "caption": " "}, {"page_url": PAGE, "caption": "c" * 201},
                          {"page_url": PAGE, "caption": "c", "width": 3}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                validate_arguments(arguments)

    def test_only_public_https_hosts_are_fetched_including_redirects(self):
        for address in ("127.0.0.1", "10.0.0.5", "192.168.1.2", "169.254.169.254", "::1"):
            with self.subTest(address=address), resolving_to(address), self.assertRaises(ValueError):
                check_public(PAGE)
        with resolving_to("93.184.216.34"):
            check_public(PAGE)
        with self.assertRaises(ValueError):
            check_public("http://example.com/")
        with resolving_to("127.0.0.1"), self.assertRaises(ValueError):
            CheckedRedirects().redirect_request(MagicMock(), None, 302, "Found", {}, "https://localhost/")

    def test_fetches_the_pages_preview_image_and_shrinks_it(self):
        page = b'<html><head><meta property="og:image" content="/img/cafe.png"></head></html>'
        with patch("tools.show_web_image.fetch", side_effect=[
            (PAGE, "text/html; charset=utf-8", page), ("https://example.com/img/cafe.png", "image/png", png(1600, 800)),
        ]) as fetch:
            image = fetch_preview_image(PAGE)
        self.assertEqual(fetch.call_args_list[1].args[0], "https://example.com/img/cafe.png")
        self.assertEqual(image.size, (1024, 512))

    def test_pages_without_a_preview_or_with_a_non_image_preview_fail(self):
        cases = [
            [(PAGE, "text/html", b"<html><head></head></html>")],
            [(PAGE, "image/png", png(4, 4))],
            [(PAGE, "text/html", b'<meta name="twitter:image" content="https://example.com/x">'),
             ("https://example.com/x", "text/html", b"<html></html>")],
        ]
        for responses in cases:
            with self.subTest(responses=responses[0][1]), \
                    patch("tools.show_web_image.fetch", side_effect=responses), self.assertRaises(ValueError):
                fetch_preview_image(PAGE)


class ShowWebImageToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.connection = MagicMock()
        self.connection.response.item.create = AsyncMock()
        self.connection.response.create = AsyncMock()
        self.current = self.connection
        self.closing = False
        self.show = MagicMock()
        self.log = MagicMock()
        self.tool = ShowWebImageTool(lambda: self.current, lambda: self.closing, self.show, self.log)

    def output(self):
        (call,) = self.connection.response.item.create.await_args_list
        return json.loads(call.kwargs["item"]["output"])

    async def test_shows_the_image_and_only_a_confirmation_enters_the_session(self):
        image = Image.new("RGB", (4, 4))
        with patch("tools.show_web_image.fetch_preview_image", return_value=image) as fetch:
            await self.tool.handle("call_img", {"page_url": f" {PAGE} ", "caption": " The cafe "})
        fetch.assert_called_once_with(PAGE)
        self.show.assert_called_once_with(image, "The cafe", PAGE)
        self.assertEqual(self.output(), SHOWN_RESULT)
        self.connection.response.create.assert_awaited_once_with()
        self.log.assert_called_once_with("Web image shown: call_id=call_img", "Web image shown")

    async def test_failure_shows_nothing_and_logs_only_the_type(self):
        with patch("tools.show_web_image.fetch_preview_image", side_effect=OSError(f"PRIVATE {PAGE}")):
            await self.tool.handle("call_img", {"page_url": PAGE, "caption": "c"})
        self.show.assert_not_called()
        self.assertEqual(self.output(), ERROR_RESULT)
        self.connection.response.create.assert_awaited_once_with()
        self.log.assert_called_once_with("Web image failed (OSError)", "Tool error")

    async def test_closing_or_a_new_connection_shows_and_sends_nothing(self):
        for change in ("closing", "connection"):
            with self.subTest(change=change):
                self.closing, self.current = False, self.connection

                def changed(url):
                    if change == "closing":
                        self.closing = True
                    else:
                        self.current = object()
                    return Image.new("RGB", (4, 4))

                with patch("tools.show_web_image.fetch_preview_image", side_effect=changed):
                    await self.tool.handle("call_img", {"page_url": PAGE, "caption": "c"})
        self.show.assert_not_called()
        self.connection.response.item.create.assert_not_called()


class ChatImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_appears_in_the_chat_with_caption_and_source_and_is_not_copied(self):
        with patch.object(CoraApp, "connect_to_cora", return_value=None):
            app = CoraApp()
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.pause()
                app.conversation.record_message = MagicMock()
                app.conversation.show_image(Image.new("RGB", (400, 200), "red"), "The cafe", PAGE)
                await pilot.pause()
                log = app.query_one("#chat-output", SelectableRichLog)
                lines = [line.text for line in log.lines]
                self.assertIn("Cora · image: The cafe", lines)
                self.assertIn("From example.com", lines)
                image_rows = [text for text in lines if "▀" in text]
                self.assertTrue(image_rows)
                self.assertTrue(all(len(text.rstrip()) <= 24 for text in image_rows))
                copied, _ = log.get_selection(Selection(None, None))
                self.assertNotIn("▀", copied)
                self.assertIn("Cora · image: The cafe", copied)
                app.conversation.record_message.assert_called_once_with(
                    "assistant", f"[image] The cafe ({PAGE})", "screen",
                )
                # Clicking the site name opens the page.
                row = lines.index("From example.com")
                content = log.content_region
                with patch.object(app, "open_url") as open_url:
                    await pilot.click(log, offset=(content.x - log.region.x + 7, content.y - log.region.y + row))
                    await pilot.pause()
                open_url.assert_called_once_with(PAGE)
                with patch.object(app, "open_url") as open_url:
                    await pilot.click(log, offset=(content.x - log.region.x + 1, content.y - log.region.y + row))
                    await pilot.pause()
                open_url.assert_not_called()

                # Clicking the picture opens it sharply in a pop-up; Esc or a click closes it.
                picture_row = next(i for i, text in enumerate(lines) if "▀" in text)
                for close in ("escape", "click"):
                    with self.subTest(close=close):
                        await pilot.click(log, offset=(
                            content.x - log.region.x + 2, content.y - log.region.y + picture_row,
                        ))
                        await pilot.pause()
                        self.assertIsInstance(app.screen, ImageScreen)
                        self.assertEqual(app.screen.query_one("#image-view-image").image.size, (400, 200))
                        self.assertEqual(str(app.screen.query_one("#image-view-caption").render()), "The cafe")

                        # Changes in the chat behind must not repaint the picture (Sixel blinks).
                        picture = app.screen.query_one("#image-view-image")
                        with patch.object(picture, "render_lines", wraps=picture.render_lines) as render:
                            log.write("Cora · still talking")
                            log.refresh()
                            await pilot.pause()
                            await pilot.pause()
                            render.assert_not_called()
                            picture.refresh()
                            await pilot.pause()
                            render.assert_called()

                        if close == "escape":
                            await pilot.press("escape")
                        else:
                            await pilot.click("#image-view-hint")
                        await pilot.pause()
                        self.assertNotIsInstance(app.screen, ImageScreen)


if __name__ == "__main__":
    unittest.main()
