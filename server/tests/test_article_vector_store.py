from __future__ import annotations

import math
import re
import hashlib
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.article_clustering import (
    ArticleVectorCandidate,
    ArticleVectorEdge,
    build_article_clusters,
)
from app.article_vector_store import (
    ARTICLE_VECTOR_SCHEMA_SQL,
    ArticleEmbeddingCursor,
    ArticleEmbeddingProgress,
    ArticleEmbeddingTarget,
    ArticleVectorStore,
    ArticleVectorWorkItem,
    _ENQUEUE_SQL,
)
from app.config import Settings
from app.vector_store import PGVECTOR_VERSION, VectorStoreError


NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def test_c10007_enqueue_explicitly_types_nullable_error_code():
    assert "$7::text" in _ENQUEUE_SQL
    assert re.search(r"\$7(?!::text)", _ENQUEUE_SQL) is None


def _settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "VECTOR_DATABASE_URL": "postgresql://vector.internal/articles",
    }
    values.update(overrides)
    return Settings(**values)


class FakeTransaction:
    def __init__(self, events: list[object]):
        self.events = events

    async def __aenter__(self):
        self.events.append("transaction_enter")
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.events.append(("transaction_exit", exc_type))
        return False


class FakeAcquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeConnection:
    def __init__(
        self,
        *,
        fetchrow_result=None,
        fetchrow_results=None,
        fetch_rows=None,
        fail_executemany_at: int | None = None,
    ):
        self.fetchrow_result = fetchrow_result
        self.fetchrow_results = list(fetchrow_results or [])
        self.fetch_rows = list(fetch_rows or [])
        self.fail_executemany_at = fail_executemany_at
        self.events: list[object] = []
        self.executemany_calls: list[tuple[str, list[tuple]]] = []
        self.execute_calls: list[tuple[str, tuple]] = []
        self.fetchrow_calls: list[tuple[str, tuple]] = []
        self.fetch_calls: list[tuple[str, tuple]] = []

    def transaction(self):
        return FakeTransaction(self.events)

    async def executemany(self, query, values):
        copied = list(values)
        self.executemany_calls.append((query, copied))
        self.events.append(("executemany", len(copied)))
        if self.fail_executemany_at == len(self.executemany_calls):
            raise RuntimeError("private vector write")

    async def execute(self, query, *args):
        self.execute_calls.append((query, args))
        self.events.append(("execute", query, args))
        return "OK"

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        self.events.append(("fetchrow", query, args))
        if self.fetchrow_results:
            return self.fetchrow_results.pop(0)
        return self.fetchrow_result

    async def fetch(self, query, *args):
        self.fetch_calls.append((query, args))
        self.events.append(("fetch", query, args))
        return self.fetch_rows


class FakePool:
    def __init__(
        self,
        *,
        version: str = PGVECTOR_VERSION,
        rows=None,
        row=None,
        connection: FakeConnection | None = None,
        fail_execute: bool = False,
    ):
        self.version = version
        self.rows = list(rows or [])
        self.row = row
        self.connection = connection or FakeConnection()
        self.fail_execute = fail_execute
        self.calls: list[tuple[str, str, tuple]] = []
        self.closed = False

    def acquire(self):
        return FakeAcquire(self.connection)

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        if self.fail_execute:
            raise RuntimeError("private vector dsn")
        return "OK"

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", query, args))
        return self.version

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        return self.rows

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        return self.row

    async def close(self):
        self.closed = True


def test_c10007_vector_schema_is_separate_bounded_and_hnsw_filtered():
    sql = " ".join(ARTICLE_VECTOR_SCHEMA_SQL.split())

    assert "CREATE TABLE IF NOT EXISTS article_text_embeddings" in sql
    assert "CREATE TABLE IF NOT EXISTS article_embedding_cursors" in sql
    assert "CREATE TABLE IF NOT EXISTS article_embedding_progress_history" in sql
    assert "vector(768)" in sql
    assert "char_length(source_text) <= 12000" in sql
    assert "'queued','processing','retry','indexed','dead','inactive'" in sql
    assert "USING hnsw (embedding vector_cosine_ops)" in sql
    assert "model = 'google/gemini-embedding-2'" in sql
    assert "status = 'indexed'" in sql
    assert "idx_article_embedding_progress_recent" in sql
    assert "(model, dimensions, id DESC)" in sql
    assert "CREATE TABLE IF NOT EXISTS article_cluster_snapshots" in sql
    assert "CREATE TABLE IF NOT EXISTS article_clusters" in sql
    assert "CREATE TABLE IF NOT EXISTS article_cluster_members" in sql
    assert "article_count BETWEEN 0 AND 500" in sql
    assert "rank BETWEEN 1 AND 8" in sql
    assert "status='processing'" in sql
    assert "REFERENCES articles" not in sql


