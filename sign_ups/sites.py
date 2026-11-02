"""Which site an address belongs to, so a sign-up stays on the site the person approved.

A site is its registered name under the Public Suffix List (the publicsuffixlist package, which
carries its own copy, so nothing is fetched): cockburn.wa.gov.au, not all of wa.gov.au, and each
github.io site on its own.
"""

from urllib.parse import urlsplit

from publicsuffixlist import PublicSuffixList

SUFFIXES = PublicSuffixList()


def site_of(url: str) -> str | None:
    """The site an http(s) address belongs to, such as example.com for https://www.app.example.com/x."""
    parts = urlsplit((url or "").strip())
    host = (parts.hostname or "").rstrip(".").lower()
    if parts.scheme.lower() not in ("http", "https") or "." not in host:
        return None
    return SUFFIXES.privatesuffix(host)


def on_site(url: str, site: str) -> bool:
    return site_of(url) == site
