"""Mail from a site she signed up to: which site really sent it, and the link that confirms her address.

Only mail Microsoft verified (DMARC passing for the From domain) counts as the site's. The link is
picked in code, never by a model reading the email: one whose address or nearby words say confirm,
verify or activate, and never an unsubscribe or preferences link.
"""

import re

from sign_ups.sites import site_of

URL = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
CONFIRMING = re.compile(r"confirm|verif|activat|validat|opt-?in|complete[\s_-]*(your\s+)?(registration|sign)",
                        re.IGNORECASE)
NOT_CONFIRMING = re.compile(r"unsubscribe|opt-?out|preferences|privacy|terms|help|support|contact", re.IGNORECASE)
# How far before a link its words are read, such as "Confirm your email: <https://…>".
LEAD_CHARS = 80


def verified_site(mail) -> str | None:
    """The site that verifiably sent this mail (DMARC passing for its From domain), or None."""
    address = (mail.sender_address or "").strip().lower()
    if "@" not in address:
        return None
    domain = address.rsplit("@", 1)[1]
    results = (mail.authentication or "").lower()
    header_from = re.search(r"header\.from=([^\s;]+)", results)
    if re.search(r"\bdmarc=pass\b", results) is None or header_from is None or header_from.group(1) != domain:
        return None
    return site_of(f"https://{domain}/")


def confirmation_link(text: str) -> str | None:
    """The link that confirms her address: its own address says so, or failing that the words just before it."""
    by_words = None
    for found in URL.finditer(text or ""):
        url = found.group(0).rstrip(".,;:!>")
        # The words on the link's own line and the line before it.
        lead = " ".join(text[max(0, found.start() - LEAD_CHARS):found.start()].splitlines()[-2:])
        # Only the address itself, not its query, whose random tokens could spell anything.
        if NOT_CONFIRMING.search(url.split("?", 1)[0]):
            continue
        if CONFIRMING.search(url):
            return url
        if by_words is None and CONFIRMING.search(lead) and not NOT_CONFIRMING.search(lead):
            by_words = url
    return by_words
