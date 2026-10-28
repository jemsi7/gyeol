"""Bounded discovery for curated primary-source documents (DR-13217).

The worker only discovers and persists document URLs. Body/PDF extraction and
Map generation remain on the existing queue/Mapper path.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import logging
import re
from typing import Any
from urllib.parse import urldefrag, urljoin, urlsplit
import xml.etree.ElementTree as ET

import httpx

from .config import Settings
from .models import Article, canonical_article_id
from .primary_documents import PrimarySource, load_primary_sources
from .repository import Repository

log = logging.getLogger("gdelt.primary_document_worker")

_USER_AGENT = "text-shorts-primary-documents/1.0"
_ACCEPT = (
    "application/rss+xml,application/atom+xml,application/xml,text/xml,"
    "text/html,application/xhtml+xml"
)


@dataclass(frozen=True, slots=True)
class PrimaryDocumentRun:
    scanned: int
    eligible: int
    inserted: int
    skipped_existing: int
    rejected: int
    failed_sources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Discovered:
    url: str
    title: str
    published_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class _Candidate:
    source: PrimarySource
    document: _Discovered
    article_id: str


@dataclass(slots=True)
class _SourceAudit:
    source: PrimarySource
    run_id: int
    scanned: int = 0
    inserted: int = 0
    updated: int = 0
    error: str | None = None
    closed: bool = False


class _SourceFailure(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _compact(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _local_name(tag: Any) -> str:
    value = str(tag)
    return value.rsplit("}", 1)[-1].casefold()


def _child_text(node: ET.Element, *names: str) -> str:
    wanted = {name.casefold() for name in names}
    for child in node:
        if _local_name(child.tag) in wanted:
            return _compact(" ".join(child.itertext()))
    return ""


def _http_url(raw: str | None, *, base_url: str) -> str | None:
    value = _compact(raw)
    if not value:
        return None
    try:
        joined = urljoin(base_url, value)
        without_fragment, _fragment = urldefrag(joined)
        parsed = urlsplit(without_fragment)
    except ValueError:
        return None
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return None
    return without_fragment


def _published_at(raw: str | None) -> datetime | None:
    value = _compact(raw)
    if not value:
        return None
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _feed_link(entry: ET.Element, *, base_url: str) -> str | None:
    # Atom may contain self/edit links before the canonical alternate link.
    for child in entry:
        if _local_name(child.tag) != "link":
            continue
        href = _compact(child.attrib.get("href"))
        if href:
            rel = _compact(child.attrib.get("rel")).casefold()
            if rel not in {"", "alternate"}:
                continue
            normalized = _http_url(href, base_url=base_url)
            if normalized is not None:
                return normalized

    # RSS uses element text rather than Atom's href attribute.
    for child in entry:
        if _local_name(child.tag) != "link":
            continue
        normalized = _http_url(
            _compact(" ".join(child.itertext())),
            base_url=base_url,
        )
        if normalized is not None:
            return normalized
    return None


def _parse_feed(
    payload: str,
    *,
    base_url: str,
    scan_limit: int,
) -> tuple[list[_Discovered], int]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise _SourceFailure("primary_document_feed_parse_failed") from exc

    entries: list[ET.Element] = []
    for node in root.iter():
        if _local_name(node.tag) in {"item", "entry"}:
            entries.append(node)
            if len(entries) >= scan_limit:
                break

    documents: list[_Discovered] = []
    for entry in entries:
        title = _child_text(entry, "title")
        link = _feed_link(entry, base_url=base_url) or ""
        published = _child_text(entry, "published", "updated", "pubdate", "date")
        documents.append(
            _Discovered(
                url=link,
                title=title,
                published_at=_published_at(published),
            )
        )
    return documents, len(entries)


class _IndexLinkParser(HTMLParser):
    def __init__(self, *, base_url: str, scan_limit: int):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.scan_limit = scan_limit
        self.scanned = 0
        self.documents: list[_Discovered] = []
        self._active_href: str | None = None
        self._active_fallback = ""
        self._active_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "a":
            return
        if self._active_href is not None:
            self._finish_anchor()
        if self.scanned >= self.scan_limit:
            return
        values = {name.casefold(): value for name, value in attrs}
        href = _compact(values.get("href"))
        if not href:
            return
        self.scanned += 1
        self._active_href = href
        self._active_fallback = _compact(
            values.get("title") or values.get("aria-label")
        )
        self._active_text = []

    def handle_data(self, data: str) -> None:
        if self._active_href is not None:
            self._active_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "a" and self._active_href is not None:
            self._finish_anchor()

    def close(self) -> None:
        super().close()
        if self._active_href is not None:
            self._finish_anchor()

    def _finish_anchor(self) -> None:
        assert self._active_href is not None
        self.documents.append(
            _Discovered(
                url=_http_url(self._active_href, base_url=self.base_url) or "",
                title=_compact(" ".join(self._active_text)) or self._active_fallback,
            )
        )
        self._active_href = None
        self._active_fallback = ""
        self._active_text = []


def _parse_index(
    payload: str,
    *,
    base_url: str,
    scan_limit: int,
) -> tuple[list[_Discovered], int]:
    parser = _IndexLinkParser(base_url=base_url, scan_limit=scan_limit)
    try:
        parser.feed(payload)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - malformed source is isolated
        raise _SourceFailure("primary_document_index_parse_failed") from exc
    return parser.documents, parser.scanned


class PrimaryDocumentWorker:
    def __init__(
        self,
        settings: Settings,
        repo: Repository,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.s = settings
        self.repo = repo
        self._client = client
        self._owns_client = client is None
        self._closed = False

    def _http_client(self) -> httpx.AsyncClient:
        if self._closed:
            raise RuntimeError("primary document worker is closed")
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.s.primary_document_timeout_seconds,
                follow_redirects=True,
                headers={"User-Agent": _USER_AGENT, "Accept": _ACCEPT},
            )
        return self._client

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_client and self._client is not None:
            await self._client.aclose()
        self._client = None

    async def _fetch_text(self, source: PrimarySource) -> str:
        client = self._http_client()
        limit = max(1, int(self.s.extract_max_html_bytes))
        payload = bytearray()
        try:
            async with client.stream(
                "GET",
                source.url,
                timeout=self.s.primary_document_timeout_seconds,
                headers={"User-Agent": _USER_AGENT, "Accept": _ACCEPT},
            ) as response:
                if response.status_code != 200:
                    raise _SourceFailure("primary_document_http_status")
                encoding = response.encoding or "utf-8"
                async for chunk in response.aiter_bytes():
                    payload.extend(chunk)
                    if len(payload) > limit:
                        raise _SourceFailure("primary_document_source_too_large")
        except _SourceFailure:
            raise
        except httpx.HTTPError as exc:
            raise _SourceFailure("primary_document_http_failed") from exc
        return payload.decode(encoding, errors="replace")

    async def _discover(
        self,
        source: PrimarySource,
    ) -> tuple[list[_Discovered], int]:
        payload = await self._fetch_text(source)
        if source.discovery == "rss":
            return _parse_feed(
                payload,
                base_url=source.url,
                scan_limit=self.s.primary_document_scan_limit,
            )
        if source.discovery == "index_html":
            return _parse_index(
                payload,
                base_url=source.url,
                scan_limit=self.s.primary_document_scan_limit,
            )
        raise _SourceFailure("primary_document_discovery_unsupported")

    async def _finish_audit(
        self,
        audit: _SourceAudit,
        *,
        ok: bool,
        error: str | None = None,
    ) -> bool:
        if audit.closed:
            return True
        try:
            await self.repo.finish_run(
                audit.run_id,
                fetched=audit.scanned,
                inserted=audit.inserted,
                updated=audit.updated,
                images_stored=0,
                ok=ok,
                error=error,
                mapped=0,
                maps_failed=0,
            )
        except Exception:  # noqa: BLE001 - one audit must not stop another source
            log.exception("primary document audit close failed source=%s", audit.source.name)
            return False
        audit.closed = True
        return True

    async def run_once(self) -> PrimaryDocumentRun:
        if self._closed:
            raise RuntimeError("primary document worker is closed")

        outstanding = await self.repo.primary_document_outstanding_count()
        outstanding_limit = self.s.primary_document_outstanding_limit
        if outstanding >= outstanding_limit:
            log.info(
                "primary document discovery held outstanding=%d limit=%d",
                outstanding,
                outstanding_limit,
            )
            return PrimaryDocumentRun(0, 0, 0, 0, 0, ())

        sources = load_primary_sources(self.s.primary_documents_file)
        failed_sources: list[str] = []
        scanned = rejected = 0
        candidates: list[_Candidate] = []
        audits: list[_SourceAudit] = []
        seen_urls: set[str] = set()

        def mark_failed(name: str) -> None:
            if name not in failed_sources:
                failed_sources.append(name)

        for source in sources:
            try:
                run_id = await self.repo.start_run(
                    f"primary-document:{source.name}",
                    source.url,
                )
            except Exception:  # noqa: BLE001 - source audit reservation is isolated
                log.exception(
                    "primary document audit reservation failed source=%s",
                    source.name,
                )
                mark_failed(source.name)
                continue

            audit = _SourceAudit(source=source, run_id=run_id)
            try:
                discovered, source_scanned = await self._discover(source)
                audit.scanned = source_scanned
                scanned += source_scanned
            except _SourceFailure as exc:
                audit.error = exc.code
                mark_failed(source.name)
                if not await self._finish_audit(audit, ok=False, error=exc.code):
                    mark_failed(source.name)
                continue
            except Exception as exc:  # noqa: BLE001 - keep other sources moving
                code = f"primary_document_source_failed:{type(exc).__name__}"
                audit.error = code
                mark_failed(source.name)
                if not await self._finish_audit(audit, ok=False, error=code):
                    mark_failed(source.name)
                continue

            source_valid = 0
            for document in discovered:
                title = _compact(document.title)
                url = _http_url(document.url, base_url=source.url)
                if (
                    not title
                    or url is None
                    or not any(url.startswith(prefix) for prefix in source.allow_url_prefixes)
                ):
                    rejected += 1
                    continue
                source_valid += 1
                if url in seen_urls:
                    rejected += 1
                    continue
                seen_urls.add(url)
                candidates.append(
                    _Candidate(
                        source=source,
                        document=_Discovered(
                            url=url,
                            title=title,
                            published_at=document.published_at,
                        ),
                        article_id=canonical_article_id(url),
                    )
                )

            if source_valid == 0:
                code = "primary_document_no_eligible_entries"
                audit.error = code
                mark_failed(source.name)
                if not await self._finish_audit(audit, ok=False, error=code):
                    mark_failed(source.name)
                continue
            audits.append(audit)

        skipped_existing = inserted = 0
        try:
            missing = await self.repo.missing_article_ids(
                [candidate.article_id for candidate in candidates]
            )
            skipped_existing = sum(
                candidate.article_id not in missing for candidate in candidates
            )
            capacity = min(
                self.s.primary_document_insert_limit,
                max(0, outstanding_limit - outstanding),
            )
            audit_by_source = {audit.source.name: audit for audit in audits}
            for candidate in candidates:
                if candidate.article_id not in missing or inserted >= capacity:
                    continue
                article = Article(
                    id=candidate.article_id,
                    url=candidate.document.url,
                    title=candidate.document.title,
                    seendate=candidate.document.published_at,
                    domain=urlsplit(candidate.document.url).hostname,
                    language=None,
                    sourcecountry=None,
                    url_mobile=None,
                    socialimage_url=None,
                    source_query=f"primary-document:{candidate.source.name}",
                    raw={
                        "_provider": "primary_document",
                        "source_name": candidate.source.name,
                        "discovery": candidate.source.discovery,
                    },
                    coverage_domain="crypto",
                    document_type=candidate.source.document_type,
                    document_source_name=candidate.source.name,
                )
                outcome = await self.repo.upsert_article(article)
                audit = audit_by_source[candidate.source.name]
                if outcome == "inserted":
                    inserted += 1
                    audit.inserted += 1
                elif outcome == "updated":
                    skipped_existing += 1
                    audit.updated += 1
                else:
                    raise ValueError(f"unexpected article upsert outcome: {outcome!r}")
        except Exception:
            for audit in audits:
                await self._finish_audit(
                    audit,
                    ok=False,
                    error="primary_document_persistence_failed",
                )
            raise

        for audit in audits:
            if not await self._finish_audit(audit, ok=True):
                mark_failed(audit.source.name)

        return PrimaryDocumentRun(
            scanned=scanned,
            eligible=len(candidates),
            inserted=inserted,
            skipped_existing=skipped_existing,
            rejected=rejected,
            failed_sources=tuple(failed_sources),
        )
