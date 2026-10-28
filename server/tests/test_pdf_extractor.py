"""Primary-source PDF extraction (I-13206-a, DR-13205).

Fixtures are real PDFs written by pypdf rather than hand-rolled bytes, so the
failure modes here are the ones a filing actually produces. The recurring theme
is that a partial read is never returned: half a filing looks like a whole one
to every stage downstream.
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.pdf_extractor import (
    PdfExtraction,
    extract_pdf,
    looks_like_pdf,
    parse_pdf_bytes,
)


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "pdf"


def _bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _extract(payload: bytes, *, status: int = 200, max_bytes: int = 4_000_000,
                   max_pages: int = 200) -> PdfExtraction:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            content=payload,
            headers={"content-type": "application/pdf"},
        )

    async with _client(handler) as client:
        return await extract_pdf(
            client,
            "https://example.test/filing.pdf",
            max_bytes=max_bytes,
            max_pages=max_pages,
        )


# --- parsing -----------------------------------------------------------------

def test_reads_the_text_layer():
    result = parse_pdf_bytes(_bytes("text_three_pages.pdf"), max_pages=200)
    assert result.ok
    assert result.page_count == 3
    assert "Ethereum TVL fell to $48 billion." in result.body_text
    assert result.truncated is False


def test_repeated_headers_are_removed():
    result = parse_pdf_bytes(_bytes("text_three_pages.pdf"), max_pages=200)
    # The running header appears on every page and would otherwise crowd out
    # the section-selected extract.
    assert "EXAMPLE FILING 2026" not in result.body_text
    assert "Findings" in result.body_text


def test_a_short_document_keeps_its_only_line():
    # The repeated-line rule must not eat a one-page document whole.
    result = parse_pdf_bytes(_bytes("single_page.pdf"), max_pages=200)
    assert result.ok
    assert "Only one page of body text here." in result.body_text


def test_encrypted_documents_fail_softly():
    result = parse_pdf_bytes(_bytes("encrypted.pdf"), max_pages=200)
    assert (result.ok, result.error) == (False, "encrypted")


def test_a_scanned_document_has_no_text_layer():
    # No OCR by design: misread characters are worse than no document.
    result = parse_pdf_bytes(_bytes("no_text_layer.pdf"), max_pages=200)
    assert (result.ok, result.error) == (False, "no_text_layer")


def test_page_cap_truncates_and_says_so():
    result = parse_pdf_bytes(_bytes("many_pages.pdf"), max_pages=5)
    assert result.ok
    assert result.page_count == 12
    assert result.truncated is True
    assert "page 5." in result.body_text
    assert "page 6." not in result.body_text


def test_malformed_bytes_are_reported_not_raised():
    result = parse_pdf_bytes(b"%PDF-1.7\nnot really a pdf", max_pages=200)
    assert result.ok is False
    assert result.error.startswith("parse:")


# --- fetching ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetches_and_parses():
    result = await _extract(_bytes("text_three_pages.pdf"))
    assert result.ok
    assert result.url.endswith("filing.pdf")


@pytest.mark.asyncio
async def test_oversize_is_refused_rather_than_cut():
    result = await _extract(_bytes("many_pages.pdf"), max_bytes=64)
    assert (result.ok, result.error) == (False, "too_large")


@pytest.mark.asyncio
async def test_non_200_is_a_fetch_failure():
    result = await _extract(_bytes("single_page.pdf"), status=404)
    assert (result.ok, result.error) == (False, "fetch_status")


@pytest.mark.asyncio
async def test_html_served_as_pdf_is_rejected_by_magic_bytes():
    result = await _extract(b"<html><body>login required</body></html>")
    assert (result.ok, result.error) == (False, "not_pdf")


@pytest.mark.asyncio
async def test_transport_errors_are_reported_not_raised():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    async with _client(handler) as client:
        result = await extract_pdf(
            client, "https://example.test/a.pdf", max_bytes=1000, max_pages=10
        )
    assert (result.ok, result.error) == (False, "fetch_failed")


# --- content-type routing ----------------------------------------------------

@pytest.mark.parametrize(
    ("content_type", "head", "url", "expected"),
    [
        ("application/pdf", b"%PDF-1.7", "https://a.test/x.pdf", True),
        ("text/html", b"%PDF-1.7", "https://a.test/x", True),
        ("application/pdf", b"<html>", "https://a.test/x.pdf", False),
        ("application/pdf", b"", "https://a.test/x", True),
        ("text/html", b"", "https://a.test/x.pdf", True),
        ("text/html", b"", "https://a.test/x", False),
    ],
)
def test_magic_bytes_outrank_the_declared_type(content_type, head, url, expected):
    assert looks_like_pdf(content_type=content_type, head=head, url=url) is expected


def test_extractor_routes_pdf_content_types_to_the_pdf_path():
    import inspect

    from app.extractor import ArticleExtractor

    fetch = inspect.getsource(ArticleExtractor._fetch_html)
    assert "PDF_CONTENT_TYPES" in fetch
    assert "_PDF_SENTINEL" in fetch
    extract = inspect.getsource(ArticleExtractor.extract)
    assert "_extract_pdf(url)" in extract
    # The PDF branch is decided before the "no usable body" branch, so a PDF is
    # never reported as a failed HTML fetch.
    assert extract.index("_PDF_SENTINEL") < extract.index("html is None")
