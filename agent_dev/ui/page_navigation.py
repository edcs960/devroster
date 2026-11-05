"""Permit top-level loopback page navigation between project ports, never APIs."""

from urllib.parse import urlsplit


def local_page_navigation(handler, path, pages):
    if not (
        handler.command == "GET"
        and path in pages
        and handler.headers.get("Sec-Fetch-Site") == "same-site"
        and handler.headers.get("Sec-Fetch-Mode") == "navigate"
        and handler.headers.get("Sec-Fetch-Dest") == "document"
    ):
        return False
    referrer = handler.headers.get("Referer")
    if referrer:
        parsed = urlsplit(referrer)
        return parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}
    # Product responses intentionally use Referrer-Policy: no-referrer.
    return True
