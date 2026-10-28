"""Open one public web page for the publisher-interest researcher (I-13212-a).

This is the first place in the product where a model-supplied URL becomes an
outbound request, so the guards are the module rather than a detail of it: only
http(s), only ports 80/443, and only hosts whose every resolved address is
public. The address check runs again on every redirect hop, because a public
host that 302s to ``a link-local metadata endpoint`` is the whole attack.

Text extraction reuses the extractor's conservative paragraph parser. The
researcher needs evidence quotes, not reading copy, so the semantic fallback is
enough and adds no new parsing dependency.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from dataclasses import dataclass
from typing import Awaitable, Callable, Final, Sequence
from urllib.parse import urljoin, urlsplit

import httpx

# Deliberately the private helper: it is the same conservative parser the
# article extractor falls back to, and duplicating it would let the two drift.
from .extractor import _fallback_article_text as _article_text

log = logging.getLogger("gdelt.web_fetch")

MAX_TEXT_CHARS: Final[int] = 4_000
MAX_TITLE_CHARS: Final[int] = 200
MAX_BYTES: Final[int] = 2_000_000
MAX_REDIRECTS: Final[int] = 3
ALLOWED_PORTS: Final[frozenset[int]] = frozenset({80, 443})
ALLOWED_CONTENT_TYPES: Final[tuple[str, ...]] = (
    "text/html",
    "application/xhtml",
    "text/plain",
)
REDIRECT_STATUSES: Final[frozenset[int]] = frozenset({301, 302, 303, 307, 308})

_TITLE_RE: Final = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_WHITESPACE_RE: Final = re.compile(r"\s+")

Resolver = Callable[[str, int], Awaitable[Sequence[str]]]


class WebFetchError(RuntimeError):
    """A stable, redacted reason the page could not be used."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class FetchedPage:
    url: str
    title: str
    text: str


async def _resolve(host: str, port: int) -> Sequence[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


async def _guard(url: str, resolve: Resolver) -> None:
    """Raise unless this exact URL may be requested.

    Called once per redirect hop. The window between this check and the request
    is a DNS-rebinding risk that pinning the resolved address would close, but
    pinning breaks TLS and virtual hosting; the bounded blast radius here is one
    GET whose body only ever reaches the researcher's prompt.
    """
    parts = urlsplit(url)
    scheme = (parts.scheme or "").casefold()
    if scheme not in ("http", "https"):
        raise WebFetchError("scheme_not_allowed")
    host = parts.hostname
    if not host:
        raise WebFetchError("host_not_allowed")
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise WebFetchError("host_not_allowed") from exc
    if port not in ALLOWED_PORTS:
        raise WebFetchError("host_not_allowed")
    try:
        addresses = await resolve(host, port)
    except WebFetchError:
        raise
    except Exception as exc:  # noqa: BLE001 - resolver internals are not ours
        raise WebFetchError("dns_failed") from exc
    if not addresses:
        raise WebFetchError("dns_failed")
    for raw in addresses:
        try:
            address = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise WebFetchError("address_not_allowed") from exc
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            raise WebFetchError("address_not_allowed")


def _title(html: str) -> str:
    match = _TITLE_RE.search(html)
    if not match:
        return ""
    return _WHITESPACE_RE.sub(" ", match.group(1)).strip()[:MAX_TITLE_CHARS]


def _body(html: str) -> str:
    return _WHITESPACE_RE.sub(" ", _article_text(html)).strip()[:MAX_TEXT_CHARS]


async def fetch_page(
    url: str,
    *,
    client: httpx.AsyncClient,
    timeout_s: float = 8.0,
    resolver: Resolver | None = None,
) -> FetchedPage:
    """Return one page's title and text, or raise ``WebFetchError``."""
    resolve = resolver or _resolve
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        await _guard(current, resolve)
        try:
            async with client.stream(
                "GET", current, follow_redirects=False, timeout=timeout_s
            ) as response:
                if response.status_code in REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location:
                        raise WebFetchError("redirect_without_location")
                    current = urljoin(current, location)
                    continue
                if response.status_code != 200:
                    raise WebFetchError(f"http_{response.status_code}")
                content_type = (response.headers.get("content-type") or "").casefold()
                if not any(kind in content_type for kind in ALLOWED_CONTENT_TYPES):
                    raise WebFetchError("unsupported_content_type")
                buffer = bytearray()
                async for chunk in response.aiter_bytes():
                    buffer.extend(chunk)
                    # Oversized pages are truncated, not refused: the evidence
                    # a researcher needs is near the top of a listing page.
                    if len(buffer) >= MAX_BYTES:
                        break
                encoding = response.encoding or "utf-8"
        except httpx.HTTPError as exc:
            log.debug("web fetch transport failure %s: %s", current, type(exc).__name__)
            raise WebFetchError("transport_failed") from exc
        html = bytes(buffer).decode(encoding, errors="replace")
        text = _body(html)
        if not text:
            raise WebFetchError("empty_document")
        return FetchedPage(url=current, title=_title(html), text=text)
    raise WebFetchError("too_many_redirects")
