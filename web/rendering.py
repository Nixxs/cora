"""Cora's written replies as HTML for the web page: she writes Markdown, and it's rendered here.

As with her emails (mail/formatting.py), raw HTML in what she writes is escaped, never passed
through, and links go only to web and mail addresses, so nothing she quotes from elsewhere can add
markup or scripts to the page. Styling is the page's own stylesheet.

Code blocks that name their language (```python) are highlighted here with Pygments, as the TUI
does, as spans with token classes that the page's code.css colours (monokai, like the TUI).
Pygments escapes the code itself; one with an unknown language, or none, is shown plain.
"""

import re

from markdown_it import MarkdownIt
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound

ALLOWED_LINK = re.compile(r"^(https?:|mailto:)", re.IGNORECASE)
# Longer code is shown plain, so one huge block can't hold up a reply.
HIGHLIGHT_MAX_CHARS = 100_000
# Just the coloured spans: markdown-it wraps them in <pre><code class="language-…">.
_formatter = HtmlFormatter(nowrap=True)


def highlighted(code: str, language: str, _attributes: str) -> str:
    """The code's tokens as coloured spans, or "" to have markdown-it show it plain."""
    if not language or len(code) > HIGHLIGHT_MAX_CHARS:
        return ""
    try:
        lexer = get_lexer_by_name(language.strip().lower(), stripnl=False, ensurenl=False)
    except ClassNotFound:
        return ""
    return highlight(code, lexer, _formatter)


_markdown = MarkdownIt("commonmark", {"html": False, "breaks": True, "linkify": False,
                                      "highlight": highlighted}).enable(["table", "strikethrough"])
# Only web and mail links, never javascript:, data: or anything else a link could carry.
_markdown.validateLink = lambda url: bool(ALLOWED_LINK.match(url.strip()))


def chat_html(text: str) -> str:
    """Her Markdown as safe HTML for one message's body."""
    return _markdown.render(text)
