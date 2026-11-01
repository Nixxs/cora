"""Which web addresses Cora's browser may open: public http(s) sites only, never this PC or the home network."""

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

# Names that only mean something on this PC or the local network.
LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home", ".home.arpa", ".corp")


def private_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    return not ip.is_global


async def refused_address(url) -> str | None:
    """Why the browser may not open url, or None when it may.

    Router pages, local services and file paths are off limits, including public names that
    resolve to a private address.
    """
    if not isinstance(url, str):
        return "That isn't a web address."
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in ("http", "https"):
        return "Only http and https web pages can be opened."
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        return "That web address has no site name."
    if host == "localhost" or host.endswith(LOCAL_SUFFIXES) or "." not in host and ":" not in host:
        return "Pages on this PC or the local network can't be opened."
    if private_address(host):
        return "Pages on this PC or the local network can't be opened."
    try:
        found = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        # An unknown name simply fails to load in the browser.
        return None
    if any(private_address(info[4][0]) for info in found):
        return "Pages on this PC or the local network can't be opened."
    return None
