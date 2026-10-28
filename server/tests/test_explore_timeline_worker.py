from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from app.article_clustering import (
    ArticleCluster,
    ArticleClusterMember,
    ArticleVectorEdge,
)
from app.article_vector_store import ArticleClusterSnapshot
from app.config import Settings
from app.openrouter_rerank import RerankResult
from app.timeline_copy import TimelineCopy


NOW = datetime(2026, 8, 2, 12, 30, tzinfo=UTC)


def _settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "OPENROUTER_API_KEY": "test-key",
        "VECTOR_DATABASE_URL": "postgresql://vector.internal/articles",
    }
    values.update(overrides)
    return Settings(**values)


def _map(article_id: str) -> dict:
    return {
        "format_version": "3",
        "headline": f"{article_id} 핵심 사건",
        "subheadline": "시간 흐름을 설명하는 기사",
        "summary": "사건의 변화와 공개된 근거를 설명하는 요약입니다.",
        "keywords": ["핵심 사건", "변화"],
        "scenes": [{"title": "배경", "summary": "사건의 배경 설명"}],
        "editorial": {"critic_verdict": "pass"},
    }


def _candidate_rows(cluster_count: int = 4, articles_per_cluster: int = 4) -> list[dict]:
    rows = []
    for cluster_rank in range(1, cluster_count + 1):
        for index in range(1, articles_per_cluster + 1):
            article_id = f"cluster-{cluster_rank}-article-{index}"
            rows.append(
                {
                    "article_id": article_id,
                    "source_title": f"클러스터 {cluster_rank} 기사 {index}",
                    "domain": f"source-{index % 2}.example",
                    "published_at": NOW - timedelta(days=index),
                    "date_source": "provider",
                    "data": _map(article_id),
                }
            )
    return rows


def _snapshot(cluster_count: int = 4, articles_per_cluster: int = 4) -> ArticleClusterSnapshot:
    clusters = []
    for rank in range(1, cluster_count + 1):
        members = tuple(
            ArticleClusterMember(
                article_id=f"cluster-{rank}-article-{index}",
                similarity_to_medoid=1.0 if index == 1 else 0.9,
                centrality_rank=index,
            )
            for index in range(1, articles_per_cluster + 1)
        )
        clusters.append(
            ArticleCluster(
                cluster_key=hashlib.sha256(f"cluster-{rank}".encode()).hexdigest(),
                rank=rank,
                medoid_article_id=members[0].article_id,
                members=members,
                domain_count=2,
                first_published_at=NOW - timedelta(days=4),
                last_published_at=NOW,
                mean_edge_similarity=0.9,
                major_score=0.9 - rank / 100,
            )
        )
    return ArticleClusterSnapshot(
        id=UUID("00000000-0000-4000-8000-000000000001"),
        input_hash="a" * 64,
        slot="2026-08-02T12:00:00Z",
        status="published",
        model="google/gemini-embedding-2",
        dimensions=768,
        algorithm_version="mutual-knn-medoid-v1",
        article_count=cluster_count * articles_per_cluster,
        indexed_count=cluster_count * articles_per_cluster,
        coverage=1.0,
        generated_at=NOW,
        valid_until=NOW + timedelta(hours=6),
        clusters=tuple(clusters),
    )


class FakeClusterWorker:
    def __init__(self):
        self.calls = 0
        self.forced = []

    async def run_once(self, *, force: bool = False):
        self.calls += 1
        self.forced.append(force)
        return {"kind": "published" if force else "not_due"}


class FakeVectorStore:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.graph_calls = []
        self.similarity_calls = []

    async def current_cluster_snapshot(self):
        return self.snapshot

    async def indexed_article_ids(self, candidates):
        return frozenset(item.article_id for item in candidates)

    async def article_knn_graph(self, candidates, *, neighbors, min_similarity):
        self.graph_calls.append((tuple(item.article_id for item in candidates), neighbors, min_similarity))
        return [
            ArticleVectorEdge(left.article_id, right.article_id, 0.9)
            for left in candidates
            for right in candidates
            if left.article_id != right.article_id
        ]

    async def article_similarity_graph(self, candidates, *, min_similarity):
        self.similarity_calls.append((tuple(item.article_id for item in candidates), min_similarity))
        return [
            ArticleVectorEdge(left.article_id, right.article_id, 0.5)
            for left in candidates
            for right in candidates
            if left.article_id != right.article_id
        ]


