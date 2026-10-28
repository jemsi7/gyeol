"""Primary source documents: which ones, and which part of them (DR-13205).

Two deterministic decisions live here. What kind of document a URL is, decided
by a curated source list rather than inference -- a wrong type becomes a wrong
scene structure, so an unlisted URL is simply not collected. And which sections
of a long document to lay out, decided by a per-type keyword order rather than
a model, because the same document must always produce the same reading.

Long filings are narrowed to one Map, never split into a serial: reading route,
budget, resume and bookmarks all assume one article is one Map.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Final, Literal, Sequence

import yaml

log = logging.getLogger("gdelt.primary_documents")

DocumentType = Literal[
    "governance_proposal",
    "post_mortem",
    "audit_report",
    "regulatory_filing",
    "foundation_notice",
]
DOCUMENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "governance_proposal",
        "post_mortem",
        "audit_report",
        "regulatory_filing",
        "foundation_notice",
    }
)
DISCOVERY_KINDS: Final[frozenset[str]] = frozenset({"rss", "index_html"})

# Keyword groups in the order the type should be read. A section that matches
# no group is dropped: it is boilerplate, appendices or navigation.
SECTION_KEYWORDS: Final[dict[str, tuple[tuple[str, ...], ...]]] = {
    "governance_proposal": (
        ("summary", "abstract", "요약", "개요"),
        ("motivation", "rationale", "background", "근거", "배경"),
        ("specification", "implementation", "detail", "명세", "구현"),
        ("against", "risk", "concern", "drawback", "반대", "리스크", "우려"),
        ("vote", "voting", "poll", "quorum", "투표", "정족수"),
    ),
    "post_mortem": (
        ("summary", "overview", "incident", "요약", "사건"),
        ("timeline", "sequence", "what happened", "경과", "타임라인"),
        ("root cause", "cause", "analysis", "원인", "분석"),
        ("impact", "affected", "loss", "영향", "피해"),
        ("response", "remediation", "next steps", "대응", "조치"),
    ),
    "audit_report": (
        ("scope", "methodology", "범위", "방법"),
        ("finding", "issue", "vulnerability", "발견", "취약"),
        ("severity", "risk rating", "심각도", "등급"),
        ("recommendation", "remediation", "fix", "조치", "권고"),
    ),
    "regulatory_filing": (
        ("parties", "petitioner", "respondent", "defendant", "당사자"),
        ("claim", "allegation", "count", "주장", "청구"),
        ("evidence", "exhibit", "basis", "근거", "증거"),
        ("relief", "order", "status", "schedule", "절차", "명령"),
    ),
    "foundation_notice": (
        ("summary", "announcement", "요약", "공지"),
        ("background", "context", "배경"),
        ("impact", "what changes", "영향", "변경"),
    ),
}
# A heading is a short phrase: no terminal punctuation and few words. Markdown
# hashes and numbered clauses are headings whatever their shape.
_MARKDOWN_HEADING_RE: Final = re.compile(r"^#{1,6}\s+\S")
_NUMBERED_HEADING_RE: Final = re.compile(r"^(?:\d+(?:\.\d+)*|[IVXLC]+)[.)]\s+\S")
_TRAILING_PUNCT: Final = ".!?,;:"
MAX_HEADING_CHARS: Final[int] = 80
MAX_HEADING_WORDS: Final[int] = 8
DEFAULT_MAX_SECTION_CHARS: Final[int] = 40_000


@dataclass(frozen=True, slots=True)
class PrimarySource:
    name: str
    document_type: str
    discovery: str
    url: str
    allow_url_prefixes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SelectedSections:
    body_text: str
    selected_headings: tuple[str, ...]
    dropped_headings: tuple[str, ...]
    truncated: bool
    used_fallback: bool


def _source(raw: Any) -> PrimarySource:
    if not isinstance(raw, dict):
        raise ValueError("each primary source must be an object")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise ValueError("primary source name must be a non-empty string")
    document_type = str(raw.get("document_type") or "").strip()
    if document_type not in DOCUMENT_TYPES:
        allowed = ", ".join(sorted(DOCUMENT_TYPES))
        raise ValueError(f"source {name!r} document_type must be one of: {allowed}")
    discovery = str(raw.get("discovery") or "").strip()
    if discovery not in DISCOVERY_KINDS:
        raise ValueError(f"source {name!r} discovery must be rss or index_html")
    url = str(raw.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"source {name!r} needs an http(s) url")
    raw_prefixes = raw.get("allow_url_prefixes")
    if not isinstance(raw_prefixes, list) or not raw_prefixes:
        raise ValueError(f"source {name!r} needs at least one allow_url_prefix")
    prefixes: list[str] = []
    for prefix in raw_prefixes:
        if not isinstance(prefix, str) or not prefix.startswith(("http://", "https://")):
            raise ValueError(f"source {name!r} has a non-http allow_url_prefix")
        prefixes.append(prefix.strip())
    return PrimarySource(
        name=name,
        document_type=document_type,
        discovery=discovery,
        url=url,
        allow_url_prefixes=tuple(prefixes),
    )


def load_primary_sources(path: str) -> tuple[PrimarySource, ...]:
    if not os.path.exists(path):
        # No list means the lane is simply off, not broken.
        log.warning("primary source list %s not found; lane stays idle", path)
        return ()
    with open(path, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    raw_sources = data.get("sources") or []
    if not isinstance(raw_sources, list):
        raise ValueError("sources must be a list")
    sources = [_source(raw) for raw in raw_sources if (raw or {}).get("enabled", True)]
    seen_names: set[str] = set()
    by_prefix: dict[str, str] = {}
    for source in sources:
        if source.name in seen_names:
            raise ValueError(f"duplicate primary source name: {source.name!r}")
        seen_names.add(source.name)
        for prefix in source.allow_url_prefixes:
            if prefix in by_prefix:
                # Two sources claiming one prefix cannot be resolved by length.
                raise ValueError(f"ambiguous allow_url_prefix: {prefix!r}")
            by_prefix[prefix] = source.name
    log.info("loaded %d primary source(s) from %s", len(sources), path)
    return tuple(sources)


def document_type_for(
    url: str, sources: Sequence[PrimarySource]
) -> str | None:
    """Longest matching prefix wins; an unlisted URL is not collected."""
    candidate = (url or "").strip()
    best: tuple[int, str] | None = None
    for source in sources:
        for prefix in source.allow_url_prefixes:
            if candidate.startswith(prefix) and (
                best is None or len(prefix) > best[0]
            ):
                best = (len(prefix), source.document_type)
    return best[1] if best is not None else None


def _is_heading(line: str) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > MAX_HEADING_CHARS:
        return False
    if _MARKDOWN_HEADING_RE.match(stripped):
        return True
    if stripped[-1] in _TRAILING_PUNCT:
        return False
    if _NUMBERED_HEADING_RE.match(stripped):
        return True
    return len(stripped.split()) <= MAX_HEADING_WORDS


def _split_sections(body_text: str) -> list[tuple[str, list[str]]]:
    sections: list[tuple[str, list[str]]] = []
    heading = ""
    lines: list[str] = []
    for line in body_text.splitlines():
        if _is_heading(line):
            if heading or lines:
                sections.append((heading, lines))
            heading = line.strip().lstrip("#").strip()
            lines = []
        else:
            lines.append(line)
    if heading or lines:
        sections.append((heading, lines))
    return sections


def _group_index(heading: str, groups: Sequence[Sequence[str]]) -> int | None:
    lowered = heading.casefold()
    for index, keywords in enumerate(groups):
        if any(keyword in lowered for keyword in keywords):
            return index
    return None


def select_sections(
    body_text: str,
    *,
    document_type: str,
    max_chars: int = DEFAULT_MAX_SECTION_CHARS,
    min_chars: int = 0,
) -> SelectedSections:
    """Narrow a long document to the sections its layout actually needs."""
    groups = SECTION_KEYWORDS.get(document_type)
    sections = _split_sections(body_text or "")
    if groups is None or not sections:
        return _fallback(body_text or "", max_chars=max_chars)

    ranked: list[tuple[int, int, str, str]] = []
    dropped: list[str] = []
    for order, (heading, lines) in enumerate(sections):
        group = _group_index(heading, groups) if heading else None
        text = "\n".join(lines).strip()
        if group is None or not text:
            if heading:
                dropped.append(heading)
            continue
        ranked.append((group, order, heading, text))
    ranked.sort(key=lambda item: (item[0], item[1]))

    chosen: list[str] = []
    headings: list[str] = []
    used = 0
    truncated = False
    for _group, _order, heading, text in ranked:
        block = f"{heading}\n{text}" if heading else text
        if used + len(block) > max_chars:
            # Whole sections only: half a section reads as a complete one.
            truncated = True
            dropped.append(heading)
            continue
        chosen.append(block)
        headings.append(heading)
        used += len(block)

    body = "\n\n".join(chosen).strip()
    if not chosen or len(body) < min_chars:
        # A document whose headings match nothing is still worth reading; an
        # empty selection must never be mistaken for a short document.
        return _fallback(body_text or "", max_chars=max_chars)
    return SelectedSections(
        body_text=body,
        selected_headings=tuple(headings),
        dropped_headings=tuple(dropped),
        truncated=truncated,
        used_fallback=False,
    )


def _fallback(body_text: str, *, max_chars: int) -> SelectedSections:
    truncated = len(body_text) > max_chars
    body = body_text[:max_chars].strip() if truncated else body_text.strip()
    return SelectedSections(
        body_text=body,
        selected_headings=(),
        dropped_headings=(),
        truncated=truncated,
        used_fallback=True,
    )
