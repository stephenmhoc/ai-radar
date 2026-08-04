"""Shared HTTP fetch helpers.

Collectors follow URLs that come from third-party feeds and web pages, not
from the operator's config. urllib happily opens ``file://``, ``ftp://`` and
``data:`` URLs, so an untrusted feed could otherwise have the collector read a
local file and publish it as article text. Every fetch that can be reached
from remote content goes through :func:`require_http_url` first.
"""

from __future__ import annotations

import urllib.parse
import urllib.request


ALLOWED_SCHEMES = frozenset({"http", "https"})


class UnsupportedURLError(ValueError):
    pass


def require_http_url(url: str, *, purpose: str = "fetch") -> str:
    value = (url or "").strip()
    if not value:
        raise UnsupportedURLError(f"{purpose} requires a URL")
    scheme = urllib.parse.urlparse(value).scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsupportedURLError(f"{purpose} refused non-HTTP URL: {value[:200]}")
    return value


def open_url(url: str, *, user_agent: str, timeout: int, purpose: str = "fetch"):
    request = urllib.request.Request(require_http_url(url, purpose=purpose), headers={"User-Agent": user_agent})
    return urllib.request.urlopen(request, timeout=timeout)


def fetch_bytes(url: str, *, user_agent: str, timeout: int = 30, purpose: str = "fetch") -> bytes:
    with open_url(url, user_agent=user_agent, timeout=timeout, purpose=purpose) as response:
        return response.read()


def fetch_text(url: str, *, user_agent: str, timeout: int = 45, purpose: str = "fetch") -> str:
    with open_url(url, user_agent=user_agent, timeout=timeout, purpose=purpose) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")