class FakeRepo:
    def __init__(self, rows, *, claim=True):
        self.rows = rows
        self.claim = claim
        self.claim_calls = []
        self.publish_calls = []
        self.retry_calls = []

    async def timeline_cluster_candidates(self, **kwargs):
        return self.rows

    async def current_explore_timeline_catalog(self, **kwargs):
        return None

    async def claim_explore_timeline_catalog(self, **kwargs):
        self.claim_calls.append(kwargs)
        return {"id": UUID("00000000-0000-4000-8000-000000000099")} if self.claim else None

    async def publish_explore_timeline_catalog(self, catalog_id, **kwargs):
        self.publish_calls.append((catalog_id, kwargs))
        return {"id": catalog_id, "status": "published"}

    async def retry_explore_timeline_catalog(self, catalog_id, **kwargs):
        self.retry_calls.append((catalog_id, kwargs))
        return "queued"


class FakeRerank:
    configured = True

    def __init__(self):
        self.calls = []

    async def rerank(self, *, query, documents, top_n):
        self.calls.append((query, documents, top_n))
        return [
            RerankResult(document.article_id, index, 1.0 - index / 100)
            for index, document in enumerate(documents[:top_n])
        ]


class FakeCopy:
    def __init__(self):
        self.calls = []

    async def polish(self, plans, previous_copy=None):
        self.calls.append((plans, previous_copy))
        return {
            plan.stable_key: TimelineCopy(
                title=f"주요 사건 타임라인 {index}",
                summary="발행 시각과 관련성을 기준으로 엄선한 주요 사건의 흐름입니다.",
            )
            for index, plan in enumerate(plans, start=1)
        }


@pytest.mark.asyncio
async def test_timeline_worker_claims_before_any_paid_provider_call():
    from app.explore_timeline_worker import ExploreTimelineWorker

    repo = FakeRepo(_candidate_rows(), claim=False)
    rerank = FakeRerank()
    copy = FakeCopy()
    worker = ExploreTimelineWorker(
        _settings(),
        repo,
        FakeVectorStore(_snapshot()),
        FakeClusterWorker(),
        rerank,
        copy,
        clock=lambda: NOW,
    )

    result = await worker.run_once()

    assert result["kind"] == "not_due"
    assert len(repo.claim_calls) == 1
    assert rerank.calls == []
    assert copy.calls == []


@pytest.mark.asyncio
async def test_timeline_worker_publishes_four_deterministic_trees_and_one_copy_batch():
    from app.explore_timeline_worker import ExploreTimelineWorker

    repo = FakeRepo(_candidate_rows())
    rerank = FakeRerank()
    copy = FakeCopy()
    vector = FakeVectorStore(_snapshot())
    worker = ExploreTimelineWorker(
        _settings(),
        repo,
        vector,
        FakeClusterWorker(),
        rerank,
        copy,
        clock=lambda: NOW,
    )

    result = await worker.run_once()

    assert result == {"kind": "published", "candidates": 16, "timelines": 4, "published": 1}
    assert len(rerank.calls) == 4
    assert len(copy.calls) == 1
    assert len(repo.publish_calls) == 1
    published = repo.publish_calls[0][1]["timelines"]
    assert len(published) == 4
    assert all(len(timeline["nodes"]) == 4 for timeline in published)
    assert all(
        [node["display_order"] for node in timeline["nodes"]] == [1, 2, 3, 4]
        for timeline in published
    )
    assert [node["article_id"] for node in published[0]["nodes"]] == [
        "cluster-1-article-4",
        "cluster-1-article-3",
        "cluster-1-article-2",
        "cluster-1-article-1",
    ]


@pytest.mark.asyncio
async def test_timeline_worker_reranks_the_full_twenty_article_window_for_backfill():
    from app.explore_timeline_worker import ExploreTimelineWorker

    repo = FakeRepo(_candidate_rows(articles_per_cluster=20))
    rerank = FakeRerank()
    vector = FakeVectorStore(_snapshot(articles_per_cluster=20))
    worker = ExploreTimelineWorker(
        _settings(),
        repo,
        vector,
        FakeClusterWorker(),
        rerank,
        FakeCopy(),
        clock=lambda: NOW,
    )

    result = await worker.run_once()

    assert result["kind"] == "published"
    assert [top_n for _query, _documents, top_n in rerank.calls] == [20, 20, 20, 20]
    assert [len(candidates) for candidates, _minimum in vector.similarity_calls] == [20, 20, 20, 20]
    assert {minimum for _candidates, minimum in vector.similarity_calls} == {0.0}