@pytest.mark.asyncio
async def test_c10007_start_requires_exact_pgvector_and_builds_schema():
    pool = FakePool()
    store = ArticleVectorStore(_settings(), pool=pool)

    assert await store.start() is True
    assert store.ready is True
    assert [call[0] for call in pool.calls] == ["execute", "fetchval", "execute"]

    bad = ArticleVectorStore(_settings(), pool=FakePool(version="0.8.1"))
    assert await bad.start() is False
    assert bad.ready is False


@pytest.mark.asyncio
async def test_c10007_hash_change_requeues_and_inactive_clears_vector():
    connection = FakeConnection()
    pool = FakePool(connection=connection)
    store = ArticleVectorStore(_settings(), pool=pool)
    cursor = ArticleEmbeddingCursor(NOW, "article-c")
    targets = [
        ArticleEmbeddingTarget("article-a", True, "a" * 64, "source a"),
        ArticleEmbeddingTarget("article-b", False),
        ArticleEmbeddingTarget(
            "article-c", True, error_code="article_embedding_source_empty"
        ),
    ]

    assert await store.enqueue_changes(targets, cursor) == 3

    assert connection.events[0] == "transaction_enter"
    assert connection.events[-1] == ("transaction_exit", None)
    query, values = connection.executemany_calls[0]
    normalized = " ".join(query.split())
    assert "source_hash IS DISTINCT FROM EXCLUDED.source_hash" in normalized
    assert "target.status = 'inactive'" in normalized
    assert "THEN 'queued'" in normalized
    assert "THEN 'inactive'" in normalized
    assert "THEN 'dead'" in normalized
    assert "embedding = CASE" in normalized
    assert "attempts = CASE" in normalized
    assert "OR target.status = 'inactive' THEN NULL" in normalized
    assert "OR target.status = 'inactive' THEN 0" in normalized
    assert values[0][0:5] == (
        "article-a",
        "google/gemini-embedding-2",
        768,
        True,
        "a" * 64,
    )
    assert values[1][3:] == (False, None, None, None)
    assert values[2][-1] == "article_embedding_source_empty"

    cursor_query, cursor_args = connection.execute_calls[-1]
    cursor_sql = " ".join(cursor_query.split())
    assert "INSERT INTO article_embedding_cursors" in cursor_sql
    assert "EXCLUDED.last_changed_at, EXCLUDED.last_article_id" in cursor_sql
    assert "> (article_embedding_cursors.last_changed_at," in cursor_sql
    assert cursor_args == (
        "google/gemini-embedding-2",
        768,
        NOW,
        "article-c",
    )


@pytest.mark.asyncio
async def test_c10007_enqueue_rejects_invalid_active_source_before_transaction():
    connection = FakeConnection()
    store = ArticleVectorStore(_settings(), pool=FakePool(connection=connection))

    invalid = (
        ArticleEmbeddingTarget("article", True, "short", "source"),
        ArticleEmbeddingTarget("article", True, "a" * 64, ""),
        ArticleEmbeddingTarget("article", True, "a" * 64, "x" * 12001),
    )
    for target in invalid:
        with pytest.raises(ValueError):
            await store.enqueue_changes([target], ArticleEmbeddingCursor())

    with pytest.raises(ValueError, match="timezone-aware"):
        await store.enqueue_changes(
            [], ArticleEmbeddingCursor(datetime(2026, 8, 1), "article")
        )
    assert connection.events == []


@pytest.mark.asyncio
async def test_c10007_cursor_round_trip_defaults_and_decodes_row():
    assert ArticleEmbeddingCursor() == ArticleEmbeddingCursor(None, "")
    pool = FakePool(
        row={"last_changed_at": NOW, "last_article_id": "article-z"}
    )
    cursor = await ArticleVectorStore(_settings(), pool=pool).get_cursor()
    assert cursor == ArticleEmbeddingCursor(NOW, "article-z")


