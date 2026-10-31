"""Her emails as formatted HTML: she writes simple Markdown, and it's rendered here.

Raw HTML in what she writes is escaped, never passed through, and links go only to web and mail
addresses, so nothing she quotes from elsewhere can add markup or scripts. A single line break
stays a line break, so plain text (a sign-off, a signature) looks as written. Styles are inline,
as Outlook and other mail apps ignore most stylesheets; no colours are set, so dark mode
still works.
"""

import re

from markdown_it import MarkdownIt

FONT = "font-family:'Segoe UI',Calibri,Arial,sans-serif;font-size:11pt;line-height:1.45"
CELL = "border:1px solid #c8c8c8;padding:4px 8px;"
STYLES = {
    "<table>": '<table style="border-collapse:collapse;margin:8px 0">',
    "<h1>": '<h1 style="font-size:16pt;margin:16px 0 8px">',
    "<h2>": '<h2 style="font-size:14pt;margin:16px 0 6px">',
    "<h3>": '<h3 style="font-size:12pt;margin:12px 0 4px">',
    "<p>": '<p style="margin:0 0 10px">',
    "<ul>": '<ul style="margin:0 0 10px;padding-left:22px">',
    "<ol>": '<ol style="margin:0 0 10px;padding-left:22px">',
    "<blockquote>": '<blockquote style="margin:0 0 10px;padding-left:10px;border-left:3px solid #c8c8c8">',
}
CELL_TAG = re.compile(r'<(td|th)(?: style="([^"]*)")?>')
ALLOWED_LINK = re.compile(r"^(https?:|mailto:)", re.IGNORECASE)

_markdown = MarkdownIt("commonmark", {"html": False, "breaks": True, "linkify": False}).enable(
    ["table", "strikethrough"])
# Only web and mail links, never file:, data: or anything else a link could carry.
_markdown.validateLink = lambda url: bool(ALLOWED_LINK.match(url.strip()))


def email_fragment(text: str) -> str:
    """Her Markdown as styled HTML, to go inside an email's body."""
    rendered = _markdown.render(text)
    for tag, styled in STYLES.items():
        rendered = rendered.replace(tag, styled)
    rendered = CELL_TAG.sub(lambda match: f'<{match[1]} style="{CELL}{match[2] or ""}">', rendered)
    return f'<div style="{FONT}">{rendered}</div>'


def email_html(text: str) -> str:
    """A whole HTML email from her Markdown."""
    return f'<html><head><meta charset="utf-8"></head><body>{email_fragment(text)}</body></html>'
