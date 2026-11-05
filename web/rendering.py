"""Cora's written replies as HTML for the web page: she writes Markdown, and it's rendered here.

As with her emails (mail/formatting.py), raw HTML in what she writes is escaped, never passed
through, and links go only to web and mail addresses, so nothing she quotes from elsewhere can add
markup or scripts to the page. Styling is the page's own stylesheet.
"""

import re

from markdown_it import MarkdownIt

ALLOWED_LINK = re.compile(r"^(https?:|mailto:)", re.IGNORECASE)

_markdown = MarkdownIt("commonmark", {"html": False, "breaks": True, "linkify": False}).enable(
    ["table", "strikethrough"])
# Only web and mail links, never javascript:, data: or anything else a link could carry.
_markdown.validateLink = lambda url: bool(ALLOWED_LINK.match(url.strip()))


def chat_html(text: str) -> str:
    """Her Markdown as safe HTML for one message's body."""
    return _markdown.render(text)
