"""Domain models: normalized article + image, plus the raw->clean mapping.

Keeping normalization in one place means the DB layer, the engine and any
downstream consumer all agree on field names and types.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlsplit


def _parse_seendate(raw: str | None) -> Optional[datetime]:
    """GDELT 'seendate' looks like '20260710T094500Z'."""
    if not raw:
        return None
    raw = raw.strip()
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _parse_iso_datetime(raw: Any) -> Optional[datetime]:
    """Parse an ISO-8601 provider timestamp and normalize it to UTC."""
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _http_url(raw: Any) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    return value


def canonical_article_id(url: str) -> str:
    """Stable primary key derived from the article URL.

    URLs are the natural identity in DOC 2.0. Hashing gives a fixed-width,
    index-friendly key and sidesteps encoding quirks in raw URLs.
    """
    return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()


@dataclass(slots=True)
class Article:
    id: str
    url: str
    title: Optional[str]
    seendate: Optional[datetime]
    domain: Optional[str]
    language: Optional[str]
    sourcecountry: Optional[str]
    url_mobile: Optional[str]
    socialimage_url: Optional[str]
    # The job/query this article was collected under (provenance).
    source_query: Optional[str] = None
    # Populated after the image is fetched & stored.
    image_id: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)
    # Coverage domain of the collecting job (DR-13208). Stamped by the engine
    # from Job.coverage_domain; only 'core' articles reach map_queue. Declared
    # last so existing positional construction keeps working.
    coverage_domain: str = "core"
    # Curated primary-source attribution (DR-13217). NULL/None means the
    # ordinary article lane. These stay trailing so positional callers keep
    # the exact contract they had before the document lane existed.
    document_type: Optional[str] = None
    document_source_name: Optional[str] = None

    @classmethod
    def from_api(cls, item: dict[str, Any], source_query: str | None = None) -> "Article":
        url = (item.get("url") or "").strip()
        social = (item.get("socialimage") or "").strip() or None
        return cls(
            id=canonical_article_id(url),
            url=url,
            title=(item.get("title") or "").strip() or None,
            seendate=_parse_seendate(item.get("seendate")),
            domain=(item.get("domain") or "").strip() or None,
            language=(item.get("language") or "").strip() or None,
            sourcecountry=(item.get("sourcecountry") or "").strip() or None,
            url_mobile=(item.get("url_mobile") or "").strip() or None,
            socialimage_url=social,
            source_query=source_query,
            raw=item,
        )

    @classmethod
    def from_newsapi(
        cls,
        item: dict[str, Any],
        *,
        source_query: str,
        language: str,
    ) -> Optional["Article"]:
        """Normalize one NewsAPI result into the shared article contract."""
        url = _http_url(item.get("url"))
        raw_title = item.get("title")
        title = raw_title.strip() if isinstance(raw_title, str) else ""
        if not url or not title or title.casefold() == "[removed]":
            return None
        parsed_url = urlsplit(url)
        social = _http_url(item.get("urlToImage"))
        normalized_language = {
            "en": "English",
        }.get(language.strip().lower(), language.strip() or None)
        raw = {**item, "_provider": "newsapi"}
        return cls(
            id=canonical_article_id(url),
            url=url,
            title=title,
            seendate=_parse_iso_datetime(item.get("publishedAt")),
            domain=parsed_url.hostname.lower() if parsed_url.hostname else None,
            language=normalized_language,
            sourcecountry=None,
            url_mobile=None,
            socialimage_url=social,
            source_query=f"newsapi:{source_query}",
            raw=raw,
        )

    @property
    def has_image(self) -> bool:
        return bool(self.socialimage_url)


@dataclass(slots=True)
class StoredImage:
    """Result of downloading + persisting one article image."""

    id: str  # sha256 of the bytes -> natural dedup key
    source_url: str
    content_type: Optional[str]
    byte_size: int
    backend: str  # db | s3 | local
    storage_ref: Optional[str]  # s3 key / local path; None for db bytea
    data: Optional[bytes] = None  # only set for the 'db' backend