@pytest.mark.asyncio
async def test_timeline_worker_preserves_current_when_only_three_major_clusters():
    from app.explore_timeline_worker import ExploreTimelineWorker

    repo = FakeRepo(_candidate_rows(cluster_count=3))
    rerank = FakeRerank()
    worker = ExploreTimelineWorker(
        _settings(),
        repo,
        FakeVectorStore(_snapshot(cluster_count=3)),
        FakeClusterWorker(),
        rerank,
        FakeCopy(),
        clock=lambda: NOW,
    )

    result = await worker.run_once()

    assert result["kind"] == "insufficient_clusters"
    assert repo.claim_calls == []
    assert repo.publish_calls == []
    assert rerank.calls == []


def test_six_and_four_hour_slots_cover_required_daily_cadence():
    from app.explore_timeline_worker import candidate_refresh_slot, cluster_refresh_slot

    cluster_slots = {
        cluster_refresh_slot(datetime(2026, 8, 2, hour, tzinfo=UTC))
        for hour in range(24)
    }
    candidate_slots = {
        candidate_refresh_slot(datetime(2026, 8, 2, hour, tzinfo=UTC))
        for hour in range(24)
    }

    assert len(cluster_slots) == 4
    assert len(candidate_slots) == 6


class FakeClusterVector:
    def __init__(self, indexed_count: int, *, reuse_existing: bool = False):
        self.indexed_count = indexed_count
        self.reuse_existing = reuse_existing
        self.publish_calls = []

    async def current_cluster_snapshot(self):
        return None

    async def indexed_article_ids(self, candidates):
        return frozenset(item.article_id for item in candidates[: self.indexed_count])

    async def article_knn_graph(self, candidates, *, neighbors, min_similarity):
        return [
            ArticleVectorEdge(left.article_id, right.article_id, 0.9)
            for left in candidates
            for right in candidates
            if left.article_id != right.article_id
        ]

    async def publish_cluster_snapshot(self, plan, *, input_hash, slot):
        self.publish_calls.append((plan, input_hash, slot))
        if self.reuse_existing:
            return _snapshot(cluster_count=4)
        # A successful publish echoes back the identity it was asked for; a
        # different one means the slot was already taken and the store reused it.
        return replace(_snapshot(cluster_count=4), input_hash=input_hash, slot=slot)


@pytest.mark.asyncio
async def test_cluster_worker_fails_closed_below_exact_eighty_percent_coverage():
    from app.article_cluster_worker import ArticleClusterWorker

    rows = _candidate_rows()
    vector = FakeClusterVector(indexed_count=12)
    worker = ArticleClusterWorker(
        _settings(),
        FakeRepo(rows),
        vector,
        clock=lambda: NOW,
    )

    result = await worker.run_once()

    assert result["kind"] == "insufficient_coverage"
    assert result["indexed"] == 12
    assert result["candidates"] == 16
    assert vector.publish_calls == []


@pytest.mark.asyncio
async def test_cluster_worker_publishes_at_exact_eighty_percent_coverage():
    from app.article_cluster_worker import ArticleClusterWorker

    rows = _candidate_rows()
    vector = FakeClusterVector(indexed_count=13)
    worker = ArticleClusterWorker(
        _settings(),
        FakeRepo(rows),
        vector,
        clock=lambda: NOW,
    )

    result = await worker.run_once()

    assert result["kind"] == "published"
    assert result["indexed"] == 13
    assert len(vector.publish_calls) == 1
    plan = vector.publish_calls[0][0]
    assert plan.article_count == 16
    assert plan.indexed_count == 13
    assert plan.coverage == 0.8125


@pytest.mark.asyncio
async def test_timeline_worker_records_each_run_for_the_ops_panel():
    from app.explore_timeline_worker import ExploreTimelineWorker

    repo = FakeRepo(_candidate_rows())
    worker = ExploreTimelineWorker(
        _settings(),
        repo,
        FakeVectorStore(_snapshot()),
        FakeClusterWorker(),
        FakeRerank(),
        FakeCopy(),
        clock=lambda: NOW,
    )

    assert worker.status["last_run"] is None
    assert worker.status["running"] is False

    result = await worker.run_once()

    status = worker.status
    assert result["kind"] == "published"
    assert status["last_run"] == {**result, "forced": False}
    assert status["last_run_at"] == NOW
    assert status["last_error_code"] is None
    assert status["last_cluster_run"] == {"kind": "not_due"}
    assert status["enabled"] is True
    assert status["rerank_configured"] is True
    assert status["candidate_day_span"] == 14


