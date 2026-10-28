"""Fetch an article page and extract clean body text + candidate images.

GDELT DOC 2.0 only returns title/url/socialimage — never the body. To feed the
LLM real content we fetch the page and run trafilatura (the standard readable-
text extractor). Image URLs are harvested from og:image / twitter:image meta
tags and from in-body <graphic> nodes trafilatura emits.

Everything is best-effort: if the page can't be fetched or parsed we return an
empty body and just the GDELT social image, and the mapper marks the result
'degraded' rather than failing the whole run.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx

from .config import Settings
from .pdf_extractor import PDF_CONTENT_TYPES, extract_pdf

# Distinguishes "this is a PDF, read it elsewhere" from "no usable body",
# which ``None`` already means.
_PDF_SENTINEL: str = "\x00pdf"

log = logging.getLogger("gdelt.extractor")

_META_IMG_RE = re.compile(
    r'<meta[^>]+(?:property|name)\s*=\s*["\'](?:og:image(?::url)?|twitter:image(?::src)?)["\'][^>]*>',
    re.IGNORECASE,
)
_CONTENT_RE = re.compile(r'content\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)
_GRAPHIC_RE = re.compile(r'<graphic[^>]+src="([^"]+)"', re.IGNORECASE)


@dataclass(slots=True)
class Extraction:
    url: str
    body_text: str = ""
    image_urls: list[str] = field(default_factory=list)
    ok: bool = False
    error: str | None = None
    strategy: str = "none"


class ArticleExtractor:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.s = settings
        self._client = client
        self._owns = client is None

    async def __aenter__(self) -> "ArticleExtractor":
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.s.extract_timeout_s,
                follow_redirects=True,
                headers={
                    "User-Agent": self.s.gdelt_user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/pdf",
                },
            )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._owns and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def extract(self, url: str) -> Extraction:
        html = await self._fetch_html(url)
        if html is _PDF_SENTINEL:
            return await self._extract_pdf(url)
        if html is None:
            return Extraction(url=url, ok=False, error="fetch failed")
        # trafilatura is CPU-bound; keep the event loop free.
        try:
            body, images, strategy = await asyncio.to_thread(
                _parse,
                html,
                url,
                self.s.map_min_source_chars,
            )
        except Exception as e:  # noqa: BLE001
            log.debug("parse error %s: %s", url, e)
            return Extraction(url=url, ok=False, error=f"parse: {e}")
        ex = Extraction(
            url=url,
            body_text=body,
            image_urls=images,
            ok=bool(body),
            strategy=strategy,
        )
        log.debug("extracted %s: %d chars, %d imgs", url, len(body), len(images))
        return ex

    async def _extract_pdf(self, url: str) -> Extraction:
        """Primary-source documents arrive as PDFs; articles never do."""
        assert self._client is not None
        result = await extract_pdf(
            self._client,
            url,
            max_bytes=self.s.extract_max_pdf_bytes,
            max_pages=self.s.extract_max_pdf_pages,
            timeout_s=self.s.extract_timeout_s,
        )
        return Extraction(
            url=url,
            body_text=result.body_text,
            image_urls=[],
            ok=result.ok,
            error=result.error,
            strategy="pdf" if result.ok else "none",
        )

    async def _fetch_html(self, url: str) -> str | None:
        assert self._client is not None
        try:
            async with self._client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    return None
                ctype = (resp.headers.get("content-type") or "").lower()
                if any(kind in ctype for kind in PDF_CONTENT_TYPES):
                    return _PDF_SENTINEL
                if "html" not in ctype and "xml" not in ctype:
                    return None
                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > self.s.extract_max_html_bytes:
                        break
                enc = resp.encoding or "utf-8"
                return buf.decode(enc, errors="replace")
        except httpx.HTTPError as e:
            log.debug("html fetch error %s: %s", url, e)
            return None


_IGNORED_TEXT_TAGS = {
    "script",
    "style",
    "noscript",
    "nav",
    "header",
    "footer",
    "form",
    "svg",
}


def _compact_text(parts: list[str]) -> str:
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


class _SemanticArticleParser(HTMLParser):
    """Conservative fallback: paragraph text, preferring article/main."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self._semantic_depth = 0
        self._paragraph: list[str] | None = None
        self._paragraph_is_semantic = False
        self._semantic_loose: list[str] = []
        self.all_paragraphs: list[str] = []
        self.semantic_paragraphs: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[no-untyped-def]
        tag = tag.casefold()
        if tag in _IGNORED_TEXT_TAGS:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag in {"article", "main"}:
            self._semantic_depth += 1
        if tag == "p" and self._paragraph is None:
            self._paragraph = []
            self._paragraph_is_semantic = self._semantic_depth > 0

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in _IGNORED_TEXT_TAGS:
            if self._ignored_depth:
                self._ignored_depth -= 1
            return
        if self._ignored_depth:
            return
        if tag == "p" and self._paragraph is not None:
            text = _compact_text(self._paragraph)
            if text:
                self.all_paragraphs.append(text)
                if self._paragraph_is_semantic:
                    self.semantic_paragraphs.append(text)
            self._paragraph = None
            self._paragraph_is_semantic = False
        if tag in {"article", "main"} and self._semantic_depth:
            self._semantic_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._ignored_depth or not data.strip():
            return
        if self._paragraph is not None:
            self._paragraph.append(data)
        if self._semantic_depth:
            self._semantic_loose.append(data)

    def text(self) -> str:
        paragraphs = self.semantic_paragraphs or self.all_paragraphs
        if paragraphs:
            return "\n\n".join(paragraphs).strip()
        return _compact_text(self._semantic_loose)


def _fallback_article_text(html: str) -> str:
    parser = _SemanticArticleParser()
    parser.feed(html)
    parser.close()
    return parser.text()


def _non_whitespace_chars(value: str) -> int:
    return len(re.sub(r"\s+", "", value))


def _parse(
    html: str,
    base_url: str,
    min_chars: int = 0,
) -> tuple[str, list[str], str]:
    import trafilatura

    primary = (
        trafilatura.extract(
            html,
            include_comments=False,
            include_tables=False,
            favor_recall=True,
            url=base_url,
        )
        or ""
    ).strip()
    body = primary
    strategy = "trafilatura" if body else "none"
    if _non_whitespace_chars(primary) < max(0, min_chars):
        fallback = _fallback_article_text(html)
        if _non_whitespace_chars(fallback) > _non_whitespace_chars(primary):
            body = fallback
            strategy = "semantic_html"

    images: list[str] = []
    # og/twitter meta images first (usually the lead photo).
    for tag in _META_IMG_RE.findall(html):
        m = _CONTENT_RE.search(tag)
        if m:
            images.append(m.group(1))
    # in-body images from trafilatura's XML view.
    try:
        xml = trafilatura.extract(
            html, output_format="xml", include_images=True, url=base_url
        )
        if xml:
            images.extend(_GRAPHIC_RE.findall(xml))
    except Exception:  # noqa: BLE001
        pass

    # normalize: absolutize, drop data URIs / trackers, dedup, keep order.
    seen: set[str] = set()
    out: list[str] = []
    for raw in images:
        u = urljoin(base_url, raw.strip())
        if not u.lower().startswith(("http://", "https://")):
            continue
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return body, out, strategy
