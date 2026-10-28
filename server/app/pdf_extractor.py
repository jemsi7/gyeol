"""Text layer of a primary-source PDF (I-13206-a, DR-13205).

Regulatory filings and audit reports are the primary documents with the least
substitutable content and the most stubborn format. This reads their embedded
text layer and nothing else: a scanned filing comes back as a failure rather
than as OCR output, because a Map built on misread characters is worse than no
Map, and OCR would widen the deployment surface for a minority of documents.
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
from collections import Counter
from dataclasses import dataclass
from typing import Final

import httpx

log = logging.getLogger("gdelt.pdf")

PDF_MAGIC: Final[bytes] = b"%PDF-"
PDF_CONTENT_TYPES: Final[tuple[str, ...]] = ("application/pdf", "application/x-pdf")
# Header/footer lines repeat on nearly every page and would otherwise dominate
# a section-selected extract.
REPEATED_LINE_MIN_PAGES: Final[int] = 3
MAX_REPEATED_LINE_CHARS: Final[int] = 120
_WHITESPACE_RE: Final = re.compile(r"[ \t]+")


@dataclass(slots=True)
class PdfExtraction:
    url: str
    body_text: str = ""
    page_count: int = 0
    truncated: bool = False
    ok: bool = False
    error: str | None = None


def looks_like_pdf(*, content_type: str, head: bytes, url: str) -> bool:
    """Decide by magic bytes first; a .pdf URL may still serve a login page."""
    if head[: len(PDF_MAGIC)] == PDF_MAGIC:
        return True
    if head:
        # Bytes that are present and not a PDF header settle it, whatever the
        # content type or the extension claim.
        return False
    lowered = (content_type or "").lower()
    return any(kind in lowered for kind in PDF_CONTENT_TYPES) or url.lower().endswith(
        ".pdf"
    )


def _strip_repeated_lines(pages: list[str]) -> str:
    counts: Counter[str] = Counter()
    for page in pages:
        for line in {line.strip() for line in page.splitlines() if line.strip()}:
            if len(line) <= MAX_REPEATED_LINE_CHARS:
                counts[line] += 1
    threshold = min(REPEATED_LINE_MIN_PAGES, len(pages))
    repeated = {
        line
        for line, count in counts.items()
        if len(pages) >= REPEATED_LINE_MIN_PAGES and count >= threshold
    }
    kept: list[str] = []
    for page in pages:
        lines = [
            _WHITESPACE_RE.sub(" ", line).strip()
            for line in page.splitlines()
            if line.strip() and line.strip() not in repeated
        ]
        if lines:
            kept.append("\n".join(lines))
    return "\n\n".join(kept).strip()


def parse_pdf_bytes(payload: bytes, *, max_pages: int) -> PdfExtraction:
    """Extract the text layer, or say precisely why there is none."""
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(payload))
        if reader.is_encrypted:
            # Some filings are "encrypted" with an empty owner password.
            try:
                if reader.decrypt("") == 0:
                    return PdfExtraction(url="", ok=False, error="encrypted")
            except Exception:  # noqa: BLE001 - any decrypt failure is the same
                return PdfExtraction(url="", ok=False, error="encrypted")
        total_pages = len(reader.pages)
        pages = [
            (reader.pages[index].extract_text() or "")
            for index in range(min(total_pages, max_pages))
        ]
    except PdfReadError as exc:
        return PdfExtraction(url="", ok=False, error=f"parse: {type(exc).__name__}")
    except Exception as exc:  # noqa: BLE001 - malformed input is expected here
        return PdfExtraction(url="", ok=False, error=f"parse: {type(exc).__name__}")

    body = _strip_repeated_lines(pages)
    if not body:
        return PdfExtraction(
            url="",
            page_count=total_pages,
            ok=False,
            error="no_text_layer",
        )
    return PdfExtraction(
        url="",
        body_text=body,
        page_count=total_pages,
        truncated=total_pages > max_pages,
        ok=True,
    )


async def extract_pdf(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int,
    max_pages: int,
    timeout_s: float = 30.0,
) -> PdfExtraction:
    """Fetch and read one PDF. Every failure mode returns, none raises."""
    payload = bytearray()
    try:
        async with client.stream("GET", url, timeout=timeout_s) as response:
            if response.status_code != 200:
                return PdfExtraction(url=url, ok=False, error="fetch_status")
            async for chunk in response.aiter_bytes():
                payload.extend(chunk)
                if len(payload) > max_bytes:
                    # A truncated PDF parses into half a document or silently
                    # loses pages, so an oversize file is a refusal, not a cut.
                    return PdfExtraction(url=url, ok=False, error="too_large")
    except httpx.HTTPError as exc:
        log.debug("pdf fetch error %s: %s", url, exc)
        return PdfExtraction(url=url, ok=False, error="fetch_failed")

    if bytes(payload[: len(PDF_MAGIC)]) != PDF_MAGIC:
        return PdfExtraction(url=url, ok=False, error="not_pdf")
    # pypdf is CPU-bound; keep the event loop free, as the HTML path does.
    result = await asyncio.to_thread(parse_pdf_bytes, bytes(payload), max_pages=max_pages)
    result.url = url
    return result
