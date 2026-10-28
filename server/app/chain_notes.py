"""Telling a reader their saved story moved (C-13210, C-13211, PP-4).

The archive is what makes Gyeol worth saving into, and it is also what
preserves a first report that later turned out wrong. These two notes close
that gap without breaking the reading: one line at the point of entry, never a
modal, never a badge, never a push.

Neither note edits a scene. That an incident was reported a certain way at a
certain time is itself part of the record, so the correction travels beside
the article rather than inside it.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Final, Mapping, Sequence

from .claim_diff import ClaimRevision, revision_for_article

CHAIN_PROGRESS_SENTENCE: Final[str] = "보관 이후 관련 보도 {count}건이 추가되었습니다."


def attach_revision_notes(
    items: Sequence[Mapping[str, Any]],
    revisions: Sequence[ClaimRevision],
) -> int:
    """Write each article's forward revision onto its own payload."""
    attached = 0
    for item in items or ():
        if not isinstance(item, dict):
            continue
        item.setdefault("revision_note", "")
        item.setdefault("revision_target_article_id", "")
        article_id = str(item.get("article_id") or "")
        if not article_id:
            continue
        revision = revision_for_article(article_id, revisions)
        if revision is None:
            continue
        item["revision_note"] = revision.sentence()
        item["revision_target_article_id"] = revision.later_article_id
        attached += 1
    return attached


def chain_progress_note(
    *,
    saved_at: datetime | None,
    joined_at: Sequence[datetime],
    revision_note: str = "",
) -> str:
    """One line about what arrived since this article was saved.

    A correction is the more useful thing to say, so it wins outright. With no
    progress the note is empty and nothing renders -- reopening a saved article
    that has not moved should feel exactly as it did before.
    """
    if revision_note:
        return revision_note
    if saved_at is None:
        return ""
    added = sum(1 for moment in joined_at or () if moment > saved_at)
    if added <= 0:
        return ""
    return CHAIN_PROGRESS_SENTENCE.format(count=added)


def attach_chain_progress(
    items: Sequence[Mapping[str, Any]],
    *,
    saved_at: Mapping[str, datetime],
    chain_joins: Mapping[str, Sequence[datetime]],
) -> int:
    """Annotate saved items whose incident has moved since they were saved.

    ``saved_at`` is the latest save per article -- re-saving restarts the
    baseline, because a reader who saved it again has seen what was there.
    """
    attached = 0
    for item in items or ():
        if not isinstance(item, dict):
            continue
        item.setdefault("chain_progress_note", "")
        article_id = str(item.get("article_id") or "")
        if not article_id:
            continue
        note = chain_progress_note(
            saved_at=saved_at.get(article_id),
            joined_at=chain_joins.get(article_id) or (),
            revision_note=str(item.get("revision_note") or ""),
        )
        if note:
            item["chain_progress_note"] = note
            attached += 1
    return attached