@pytest.mark.asyncio
async def test_c10007_claim_reclaims_stale_processing_with_skip_locked():
    pool = FakePool(
        rows=[
            {
                "article_id": "article-a",
                "source_hash": "a" * 64,
                "source_text": "source a",
                "attempts": 2,
            }
        ]
    )
    store = ArticleVectorStore(_settings(), pool=pool)

    work = await store.claim_due(500)

    assert work == [
        ArticleVectorWorkItem("article-a", "a" * 64, "source a", 2)
    ]
    _kind, query, args = pool.calls[-1]
    sql = " ".join(query.split())
    assert "status IN ('queued','retry')" in sql
    assert "status='processing'" in sql
    assert "interval '30 minutes'" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert args[-1] == 50


@pytest.mark.asyncio
async def test_c10007_claim_finalizes_exhausted_stale_processing():
    pool = FakePool()
    store = ArticleVectorStore(
        _settings(ARTICLE_EMBEDDING_MAX_ATTEMPTS=5),
        pool=pool,
    )

    assert await store.claim_due(50) == []

    _kind, query, args = pool.calls[-1]
    sql = " ".join(query.split())
    assert sql.startswith("WITH exhausted AS ( UPDATE article_text_embeddings")
    assert "status='processing' AND attempts >= $3" in sql
    assert "updated_at <= now() - interval '30 minutes'" in sql
    assert "SET status='dead', error_code='lease_expired'" in sql
    assert "WITH due AS" not in sql
    assert "), due AS (" in sql
    assert args[2] == 5


@pytest.mark.asyncio
async def test_c10008_mark_indexed_atomically_records_each_success():
    connection = FakeConnection(
        fetchrow_result={
            "embedded_articles": 2,
            "total_articles": 10,
            "coverage_percent": 20.0,
            "recorded_at": NOW,
        }
    )
    store = ArticleVectorStore(_settings(), pool=FakePool(connection=connection))

    progress = await store.mark_indexed(
        "article-a", "a" * 64, [0.25] * 768, total_articles=10
    )

    assert progress == ArticleEmbeddingProgress(2, 10, 20.0, NOW)
    assert connection.events[0] == "transaction_enter"
    assert connection.events[-1] == ("transaction_exit", None)
    assert len(connection.fetchrow_calls) == 1
    assert len(connection.execute_calls) == 1
    assert "pg_advisory_xact_lock" in connection.execute_calls[0][0]
    query, args = connection.fetchrow_calls[0]
    sql = " ".join(query.split())
    assert sql.startswith("WITH indexed_before AS")
    assert "UPDATE article_text_embeddings" in sql
    assert "source_hash=$4" in sql
    assert "status='processing'" in sql
    assert "INSERT INTO article_embedding_progress_history" in sql
    assert "FROM indexed CROSS JOIN indexed_before" in sql
    assert "RETURNING embedded_articles, total_articles" in sql
    assert args[0:4] == (
        "article-a",
        "google/gemini-embedding-2",
        768,
        "a" * 64,
    )
    assert args[5] == 10


@pytest.mark.asyncio
async def test_c10008_stale_hash_and_failures_do_not_record_progress():
    connection = FakeConnection(fetchrow_result=None)
    store = ArticleVectorStore(_settings(), pool=FakePool(connection=connection))

    assert (
        await store.mark_indexed(
            "article-a", "b" * 64, [0.0] * 768, total_articles=4
        )
        is None
    )
    query, _args = connection.fetchrow_calls[0]
    assert "FROM indexed CROSS JOIN indexed_before" in query

    before = len(connection.events)
    for bad_vector in ([0.0] * 767, [math.nan] * 768, [True] * 768):
        with pytest.raises(ValueError):
            await store.mark_indexed(
                "article-a", "a" * 64, bad_vector, total_articles=4
            )
    assert len(connection.events) == before


@pytest.mark.asyncio
async def test_c10008_progress_clamps_and_handles_zero_total():
    over = ArticleVectorStore(
        _settings(),
        pool=FakePool(row={"embedded_count": 12, "updated_at": NOW}),
    )
    zero = ArticleVectorStore(
        _settings(),
        pool=FakePool(row={"embedded_count": 12, "updated_at": NOW}),
    )

    assert await over.progress(10) == ArticleEmbeddingProgress(10, 10, 100.0, NOW)
    assert await zero.progress(0) == ArticleEmbeddingProgress(0, 0, 0.0, NOW)


