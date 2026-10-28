"""Four-hour offline Explore timeline selection and atomic publication."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Mapping, Sequence
from uuid import UUID, uuid5

from .article_cluster_worker import ArticleClusterWorker, cluster_refresh_slot
from .article_clustering import ArticleCluster, ArticleVectorCandidate
from .article_embedding_worker import build_article_embedding_source
from .config import Settings
from .explore_timeline import (
    TimelineCandidate,
    TimelineCluster,
    TimelineDateSource,
    TimelineLane,
    TimelineNode,
    TimelinePlan,
    TimelineSimilarityEdge,
    TimelineUpdateKind,
    TimelineValidationError,
    build_timeline_plan,
    validate_timeline_catalog,
)
from .openrouter import (
    OpenRouterPermanentError,
    OpenRouterRequestError,
    OpenRouterTransientError,
)
from .openrouter_rerank import RerankDocument
from .timeline_copy import TimelineCopy, TimelineCopyInput


log = logging.getLogger("gdelt.explore_timeline_worker")


def candidate_refresh_slot(now: datetime) -> str:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("candidate slot time must be timezone-aware")
    utc = now.astimezone(UTC)
    start = utc.replace(hour=(utc.hour // 4) * 4, minute=0, second=0, microsecond=0)
    return start.strftime("%Y-%m-%dT%H:00:00Z")


def manual_candidate_slot(now: datetime) -> str:
    """Return a minute-precision slot reserved for the /ops manual refresh.

    ``candidate_slot`` is unique per edition, which is what stops the scheduled
    worker from paying for the same four-hour window twice.  A forced run takes
    a slot the schedule will never emit so it can publish without discarding the
    edition the current window already claimed.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("candidate slot time must be timezone-aware")
    utc = now.astimezone(UTC)
    return utc.replace(second=0, microsecond=0).strftime("manual:%Y-%m-%dT%H:%MZ")


@dataclass(frozen=True, slots=True)
class _CandidateRecord:
    vector: ArticleVectorCandidate
    row: Mapping[str, Any]
    text: str


