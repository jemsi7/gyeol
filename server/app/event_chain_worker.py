"""Assign crypto reports to incident chains (C-13209, PP-4).

The decision itself lives in ``event_chains``; this owns the surrounding work:
which articles to consider, how to gather their neighbours, and how to persist
a join without letting two chains ever claim the same article.

It reuses the embedding index the Explore timelines already maintain rather
than building a second one. What it does not reuse is the clustering
parameters: grouping a day's coverage by topic and asserting two reports are
the same incident are different claims with different costs of being wrong.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping, Sequence
from uuid import UUID, uuid4

from .event_chains import (
    JOIN_SIMILARITY,
    MAX_CHAIN_SPAN,
    MAX_NEIGHBOURS,
    ChainCandidate,
    article_entities,
    chain_decision,
    deduplicate_neighbours,
    should_open_chain,
    story_key,
)

log = logging.getLogger("gdelt.event_chain_worker")

# One pass looks at recent crypto articles only. The chain is a crypto feature
# and the span bound already limits how far back a join can reach.
DEFAULT_BATCH_LIMIT = 60
# Incidents unfold over days, so a pass every ten minutes is ample.
DEFAULT_POLL_INTERVAL_S = 600.0


@dataclass(frozen=True, slots=True)
class ChainOutcome:
    considered: int = 0
    joined: int = 0
    opened: int = 0
    unchained: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "considered": self.considered,
            "joined": self.joined,
            "opened": self.opened,
            "unchained": self.unchained,
        }


def _entities_for(article: Mapping[str, Any]) -> frozenset[str]:
    return article_entities(
        title=str(article.get("title") or ""),
        tagged_atoms=article.get("tagged_atoms") or (),
    )


def plan_assignments(
    articles: Sequence[Mapping[str, Any]],
    neighbours_by_article: Mapping[str, Sequence[ChainCandidate]],
) -> tuple[dict[str, str | None], dict[str, tuple[str, ...]]]:
    """Decide each article's chain without touching the database.

    Returns the joins to apply and the new chains to open. Keeping this pure
    is what lets the decision be measured (G3) against a labelled sample
    without a live index.
    """
    entities = {
        str(article.get("article_id") or ""): _entities_for(article)
        for article in articles
        if isinstance(article, Mapping)
    }
    story_keys = {
        str(article.get("article_id") or ""): story_key(str(article.get("title") or ""))
        for article in articles
        if isinstance(article, Mapping)
    }
    joins: dict[str, str | None] = {}
    opens: dict[str, tuple[str, ...]] = {}
    for article in articles:
        if not isinstance(article, Mapping):
            continue
        article_id = str(article.get("article_id") or "")
        if not article_id:
            continue
        neighbours = list(neighbours_by_article.get(article_id) or ())[:MAX_NEIGHBOURS]
        # Reprints of this same headline leave before anything is judged: they
        # are this report again, not a later one (DR-13233).
        neighbours, reprint_chains = deduplicate_neighbours(
            neighbours,
            story_key=story_keys.get(article_id, ""),
            neighbour_story_keys=story_keys,
        )
        own = entities.get(article_id, frozenset())
        neighbour_entities = {
            candidate.article_id: entities.get(candidate.article_id, frozenset())
            for candidate in neighbours
        }
        chain_id = chain_decision(
            neighbours, entities=own, neighbour_entities=neighbour_entities
        )
        if chain_id is not None and chain_id not in reprint_chains:
            joins[article_id] = chain_id
            continue
        if chain_id is not None:
            # The chain already carries this story; adding it again would make
            # one report look like a follow-up to itself.
            joins[article_id] = None
            continue
        members = should_open_chain(
            neighbours, entities=own, neighbour_entities=neighbour_entities
        )
        if members:
            opens[article_id] = members
        else:
            joins[article_id] = None
    return joins, opens


class EventChainWorker:
    """Walk recent crypto articles and record which incident each belongs to."""

    def __init__(
        self,
        repo: Any,
        vector_store: Any,
        *,
        coverage_domain: str = "crypto",
        batch_limit: int = DEFAULT_BATCH_LIMIT,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        now: Any = None,
    ):
        self.repo = repo
        self.vector_store = vector_store
        self.coverage_domain = coverage_domain
        self.batch_limit = batch_limit
        self.poll_interval_s = poll_interval_s
        self._now = now or (lambda: datetime.now(UTC))
        self._task: asyncio.Task[None] | None = None


    def start(self) -> bool:
        """Run passes on an interval until stopped."""
        if self.vector_store is None:
            log.info("event chain worker disabled (no vector index)")
            return False
        if self._task is not None and not self._task.done():
            return False
        self._task = asyncio.create_task(self._run(), name="event-chain-worker")
        log.info("event chain worker started interval=%ss", self.poll_interval_s)
        return True

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep the supervisor alive
                log.warning("event chain pass failed")
            await asyncio.sleep(self.poll_interval_s)

    async def run_once(self) -> ChainOutcome:
        since = self._now() - MAX_CHAIN_SPAN
        try:
            articles = await self.repo.chainable_articles(
                coverage_domain=self.coverage_domain,
                since=since,
                limit=self.batch_limit,
            )
        except Exception:  # noqa: BLE001 - chaining is enrichment, never fatal
            log.warning("event chain batch unavailable")
            return ChainOutcome()
        if not articles:
            return ChainOutcome()

        try:
            neighbours = await self._neighbours(articles)
        except Exception:  # noqa: BLE001
            log.warning("event chain neighbours unavailable")
            return ChainOutcome(considered=len(articles))

        joins, opens = plan_assignments(articles, neighbours)
        joined = opened = 0
        for article_id, chain_id in joins.items():
            if chain_id is None:
                continue
            if await self._safe_join(chain_id, article_id, neighbours):
                joined += 1
        for article_id, members in opens.items():
            if await self._safe_open(article_id, members, neighbours):
                opened += 1
        unchained = len(articles) - joined - opened
        log.info(
            "event chain pass considered=%d joined=%d opened=%d",
            len(articles),
            joined,
            opened,
        )
        return ChainOutcome(
            considered=len(articles),
            joined=joined,
            opened=opened,
            unchained=unchained,
        )

    async def _neighbours(
        self, articles: Sequence[Mapping[str, Any]]
    ) -> dict[str, list[ChainCandidate]]:
        from .article_clustering import ArticleVectorCandidate

        candidates = []
        for article in articles:
            published = article.get("published_at")
            if not isinstance(published, datetime):
                continue
            candidates.append(
                ArticleVectorCandidate(
                    article_id=str(article.get("article_id") or ""),
                    source_hash=str(article.get("source_hash") or ""),
                    domain=str(article.get("domain") or ""),
                    published_at=published,
                )
            )
        if not candidates:
            return {}
        edges = await self.vector_store.article_knn_graph(
            candidates,
            neighbors=MAX_NEIGHBOURS,
            min_similarity=JOIN_SIMILARITY,
        )
        existing = await self.repo.chain_ids_for_articles(
            [candidate.article_id for candidate in candidates]
        )
        grouped: dict[str, list[ChainCandidate]] = {}
        for edge in edges:
            grouped.setdefault(edge.source_article_id, []).append(
                ChainCandidate(
                    article_id=edge.target_article_id,
                    similarity=float(edge.similarity),
                    chain_id=existing.get(edge.target_article_id),
                )
            )
        for items in grouped.values():
            items.sort(key=lambda item: item.similarity, reverse=True)
        return grouped

    async def _safe_join(
        self,
        chain_id: str,
        article_id: str,
        neighbours: Mapping[str, Sequence[ChainCandidate]],
    ) -> bool:
        similarity = max(
            (
                candidate.similarity
                for candidate in neighbours.get(article_id) or ()
                if candidate.chain_id == chain_id
            ),
            default=JOIN_SIMILARITY,
        )
        try:
            return await self.repo.join_event_chain(
                chain_id=chain_id, article_id=article_id, similarity=similarity
            )
        except Exception:  # noqa: BLE001 - a lost race keeps the first chain
            log.info("event chain join skipped for a concurrent writer")
            return False

    async def _safe_open(
        self,
        article_id: str,
        members: Sequence[str],
        neighbours: Mapping[str, Sequence[ChainCandidate]],
    ) -> bool:
        similarities = {
            candidate.article_id: candidate.similarity
            for candidate in neighbours.get(article_id) or ()
        }
        try:
            return await self.repo.open_event_chain(
                chain_id=str(uuid4()),
                coverage_domain=self.coverage_domain,
                members=[
                    (member, similarities.get(member, JOIN_SIMILARITY))
                    for member in (article_id, *members)
                ],
            )
        except Exception:  # noqa: BLE001 - a lost race leaves no empty chain
            log.info("event chain open skipped for a concurrent writer")
            return False