@pytest.mark.asyncio
async def test_c10008_progress_rounds_half_up_like_history_sql():
    store = ArticleVectorStore(
        _settings(),
        pool=FakePool(row={"embedded_count": 1, "updated_at": NOW}),
    )

    assert await store.progress(80) == ArticleEmbeddingProgress(1, 80, 1.3, NOW)


@pytest.mark.asyncio
async def test_c10007_retry_and_dead_are_bounded_guarded_and_redacted():
    pool = FakePool()
    store = ArticleVectorStore(_settings(), pool=pool)

    await store.mark_retry(["article-b", "article-a", "article-a"], "Rate (429)!")
    await store.mark_dead(["article-a"], "Provider secret/body")

    retry_call, dead_call = pool.calls[-2:]
    retry_sql = " ".join(retry_call[1].split())
    dead_sql = " ".join(dead_call[1].split())
    assert "attempts >= $4 THEN 'dead' ELSE 'retry'" in retry_sql
    assert "interval '6 hours'" in retry_sql
    assert "status='processing'" in retry_sql
    assert retry_call[2][0] == ["article-a", "article-b"]
    assert retry_call[2][-1] == "rate_429"
    assert "status='dead'" in dead_sql
    assert "status='processing'" in dead_sql
    assert dead_call[2][-1] == "provider_secret_body"


@pytest.mark.asyncio
async def test_c10007_nearest_articles_applies_cosine_exclusions_and_limit():
    pool = FakePool(
        rows=[
            {"article_id": "article-b", "similarity": 0.8},
            {"article_id": "article-c", "similarity": 0.35},
        ]
    )
    store = ArticleVectorStore(_settings(), pool=pool)

    hits = await store.nearest_articles(
        [0.0] * 768,
        exclude_ids=["article-a", "article-a"],
        limit=100,
        min_similarity=0.35,
    )

    assert [(hit.article_id, hit.similarity) for hit in hits] == [
        ("article-b", 0.8),
        ("article-c", 0.35),
    ]
    _kind, query, args = pool.calls[-1]
    sql = " ".join(query.split())
    assert "embedding <=> $4::vector(768)" in sql
    assert "NOT (article_id = ANY($5::text[]))" in sql
    assert "ORDER BY embedding <=> $4::vector(768), article_id" in sql
    assert args[4] == ["article-a"]
    assert args[5] == 60


def _cluster_candidates_and_plan():
    candidates = [
        ArticleVectorCandidate(
            article_id,
            hashlib.sha256(f"source:{article_id}".encode()).hexdigest(),
            domain,
            NOW,
        )
        for article_id, domain in (
            ("article-a", "one.example"),
            ("article-b", "one.example"),
            ("article-c", "two.example"),
        )
    ]
    edges = [
        ArticleVectorEdge(source.article_id, target.article_id, 0.9)
        for source in candidates
        for target in candidates
        if source.article_id != target.article_id
    ]
    return candidates, build_article_clusters(candidates, edges)


@pytest.mark.asyncio
async def test_c12207_indexed_article_ids_require_exact_current_source_hash():
    candidates, _plan = _cluster_candidates_and_plan()
    pool = FakePool(rows=[{"article_id": "article-a"}, {"article_id": "article-c"}])
    store = ArticleVectorStore(_settings(), pool=pool)

    indexed = await store.indexed_article_ids(list(reversed(candidates)))

    assert indexed == frozenset({"article-a", "article-c"})
    _kind, query, args = pool.calls[-1]
    sql = " ".join(query.split())
    assert "unnest($1::text[],$2::text[])" in sql
    assert "requested.source_hash=stored.source_hash" in sql
    assert "stored.status='indexed'" in sql
    assert "stored.embedding IS NOT NULL" in sql
    assert args[0] == ["article-a", "article-b", "article-c"]

    with pytest.raises(ValueError, match="unique"):
        await store.indexed_article_ids([candidates[0], candidates[0]])