@pytest.mark.asyncio
async def test_timeline_worker_records_a_quiet_shortfall_as_its_own_code():
    from app.explore_timeline_worker import ExploreTimelineWorker

    worker = ExploreTimelineWorker(
        _settings(),
        FakeRepo(_candidate_rows(cluster_count=3)),
        FakeVectorStore(_snapshot(cluster_count=3)),
        FakeClusterWorker(),
        FakeRerank(),
        FakeCopy(),
        clock=lambda: NOW,
    )

    result = await worker.run_once()

    # This outcome publishes nothing and previously left no trace anywhere.
    assert result["published"] == 0
    assert worker.status["last_run"]["kind"] == result["kind"]
    assert worker.status["last_error_code"] == result["kind"]


@pytest.mark.asyncio
async def test_forced_refresh_takes_a_slot_the_schedule_never_emits():
    from app.explore_timeline_worker import ExploreTimelineWorker, candidate_refresh_slot

    repo = FakeRepo(_candidate_rows())
    cluster_worker = FakeClusterWorker()
    worker = ExploreTimelineWorker(
        _settings(),
        repo,
        FakeVectorStore(_snapshot()),
        cluster_worker,
        FakeRerank(),
        FakeCopy(),
        clock=lambda: NOW,
    )

    result = await worker.run_once(force=True)

    assert result["kind"] == "published"
    assert cluster_worker.forced == [True]
    claimed_slot = repo.claim_calls[0]["candidate_slot"]
    # The scheduled four-hour slot stays free, so the stored edition that owns
    # it is neither overwritten nor deleted.
    assert claimed_slot != candidate_refresh_slot(NOW)
    assert claimed_slot == "manual:2026-08-02T12:30Z"
    assert worker.status["last_run"]["forced"] is True


@pytest.mark.asyncio
async def test_scheduled_refresh_keeps_using_the_four_hour_slot():
    from app.explore_timeline_worker import ExploreTimelineWorker, candidate_refresh_slot

    repo = FakeRepo(_candidate_rows())
    cluster_worker = FakeClusterWorker()
    worker = ExploreTimelineWorker(
        _settings(),
        repo,
        FakeVectorStore(_snapshot()),
        cluster_worker,
        FakeRerank(),
        FakeCopy(),
        clock=lambda: NOW,
    )

    await worker.run_once()

    assert cluster_worker.forced == [False]
    assert repo.claim_calls[0]["candidate_slot"] == candidate_refresh_slot(NOW)


@pytest.mark.asyncio
async def test_forced_cluster_run_publishes_beside_the_taken_slot():
    from app.article_cluster_worker import (
        ArticleClusterWorker,
        cluster_refresh_slot,
        manual_cluster_slot,
    )

    vector = FakeClusterVector(indexed_count=16)
    worker = ArticleClusterWorker(
        _settings(),
        FakeRepo(_candidate_rows()),
        vector,
        clock=lambda: NOW,
    )

    scheduled = await worker.run_once()
    forced = await worker.run_once(force=True)

    scheduled_hash, scheduled_slot = vector.publish_calls[0][1:]
    forced_hash, forced_slot = vector.publish_calls[1][1:]
    assert scheduled_slot == cluster_refresh_slot(NOW)
    assert forced_slot == manual_cluster_slot(NOW)
    assert forced_slot != scheduled_slot
    # Both keys are unique, so an unchanged pool must still yield a new identity.
    assert forced_hash != scheduled_hash
    assert scheduled["kind"] == "published"
    assert forced["kind"] == "published"


@pytest.mark.asyncio
async def test_cluster_run_reports_reuse_instead_of_claiming_a_publish():
    from app.article_cluster_worker import ArticleClusterWorker

    vector = FakeClusterVector(indexed_count=16, reuse_existing=True)
    worker = ArticleClusterWorker(
        _settings(),
        FakeRepo(_candidate_rows()),
        vector,
        clock=lambda: NOW,
    )

    result = await worker.run_once()

    # The slot was already taken, so /ops must not be told a new snapshot landed.
    assert result["kind"] == "reused"
    assert result["algorithm_version"] == "mutual-knn-medoid-v1"