class ExploreTimelineWorker:
    def __init__(
        self,
        settings: Settings,
        repo: Any,
        vector_store: Any,
        cluster_worker: ArticleClusterWorker | Any,
        rerank: Any,
        copy_polisher: Any,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.vector = vector_store
        self.cluster_worker = cluster_worker
        self.rerank = rerank
        self.copy = copy_polisher
        self.clock = clock
        self._task: asyncio.Task[None] | None = None
        # A manual /ops refresh and the poll loop must never claim at once.
        self._lock = asyncio.Lock()
        self.started = False
        # /ops needs to distinguish "quietly not due" from "cannot build one",
        # which neither the catalog ledger nor the previous logs recorded.
        self.last_run: dict[str, Any] | None = None
        self.last_cluster_run: dict[str, Any] | None = None
        self.last_error_code: str | None = None
        self.last_run_at: datetime | None = None

    @property
    def status(self) -> dict[str, Any]:
        """Return the in-process worker state shown on the /ops dashboard."""
        return {
            "enabled": bool(self.settings.timeline_worker_enabled),
            "rerank_configured": bool(getattr(self.rerank, "configured", False)),
            "running": bool(
                self._task is not None and not self._task.done()
            ),
            "started": self.started,
            "poll_seconds": self.settings.timeline_poll_seconds,
            "candidate_day_span": self.settings.timeline_candidate_day_span,
            "candidate_limit": self.settings.timeline_candidate_limit,
            "rerank_model": self.settings.timeline_rerank_model,
            "last_run_at": self.last_run_at,
            "last_run": self.last_run,
            "last_cluster_run": self.last_cluster_run,
            "last_error_code": self.last_error_code,
        }

    def start(self) -> bool:
        if (
            not self.settings.timeline_worker_enabled
            or not bool(getattr(self.rerank, "configured", False))
        ):
            log.info("explore timeline worker disabled or rerank unavailable")
            return False
        if self._task is not None and not self._task.done():
            return False
        self._task = asyncio.create_task(self._run(), name="explore-timeline-worker")
        self.started = True
        log.info(
            "explore timeline worker started cluster_seconds=%s candidate_seconds=%s",
            self.settings.timeline_cluster_refresh_seconds,
            self.settings.timeline_candidate_refresh_seconds,
        )
        return True

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        log.info("explore timeline worker stopped")

    async def run_once(self, *, force: bool = False) -> dict[str, int | str]:
        """Run one refresh and record its outcome for /ops.

        ``force`` is the operator-triggered path: it rebuilds the cluster
        snapshot and claims a fresh edition even when the scheduled slots have
        already been spent.  It costs one rerank batch plus one copy call, so
        only /ops may set it.
        """
        started_at = self.clock()
        async with self._lock:
            try:
                result = await self._refresh_once(force=force)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.last_run_at = started_at
                self.last_run = {"kind": "exception", "published": 0}
                self.last_error_code = type(error).__name__
                raise
            self.last_run_at = started_at
            self.last_run = {**result, "forced": force}
            self.last_error_code = (
                None
                if result.get("kind") in {"published", "not_due"}
                else str(result.get("kind"))
            )
        log.info(
            "explore timeline run forced=%s %s",
            force,
            " ".join(f"{key}={value}" for key, value in sorted(result.items())),
        )
        return result

    async def _refresh_once(self, *, force: bool = False) -> dict[str, int | str]:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("timeline worker clock must be timezone-aware")
        self.last_cluster_run = dict(
            await self.cluster_worker.run_once(force=force) or {}
        )
        snapshot = await self.vector.current_cluster_snapshot()
        if snapshot is None:
            return {"kind": "cluster_unavailable", "candidates": 0, "published": 0}
        if len(snapshot.clusters) < self.settings.timeline_min_timelines:
            return {
                "kind": "insufficient_clusters",
                "candidates": snapshot.article_count,
                "published": 0,
            }
        rows = await self.repo.timeline_cluster_candidates(
            since_days=self.settings.timeline_candidate_days,
            limit=self.settings.timeline_candidate_limit,
            day_span=self.settings.timeline_candidate_day_span,
        )
        records = _candidate_records(
            rows,
            max_chars=self.settings.article_embedding_source_max_chars,
        )
        indexed_ids = await self.vector.indexed_article_ids(
            [record.vector for record in records.values()]
        )
        records = {
            article_id: record
            for article_id, record in records.items()
            if article_id in indexed_ids
        }
        memberships = await self._cluster_memberships(snapshot.clusters, records)
        eligible = [
            cluster
            for cluster in snapshot.clusters[: self.settings.timeline_max_timelines]
            if len(memberships.get(cluster.cluster_key, ())) >= self.settings.timeline_min_nodes
        ]
        if len(eligible) < self.settings.timeline_min_timelines:
            return {
                "kind": "insufficient_clusters",
                "candidates": len(rows),
                "published": 0,
            }
        slot = manual_candidate_slot(now) if force else candidate_refresh_slot(now)
        input_hash = _candidate_input_hash(
            snapshot_id=str(snapshot.id),
            slot=slot,
            records=records,
        )
        claimed = await self.repo.claim_explore_timeline_catalog(
            cluster_snapshot_id=str(snapshot.id),
            cluster_slot=snapshot.slot,
            candidate_slot=slot,
            input_hash=input_hash,
            candidate_count=len(rows),
            max_attempts=self.settings.timeline_max_attempts,
            lease_s=self.settings.timeline_processing_lease_s,
        )
        if claimed is None:
            return {"kind": "not_due", "candidates": len(rows), "published": 0}
        catalog_id = claimed["id"]
        try:
            previous_catalog = await self.repo.current_explore_timeline_catalog(
                now=now,
                stale_hours=self.settings.timeline_stale_hours,
            )
            previous_plans = _previous_plans(previous_catalog)
            plans: list[TimelinePlan] = []
            headlines_by_key: dict[str, tuple[str, ...]] = {}
            for cluster in eligible:
                member_ids = memberships[cluster.cluster_key]
                documents = _cluster_documents(
                    cluster,
                    member_ids,
                    records,
                    limit=self.settings.timeline_rerank_document_limit,
                )
                if len(documents) < self.settings.timeline_min_nodes:
                    continue
                medoid = records.get(cluster.medoid_article_id)
                topic = _topic_for_cluster(cluster, medoid)
                rerank_results = await self.rerank.rerank(
                    query=f"{topic} 관련 사건의 시간 흐름과 주요 전개",
                    documents=[
                        RerankDocument(article_id=article_id, text=records[article_id].text[:4000])
                        for article_id in documents
                    ],
                    top_n=len(documents),
                )
                ranked_candidates = [
                    _timeline_candidate(
                        result.article_id,
                        relevance=result.relevance_score,
                        cluster=cluster,
                        records=records,
                    )
                    for result in rerank_results
                ]
                reranked_vectors = [
                    records[result.article_id].vector for result in rerank_results
                ]
                directed = await self.vector.article_similarity_graph(
                    reranked_vectors,
                    min_similarity=0.0,
                )
                similarities = _timeline_edges(directed)
                try:
                    plan = build_timeline_plan(
                        TimelineCluster(
                            cluster_key=cluster.cluster_key,
                            topic=topic,
                            member_article_ids=tuple(documents),
                        ),
                        ranked_candidates,
                        similarities,
                        previous_plans,
                        min_nodes=self.settings.timeline_min_nodes,
                        max_nodes=self.settings.timeline_max_nodes,
                    )
                except TimelineValidationError:
                    continue
                plans.append(plan)
                headlines_by_key[plan.stable_key] = tuple(
                    _headline(records[node.article_id].row) for node in plan.nodes
                )
            frozen = validate_timeline_catalog(
                plans,
                min_timelines=self.settings.timeline_min_timelines,
                max_timelines=self.settings.timeline_max_timelines,
            )
            previous_copy = {
                plan.stable_key: TimelineCopy(plan.title, plan.summary)
                for plan in previous_plans
            }
            polished = await self.copy.polish(
                [
                    TimelineCopyInput(
                        stable_key=plan.stable_key,
                        topic=plan.topic,
                        started_at=plan.started_at,
                        ended_at=plan.ended_at,
                        headlines=headlines_by_key[plan.stable_key],
                    )
                    for plan in frozen
                ],
                previous_copy,
            )
            final_plans = validate_timeline_catalog(
                [
                    replace(
                        plan,
                        title=polished[plan.stable_key].title,
                        summary=polished[plan.stable_key].summary,
                    )
                    for plan in frozen
                ],
                min_timelines=self.settings.timeline_min_timelines,
                max_timelines=self.settings.timeline_max_timelines,
            )
            timelines = _publication_payload(catalog_id, final_plans)
            await self.repo.publish_explore_timeline_catalog(
                catalog_id,
                timelines=timelines,
                model=self.settings.timeline_rerank_model,
                valid_until=now + timedelta(hours=self.settings.timeline_valid_hours),
            )
        except asyncio.CancelledError:
            raise
        except OpenRouterPermanentError:
            return await self._retry(catalog_id, len(rows), "provider_permanent")
        except OpenRouterTransientError:
            return await self._retry(catalog_id, len(rows), "provider_transient")
        except OpenRouterRequestError:
            return await self._retry(catalog_id, len(rows), "invalid_timeline_plan")
        except TimelineValidationError:
            return await self._retry(catalog_id, len(rows), "invalid_timeline_plan")
        except Exception as error:  # noqa: BLE001 - retry ledger stores stable code only
            log.warning("explore timeline run failed error=%s", type(error).__name__)
            return await self._retry(catalog_id, len(rows), "timeline_unavailable")
        return {
            "kind": "published",
            "candidates": len(rows),
            "timelines": len(final_plans),
            "published": 1,
        }

    async def _cluster_memberships(
        self,
        clusters: Sequence[ArticleCluster],
        records: Mapping[str, _CandidateRecord],
    ) -> dict[str, tuple[str, ...]]:
        memberships = {
            cluster.cluster_key: tuple(
                member.article_id
                for member in cluster.members
                if member.article_id in records
            )
            for cluster in clusters
        }
        existing = {
            article_id for member_ids in memberships.values() for article_id in member_ids
        }
        new_records = [record for key, record in records.items() if key not in existing]
        medoid_records = [
            records[cluster.medoid_article_id]
            for cluster in clusters
            if cluster.medoid_article_id in records
        ]
        if not new_records or not medoid_records:
            return memberships
        assignment_records = {
            record.vector.article_id: record for record in new_records + medoid_records
        }
        edges = await self.vector.article_knn_graph(
            [assignment_records[key].vector for key in sorted(assignment_records)],
            neighbors=min(8, max(1, len(assignment_records) - 1)),
            min_similarity=self.settings.timeline_assignment_min_similarity,
        )
        medoid_to_cluster = {
            cluster.medoid_article_id: cluster for cluster in clusters
        }
        scores: dict[str, list[tuple[float, int, str]]] = {}
        new_ids = {record.vector.article_id for record in new_records}
        for edge in edges:
            if edge.source_article_id in new_ids and edge.target_article_id in medoid_to_cluster:
                cluster = medoid_to_cluster[edge.target_article_id]
                scores.setdefault(edge.source_article_id, []).append(
                    (edge.similarity, cluster.rank, cluster.cluster_key)
                )
            elif edge.target_article_id in new_ids and edge.source_article_id in medoid_to_cluster:
                cluster = medoid_to_cluster[edge.source_article_id]
                scores.setdefault(edge.target_article_id, []).append(
                    (edge.similarity, cluster.rank, cluster.cluster_key)
                )
        mutable = {key: list(value) for key, value in memberships.items()}
        for article_id, choices in sorted(scores.items()):
            choices.sort(key=lambda item: (-item[0], item[1], item[2]))
            mutable[choices[0][2]].append(article_id)
        return {key: tuple(value) for key, value in mutable.items()}

    async def _retry(self, catalog_id: UUID, candidates: int, code: str) -> dict[str, int | str]:
        status = await self.repo.retry_explore_timeline_catalog(
            catalog_id,
            error_code=code,
            base_delay_s=self.settings.timeline_retry_base_delay_s,
        )
        return {"kind": status, "candidates": candidates, "published": 0}

    async def _run(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - next poll safely retries
                log.warning("explore timeline poll failed error=%s", type(error).__name__)
            await asyncio.sleep(self.settings.timeline_poll_seconds)


def _candidate_records(
    rows: Sequence[Mapping[str, Any]],
    *,
    max_chars: int,
) -> dict[str, _CandidateRecord]:
    result: dict[str, _CandidateRecord] = {}
    for row in rows:
        try:
            article_id = str(row.get("article_id") or "")
            source = build_article_embedding_source(row.get("data"), max_chars=max_chars)
            vector = ArticleVectorCandidate(
                article_id=article_id,
                source_hash=source.source_hash,
                domain=str(row.get("domain") or "unknown.invalid"),
                published_at=row.get("published_at"),
            )
            if article_id in result:
                raise ValueError("duplicate timeline candidate")
            result[article_id] = _CandidateRecord(vector=vector, row=row, text=source.text)
        except (TypeError, ValueError):
            continue
    return result


def _candidate_input_hash(
    *,
    snapshot_id: str,
    slot: str,
    records: Mapping[str, _CandidateRecord],
) -> str:
    payload = {
        "snapshot_id": snapshot_id,
        "slot": slot,
        "articles": [
            (key, records[key].vector.source_hash) for key in sorted(records)
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _cluster_documents(
    cluster: ArticleCluster,
    member_ids: Sequence[str],
    records: Mapping[str, _CandidateRecord],
    *,
    limit: int,
) -> tuple[str, ...]:
    centrality = {
        member.article_id: member.centrality_rank for member in cluster.members
    }
    ordered = sorted(
        (article_id for article_id in member_ids if article_id in records),
        key=lambda article_id: (
            centrality.get(article_id, 501),
            article_id,
        ),
    )
    return tuple(ordered[:limit])


def _topic_for_cluster(
    cluster: ArticleCluster,
    medoid: _CandidateRecord | None,
) -> str:
    if medoid is None:
        return f"주요 사건 {cluster.rank}"
    data = medoid.row.get("data")
    if isinstance(data, Mapping):
        keywords = data.get("keywords")
        if isinstance(keywords, Sequence) and not isinstance(keywords, (str, bytes)):
            topic = " · ".join(str(value).strip() for value in keywords[:2] if str(value).strip())
            if topic:
                return topic[:100]
    return _headline(medoid.row)[:100]


def _headline(row: Mapping[str, Any]) -> str:
    data = row.get("data")
    if isinstance(data, Mapping):
        headline = data.get("headline")
        if isinstance(headline, str) and headline.strip():
            return " ".join(headline.split())[:200]
    return " ".join(str(row.get("source_title") or "주요 사건").split())[:200]


def _timeline_candidate(
    article_id: str,
    *,
    relevance: float,
    cluster: ArticleCluster,
    records: Mapping[str, _CandidateRecord],
) -> TimelineCandidate:
    record = records[article_id]
    ranks = {member.article_id: member.centrality_rank for member in cluster.members}
    rank = ranks.get(article_id, len(cluster.members) + 1)
    centrality = max(0.0, 1.0 - (rank - 1) / max(1, len(cluster.members)))
    date_source = str(record.row.get("date_source") or "provider")
    published_at = record.vector.published_at
    return TimelineCandidate(
        article_id=article_id,
        source_domain=record.vector.domain,
        relevance_score=relevance,
        centrality=centrality,
        published_at=published_at if date_source == "provider" else None,
        first_seen_at=published_at if date_source == "first_seen" else None,
        source_hash=record.vector.source_hash,
    )


def _timeline_edges(directed: Sequence[Any]) -> tuple[TimelineSimilarityEdge, ...]:
    pairs: dict[tuple[str, str], float] = {}
    for edge in directed:
        key = tuple(sorted((edge.source_article_id, edge.target_article_id)))
        pairs[key] = max(pairs.get(key, 0.0), float(edge.similarity))
    return tuple(
        TimelineSimilarityEdge(left, right, score)
        for (left, right), score in sorted(pairs.items())
    )


def _previous_plans(catalog: Mapping[str, Any] | None) -> tuple[TimelinePlan, ...]:
    if not catalog:
        return ()
    plans: list[TimelinePlan] = []
    try:
        for raw in catalog.get("timelines", []):
            nodes = tuple(
                TimelineNode(
                    node_id=node["node_id"],
                    article_id=node["article_id"],
                    lane=TimelineLane(node["lane"]),
                    anchor_node_id=node.get("anchor_node_id"),
                    display_order=node["display_order"],
                    update_kind=TimelineUpdateKind(node["update_kind"]),
                    relevance_score=float(node["relevance_score"]),
                    published_at=node["published_at_snapshot"],
                    date_source=TimelineDateSource(node["date_source"]),
                    source_domain=str(node.get("domain") or "unknown.invalid"),
                )
                for node in raw["nodes"]
            )
            plans.append(
                TimelinePlan(
                    stable_key=raw["stable_key"],
                    cluster_key=raw.get("cluster_key", raw["stable_key"]),
                    topic=raw["topic"],
                    title=raw["title"],
                    summary=raw["summary"],
                    started_at=raw["started_at"],
                    ended_at=raw["ended_at"],
                    nodes=nodes,
                    member_article_ids=tuple(node.article_id for node in nodes),
                    source_domains=tuple(sorted({node.source_domain for node in nodes})),
                )
            )
    except (KeyError, TypeError, ValueError):
        return ()
    return tuple(plans)


def _publication_payload(catalog_id: UUID, plans: Sequence[TimelinePlan]) -> list[dict]:
    return [
        {
            "id": uuid5(catalog_id, plan.stable_key),
            "stable_key": plan.stable_key,
            "cluster_key": plan.cluster_key,
            "rank": rank,
            "title": plan.title,
            "summary": plan.summary,
            "topic": plan.topic,
            "started_at": plan.started_at,
            "ended_at": plan.ended_at,
            "nodes": [
                {
                    "node_id": node.node_id,
                    "article_id": node.article_id,
                    "anchor_node_id": node.anchor_node_id,
                    "lane": node.lane.value,
                    "display_order": node.display_order,
                    "update_kind": node.update_kind.value,
                    "relevance_score": node.relevance_score,
                    "published_at": node.published_at,
                    "date_source": node.date_source.value,
                }
                for node in plan.nodes
            ],
        }
        for rank, plan in enumerate(plans, start=1)
    ]


__all__ = [
    "ExploreTimelineWorker",
    "candidate_refresh_slot",
    "cluster_refresh_slot",
    "manual_candidate_slot",
]