@pytest.mark.asyncio
async def test_c12200_article_knn_graph_is_bounded_and_requires_exact_source_hash():
    candidates, _plan = _cluster_candidates_and_plan()
    pool = FakePool(
        rows=[
            {
                "source_article_id": "article-b",
                "target_article_id": "article-c",
                "similarity": 0.80,
            },
            {
                "source_article_id": "article-a",
                "target_article_id": "article-b",
                "similarity": 0.90,
            },
        ]
    )
    store = ArticleVectorStore(_settings(), pool=pool)

    edges = await store.article_knn_graph(
        list(reversed(candidates)),
        neighbors=8,
        min_similarity=0.72,
    )

    assert [(edge.source_article_id, edge.target_article_id) for edge in edges] == [
        ("article-a", "article-b"),
        ("article-b", "article-c"),
    ]
    _kind, query, args = pool.calls[-1]
    sql = " ".join(query.split())
    assert "unnest($1::text[], $2::text[])" in sql
    assert "requested.source_hash=stored.source_hash" in sql
    assert "stored.status='indexed'" in sql
    assert "target.embedding <=> seed.embedding" in sql
    assert "LIMIT $6" in sql
    assert args[0] == ["article-a", "article-b", "article-c"]
    assert args[1] == [
        next(item.source_hash for item in candidates if item.article_id == article_id)
        for article_id in args[0]
    ]
    assert args[4:] == (0.72, 8)

    with pytest.raises(ValueError, match="kNN"):
        await store.article_knn_graph(candidates, neighbors=9, min_similarity=0.72)
    with pytest.raises(ValueError, match="unique"):
        await store.article_knn_graph(
            [candidates[0], candidates[0]],
            neighbors=1,
            min_similarity=0.72,
        )


@pytest.mark.asyncio
async def test_c12200_article_knn_graph_rejects_per_source_overflow():
    candidates, _plan = _cluster_candidates_and_plan()
    pool = FakePool(
        rows=[
            {
                "source_article_id": "article-a",
                "target_article_id": "article-b",
                "similarity": 0.90,
            },
            {
                "source_article_id": "article-a",
                "target_article_id": "article-c",
                "similarity": 0.80,
            },
        ]
    )
    store = ArticleVectorStore(_settings(), pool=pool)

    with pytest.raises(VectorStoreError, match="invalid_article_knn_result"):
        await store.article_knn_graph(
            candidates,
            neighbors=1,
            min_similarity=0.72,
        )


@pytest.mark.asyncio
async def test_timeline_similarity_graph_returns_full_bounded_pairwise_edges():
    candidates, _plan = _cluster_candidates_and_plan()
    pool = FakePool(
        rows=[
            {
                "source_article_id": "article-c",
                "target_article_id": "article-a",
                "similarity": 0.92,
            },
            {
                "source_article_id": "article-a",
                "target_article_id": "article-c",
                "similarity": 0.92,
            },
        ]
    )
    store = ArticleVectorStore(_settings(), pool=pool)

    edges = await store.article_similarity_graph(candidates, min_similarity=0.92)

    assert [(edge.source_article_id, edge.target_article_id) for edge in edges] == [
        ("article-a", "article-c"),
        ("article-c", "article-a"),
    ]
    _kind, query, args = pool.calls[-1]
    sql = " ".join(query.split())
    assert "JOIN eligible AS target ON target.article_id<>seed.article_id" in sql
    assert "LIMIT" not in sql
    assert args[0] == ["article-a", "article-b", "article-c"]
    assert args[4:] == (0.92,)

    overflow = [
        ArticleVectorCandidate(
            article_id=f"article-{index}",
            source_hash=f"{index:064x}",
            domain="one.example",
            published_at=NOW,
        )
        for index in range(21)
    ]
    with pytest.raises(ValueError, match="exceeds 20"):
        await store.article_similarity_graph(overflow, min_similarity=0.92)


def _snapshot_header(*, status: str, input_hash: str = "f" * 64):
    return {
        "id": UUID("11111111-1111-1111-1111-111111111111"),
        "input_hash": input_hash,
        "slot": "2026-08-02T12:00:00Z",
        "status": status,
        "model": "google/gemini-embedding-2",
        "dimensions": 768,
        "algorithm_version": "mutual-knn-medoid-v1",
        "article_count": 3,
        "indexed_count": 3,
        "coverage": 1.0,
        "generated_at": NOW if status == "published" else None,
        "valid_until": datetime(2026, 8, 2, 18, tzinfo=UTC),
    }


def _snapshot_member_rows(plan):
    return [
        {
            "cluster_key": cluster.cluster_key,
            "rank": cluster.rank,
            "medoid_article_id": cluster.medoid_article_id,
            "member_count": cluster.member_count,
            "domain_count": cluster.domain_count,
            "first_published_at": cluster.first_published_at,
            "last_published_at": cluster.last_published_at,
            "mean_edge_similarity": cluster.mean_edge_similarity,
            "major_score": cluster.major_score,
            "article_id": member.article_id,
            "similarity_to_medoid": member.similarity_to_medoid,
            "centrality_rank": member.centrality_rank,
        }
        for cluster in plan.clusters
        for member in cluster.members
    ]


