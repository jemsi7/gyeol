"""Provider-free six-hour article clustering worker for Explore timelines."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any, Callable

from .article_clustering import (
    CLUSTER_TIME_ZONE,
    ArticleVectorCandidate,
    build_article_clusters,
    cluster_day,
)
from .article_embedding_worker import build_article_embedding_source
from .config import Settings


log = logging.getLogger("gdelt.article_clusters")


def cluster_refresh_slot(now: datetime) -> str:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("cluster slot time must be timezone-aware")
    utc = now.astimezone(UTC)
    start = utc.replace(hour=(utc.hour // 6) * 6, minute=0, second=0, microsecond=0)
    return start.strftime("%Y-%m-%dT%H:00:00Z")


def manual_cluster_slot(now: datetime) -> str:
    """Return a `manual:`-namespaced slot used only by the /ops refresh.

    One snapshot per six-hour slot is a deliberate cost ceiling, so a forced
    rebuild cannot reuse the scheduled slot without destroying the row that
    holds it.  A separate namespace publishes a new snapshot beside the existing
    one and lets the normal supersede path retire the old edition.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("cluster slot time must be timezone-aware")
    utc = now.astimezone(UTC)
    return utc.replace(second=0, microsecond=0).strftime("manual:%Y-%m-%dT%H:%MZ")


class ArticleClusterWorker:
    """Build one deterministic derived snapshot per six-hour UTC slot."""

    def __init__(
        self,
        settings: Settings,
        repo: Any,
        vector_store: Any,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.vector = vector_store
        self.clock = clock

    async def run_once(self, *, force: bool = False) -> dict[str, int | str | float]:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("cluster worker clock must be timezone-aware")
        rows = await self.repo.timeline_cluster_candidates(
            since_days=self.settings.timeline_candidate_days,
            limit=self.settings.timeline_candidate_limit,
            day_span=self.settings.timeline_candidate_day_span,
        )
        candidates: list[ArticleVectorCandidate] = []
        for row in rows:
            try:
                source = build_article_embedding_source(
                    row.get("data"),
                    max_chars=self.settings.article_embedding_source_max_chars,
                )
                published_at = row.get("published_at")
                candidates.append(
                    ArticleVectorCandidate(
                        article_id=str(row.get("article_id") or ""),
                        source_hash=source.source_hash,
                        domain=str(row.get("domain") or "unknown.invalid"),
                        published_at=published_at,
                    )
                )
            except (TypeError, ValueError):
                continue
        candidate_count = len(rows)
        input_hash = _cluster_input_hash(candidates, candidate_count=candidate_count)
        slot = manual_cluster_slot(now) if force else cluster_refresh_slot(now)
        if force:
            # Both slot and input_hash are unique keys, so an unchanged pool
            # would otherwise dedupe straight back onto the stored snapshot.
            input_hash = hashlib.sha256(
                f"{input_hash}:manual:{slot}".encode("utf-8")
            ).hexdigest()
        current = await self.vector.current_cluster_snapshot()
        if (
            not force
            and current is not None
            and current.slot == slot
            and current.input_hash == input_hash
        ):
            return {
                "kind": "not_due",
                "candidates": candidate_count,
                "indexed": current.indexed_count,
                "clusters": len(current.clusters),
            }
        indexed_ids = await self.vector.indexed_article_ids(candidates)
        indexed = [item for item in candidates if item.article_id in indexed_ids]
        indexed_count = len(indexed)
        coverage = indexed_count / candidate_count if candidate_count else 0.0
        if coverage < self.settings.timeline_min_vector_coverage:
            return {
                "kind": "insufficient_coverage",
                "candidates": candidate_count,
                "indexed": indexed_count,
                "coverage": round(coverage, 6),
            }
        edges = await self.vector.article_knn_graph(
            indexed,
            neighbors=self.settings.timeline_knn_neighbors,
            min_similarity=self.settings.timeline_cluster_min_similarity,
        )
        plan = build_article_clusters(
            indexed,
            edges,
            min_members=3,
            min_domains=2,
            max_clusters=self.settings.timeline_max_timelines,
        ).with_population(
            article_count=candidate_count,
            indexed_count=indexed_count,
        )
        snapshot = await self.vector.publish_cluster_snapshot(
            plan,
            input_hash=input_hash,
            slot=slot,
        )
        # Publication is one snapshot per slot: when the slot is already taken
        # the store hands back the stored edition instead of the new plan, and
        # reporting that as "published" hid a stale algorithm from /ops.
        reused = snapshot.input_hash != input_hash or snapshot.slot != slot
        return {
            "kind": "reused" if reused else "published",
            "candidates": candidate_count,
            "indexed": indexed_count,
            "coverage": snapshot.coverage,
            "clusters": len(snapshot.clusters),
            "slot": snapshot.slot,
            "algorithm_version": snapshot.algorithm_version,
        }


def _cluster_input_hash(
    candidates: list[ArticleVectorCandidate],
    *,
    candidate_count: int,
) -> str:
    payload = {
        "candidate_count": candidate_count,
        "articles": [
            {
                "article_id": item.article_id,
                "source_hash": item.source_hash,
                "domain": item.domain.casefold(),
                "published_date": cluster_day(
                    item.published_at,
                    "cluster candidate published_at",
                ).astimezone(CLUSTER_TIME_ZONE).date().isoformat(),
            }
            for item in sorted(candidates, key=lambda value: value.article_id)
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
