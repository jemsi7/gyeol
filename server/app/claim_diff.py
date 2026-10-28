"""How a figure changed as an incident unfolded (C-13210, PP-4).

Early crypto reporting is almost always wrong about magnitude -- a hack's first
loss estimate rarely matches the final one -- and Gyeol's archive is what keeps
the first version alive. This compares the same subject's figures across a
chain so a reader who saved the early report can find the later one.

It does not decide who was right. Gyeol is not a fact-checking body, and
"published later" is not evidence. Only a document type whose whole purpose is
to establish what happened -- a post-mortem, an audit, a regulatory filing --
turns a change into a correction. Everything else is reported as a change, and
the original scene is never edited or deleted: that it was reported that way is
itself part of the record.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Mapping, Sequence

from .staleness import parse_scaled_number

# Below this the two figures are the same claim rounded differently, not a
# revision worth telling a reader about.
MIN_REVISION_CHANGE: Final[float] = 0.15
# Document types that exist to establish what happened. A later news report is
# just a later news report.
AUTHORITATIVE_DOCUMENT_TYPES: Final[frozenset[str]] = frozenset(
    {"post_mortem", "audit_report", "regulatory_filing"}
)
MAX_CHAIN_ARTICLES: Final[int] = 100

REVISION_SENTENCES: Final[Mapping[str, str]] = {
    "correction": "이 기사의 수치는 이후 {document}{particle} 갱신되었습니다.",
    "change": "이 기사 이후 같은 항목이 다시 보도되었습니다.",
}
DOCUMENT_NAMES: Final[Mapping[str, str]] = {
    "post_mortem": "사후 보고서",
    "audit_report": "감사 보고서",
    "regulatory_filing": "규제 파일링",
}


@dataclass(frozen=True, slots=True)
class ClaimRevision:
    subject: tuple[str, str, str]
    earlier_article_id: str
    earlier_text: str
    later_article_id: str
    later_text: str
    later_document_type: str
    relative_change: float

    @property
    def kind(self) -> str:
        """``correction`` only when the later source establishes facts."""
        return (
            "correction"
            if self.later_document_type in AUTHORITATIVE_DOCUMENT_TYPES
            else "change"
        )

    def sentence(self) -> str:
        template = REVISION_SENTENCES[self.kind]
        if self.kind == "correction":
            document = DOCUMENT_NAMES.get(self.later_document_type, "공식 문서")
            return template.format(
                document=document, particle=instrumental_particle(document)
            )
        return template


def instrumental_particle(word: str) -> str:
    """Korean 으로/로 depends on the preceding syllable's final consonant.

    Getting this wrong is immediately visible to a reader, and the document
    names this sentence interpolates fall on both sides of the rule.
    """
    if not word:
        return "으로"
    last = word[-1]
    if not "가" <= last <= "힣":
        return "으로"
    final_consonant = (ord(last) - 0xAC00) % 28
    # No final consonant, or a final ㄹ, both take the short form.
    return "로" if final_consonant in (0, 8) else "으로"


def _sort_key(article: Mapping[str, Any]) -> tuple[Any, str]:
    published = article.get("published_at")
    # Articles published in the same instant still need one stable order, or
    # the same chain would produce different revisions on different runs.
    return (published or datetime.min, str(article.get("article_id") or ""))


def build_revisions(
    chain_articles: Sequence[Mapping[str, Any]]
) -> tuple[ClaimRevision, ...]:
    """Adjacent changes to the same subject, oldest first.

    Each article supplies ``article_id``, ``published_at``, ``document_type``
    and its ``tagged_atoms``.
    """
    ordered = sorted(
        (item for item in chain_articles or () if isinstance(item, Mapping)),
        key=_sort_key,
    )[-MAX_CHAIN_ARTICLES:]

    by_subject: dict[tuple[str, str, str], list[tuple[Mapping[str, Any], Mapping[str, Any]]]] = {}
    for article in ordered:
        for atom in article.get("tagged_atoms") or ():
            if not isinstance(atom, Mapping):
                continue
            subject = (
                str(atom.get("tag") or ""),
                str(atom.get("subject_kind") or ""),
                str(atom.get("subject_id") or ""),
            )
            if not all(subject):
                continue
            by_subject.setdefault(subject, []).append((article, atom))

    revisions: list[ClaimRevision] = []
    for subject, entries in by_subject.items():
        for (earlier, earlier_atom), (later, later_atom) in zip(entries, entries[1:]):
            earlier_text = str(earlier_atom.get("atom") or "")
            later_text = str(later_atom.get("atom") or "")
            earlier_value = parse_scaled_number(earlier_text)
            later_value = parse_scaled_number(later_text)
            if earlier_value is None or later_value is None:
                continue
            if earlier_value[0] <= 0:
                continue
            change = (later_value[0] - earlier_value[0]) / earlier_value[0]
            if abs(change) < MIN_REVISION_CHANGE:
                continue
            revisions.append(
                ClaimRevision(
                    subject=subject,
                    earlier_article_id=str(earlier.get("article_id") or ""),
                    earlier_text=earlier_text,
                    later_article_id=str(later.get("article_id") or ""),
                    later_text=later_text,
                    later_document_type=str(later.get("document_type") or ""),
                    relative_change=change,
                )
            )
    return tuple(revisions)


def revision_for_article(
    article_id: str, revisions: Sequence[ClaimRevision]
) -> ClaimRevision | None:
    """The note this article should carry, if any.

    A correction outranks a plain change, and the most recent one wins: a
    reader needs the current state of the story, not its whole history.
    """
    candidates = [
        revision
        for revision in revisions
        if revision.earlier_article_id == article_id
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item.kind == "correction", abs(item.relative_change)))
    return candidates[-1]