@pytest.mark.asyncio
async def test_c12200_cluster_snapshot_publish_is_atomic_and_single_per_slot():
    _candidates, plan = _cluster_candidates_and_plan()
    processing = _snapshot_header(status="processing")
    published = _snapshot_header(status="published")
    connection = FakeConnection(fetchrow_results=[None, processing, published])
    store = ArticleVectorStore(_settings(), pool=FakePool(connection=connection))

    snapshot = await store.publish_cluster_snapshot(
        plan,
        input_hash="f" * 64,
        slot="2026-08-02T12:00:00Z",
    )

    assert snapshot.status == "published"
    assert snapshot.clusters == plan.clusters
    assert connection.events[0] == "transaction_enter"
    assert connection.events[-1] == ("transaction_exit", None)
    assert len(connection.executemany_calls) == 2
    assert len(connection.executemany_calls[0][1]) == 1
    assert len(connection.executemany_calls[1][1]) == 3
    assert any("status='superseded'" in query for query, _args in connection.execute_calls)
    assert "status='published'" in connection.fetchrow_calls[-1][0]

    existing_connection = FakeConnection(
        fetchrow_results=[published],
        fetch_rows=_snapshot_member_rows(plan),
    )
    existing_store = ArticleVectorStore(
        _settings(),
        pool=FakePool(connection=existing_connection),
    )
    same = await existing_store.publish_cluster_snapshot(
        plan,
        input_hash="f" * 64,
        slot="2026-08-02T12:00:00Z",
    )
    assert same == snapshot
    assert existing_connection.executemany_calls == []
    assert len(existing_connection.fetchrow_calls) == 1


@pytest.mark.asyncio
async def test_c12200_cluster_snapshot_rolls_back_before_superseding_on_member_failure():
    _candidates, plan = _cluster_candidates_and_plan()
    connection = FakeConnection(
        fetchrow_results=[None, _snapshot_header(status="processing")],
        fail_executemany_at=2,
    )
    store = ArticleVectorStore(_settings(), pool=FakePool(connection=connection))

    with pytest.raises(RuntimeError, match="private vector write"):
        await store.publish_cluster_snapshot(
            plan,
            input_hash="f" * 64,
            slot="2026-08-02T12:00:00Z",
        )

    assert connection.events[-1] == ("transaction_exit", RuntimeError)
    assert not any(
        "status='superseded'" in query for query, _args in connection.execute_calls
    )


@pytest.mark.asyncio
async def test_c12200_current_cluster_snapshot_hydrates_stable_member_order():
    _candidates, plan = _cluster_candidates_and_plan()
    header = _snapshot_header(status="published")
    pool = FakePool(row=header, rows=_snapshot_member_rows(plan))
    store = ArticleVectorStore(_settings(), pool=pool)

    snapshot = await store.current_cluster_snapshot()

    assert snapshot is not None
    assert snapshot.status == "published"
    assert snapshot.clusters == plan.clusters
    assert "valid_until > now()" in pool.calls[0][1]


@pytest.mark.asyncio
async def test_c12200_snapshot_rejects_invalid_hash_and_non_six_hour_slot_before_db():
    _candidates, plan = _cluster_candidates_and_plan()
    connection = FakeConnection()
    store = ArticleVectorStore(_settings(), pool=FakePool(connection=connection))

    with pytest.raises(ValueError, match="input_hash"):
        await store.publish_cluster_snapshot(
            plan,
            input_hash="short",
            slot="2026-08-02T12:00:00Z",
        )
    # Scheduled slots stay pinned to the six-hour boundaries; only the
    # `manual:` namespace the /ops refresh uses may carry another time.
    for slot in (
        "2026-08-02T14:00:00Z",
        "2026-08-02T12:30:00Z",
        "manual:2026-08-02T14:00:00Z",
        "manual:2026-08-02T24:00Z",
        "2026-08-02T14:30Z",
    ):
        with pytest.raises(ValueError, match="scheduled or manual"):
            await store.publish_cluster_snapshot(
                plan,
                input_hash="f" * 64,
                slot=slot,
            )
    assert connection.events == []
