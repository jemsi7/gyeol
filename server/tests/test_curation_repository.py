from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.db import SCHEMA_SQL
from app.repository import Repository


CATALOG_ID = UUID("00000000-0000-4000-8000-0000000000c1")


class RecordingPool:
    def __init__(self, rows=None, row=None):
        self.rows = rows or []
        self.row = row
        self.calls = []

    async def fetch(self, query, *args):
        self.calls.append((query, args))
        return self.rows

    async def fetchrow(self, query, *args):
        self.calls.append((query, args))
        return self.row


def _repo(pool):
    return Repository(SimpleNamespace(pool=pool))


def test_schema_has_catalog_route_item_cooldown_and_worker_contracts():
    for value in (
        "CREATE TABLE IF NOT EXISTS article_curation_catalogs",
        "CREATE TABLE IF NOT EXISTS article_curation_routes",
        "CREATE TABLE IF NOT EXISTS article_curation_items",
        "CREATE TABLE IF NOT EXISTS app_request_cooldowns",
        "idx_curation_one_processing",
        "idx_curation_one_representative",
        "'curation'",
    ):
        assert value in SCHEMA_SQL


@pytest.mark.asyncio
async def test_candidate_query_is_bounded_to_valid_recent_maps():
    pool = RecordingPool()
    assert await _repo(pool).curation_candidate_rows(limit=48, since_days=30) == []
    query, args = pool.calls[0]
    assert "m.status='ok'" in query
    assert "critic_verdict'='pass'" in query
    assert "LIMIT $1" in query
    assert args[0] == 48


@pytest.mark.asyncio
async def test_agent_pool_state_uses_strict_eligibility_and_fingerprint():
    pool = RecordingPool(
        row={"candidate_count": 42, "pool_fingerprint": "a" * 32}
    )
    assert await _repo(pool).curation_pool_state(since_days=30) == {
        "candidate_count": 42,
        "pool_fingerprint": "a" * 32,
    }
    query, args = pool.calls[0]
    assert "m.status='ok'" in query
    assert "format_version'='3'" in query
    assert "critic_verdict'='pass'" in query
    assert "string_agg" in query
    assert args[0].days == 30


@pytest.mark.asyncio
async def test_curation_sample_accounts_uses_postgres_jsonb_nonempty_predicate():
    pool = RecordingPool(
        row={"eligible_user_count": 0, "account_ids": []}
    )

    assert await _repo(pool).curation_sample_accounts(
        snapshot_at=datetime(2026, 7, 26, tzinfo=UTC),
        seed=1,
    ) == {"eligible_user_count": 0, "account_ids": []}

    query, _args = pool.calls[0]
    assert "s.saved_articles <> '{}'::jsonb" in query
    assert "jsonb_object_length" not in query


@pytest.mark.asyncio
async def test_curation_audience_queries_cast_snapshot_parameters_to_timestamptz():
    pool = RecordingPool(
        row={"eligible_user_count": 1, "account_ids": []}
    )
    repo = _repo(pool)
    snapshot_at = datetime(2026, 7, 26, tzinfo=UTC)

    await repo.curation_sample_accounts(snapshot_at=snapshot_at, seed=1)
    await repo.curation_audience_signal_rows(
        account_ids=[UUID("00000000-0000-4000-8000-000000000001")],
        snapshot_at=snapshot_at,
    )

    sample_query = pool.calls[0][0]
    signal_query = pool.calls[1][0]
    assert "$1::timestamptz-$2::interval" in sample_query
    assert "<= $1::timestamptz" in sample_query
    assert "$1-$2::interval" not in sample_query
    assert "$2::timestamptz-$3::interval" in signal_query
    assert "<= $2::timestamptz" in signal_query
    assert "$2-$3::interval" not in signal_query


@pytest.mark.asyncio
async def test_agent_search_supports_recent_browse_phrase_and_offset():
    pool = RecordingPool()
    repo = _repo(pool)

    assert (
        await repo.curation_search_rows(
            query="", offset=20, limit=12, since_days=30
        )
        == []
    )
    browse_query, browse_args = pool.calls[-1]
    assert "m.status='ok'" in browse_query
    assert "OFFSET $4 LIMIT $5" in browse_query
    assert browse_args[0] == ""
    assert browse_args[1] == []
    assert browse_args[3:] == (20, 12)

    assert (
        await repo.curation_search_rows(
            query="AI_인프라", offset=0, limit=20, since_days=30
        )
        == []
    )
    _search_query, search_args = pool.calls[-1]
    assert search_args[0] == "%ai\\_인프라%"
    assert "%ai\\_인프라%" in search_args[1]


@pytest.mark.asyncio
async def test_agent_details_rechecks_strict_pool_and_preserves_input_order():
    pool = RecordingPool()
    ids = ["article-2", "article-1"]
    assert (
        await _repo(pool).curation_detail_rows(article_ids=ids, since_days=30)
        == []
    )
    query, args = pool.calls[0]
    assert "a.id=ANY($1::text[])" in query
    assert "m.status='ok'" in query
    assert "array_position($1::text[],a.id)" in query
    assert args[0] == ids


@pytest.mark.asyncio
async def test_search_uses_parameterized_phrase_tokens_and_limit():
    pool = RecordingPool()
    assert await _repo(pool).search_article_maps(query="AI 인프라", limit=12) == []
    query, args = pool.calls[0]
    assert "ILIKE ANY($2::text[])" in query
    assert "LIMIT $3" in query
    assert args[0] == "%ai 인프라%"
    assert "%ai%" in args[1]
    assert "%인프라%" in args[1]
    assert args[2] == 12


@pytest.mark.asyncio
async def test_c11601_rag_hydration_revalidates_public_maps_and_preserves_hit_order():
    rows = [
        {
            "article_id": "b" * 64,
            "source_url": "https://news.example/b",
            "domain": "news.example",
            "published_at": datetime(2026, 8, 2, tzinfo=UTC),
            "source_language": "Korean",
            "sourcecountry": "South Korea",
            "status": "ok",
            "data": '{"headline":"B"}',
        }
    ]
    pool = RecordingPool(rows=rows)
    ids = ["b" * 64, "a" * 64]

    hydrated = await _repo(pool).published_article_maps_by_ids(article_ids=ids)

    assert hydrated[0]["article_id"] == "b" * 64
    assert hydrated[0]["data"] == {"headline": "B"}
    query, args = pool.calls[0]
    assert "unnest($1::text[]) WITH ORDINALITY" in query
    assert "m.status IN ('ok','degraded')" in query
    assert "m.data IS NOT NULL" in query
    assert "p.draft_id IS NULL OR p.state='published'" in query
    assert "ORDER BY requested.ordinality" in query
    assert args == (ids,)


@pytest.mark.asyncio
async def test_published_editorial_query_is_kind_state_and_keyset_filtered():
    pool = RecordingPool()
    cursor_time = datetime(2026, 7, 22, 11, 0, tzinfo=UTC)
    article_id = "a" * 64

    assert await _repo(pool).published_editorial_maps(
        publication_kind="weekly_insight",
        limit=21,
        cursor_published_at=cursor_time,
        cursor_article_id=article_id,
    ) == []

    query, args = pool.calls[0]
    assert "FROM editorial_publications p" in query
    assert "JOIN editorial_revisions r ON r.id=p.revision_id" in query
    assert "p.state='published'" in query
    assert "COALESCE(r.document->>'publication_kind','editorial')=$4" in query
    assert "m.status IN ('ok','degraded')" in query
    assert "p.published_at < $2" in query
    assert "p.published_at=$2 AND a.id < $3" in query
    assert "ORDER BY p.published_at DESC, a.id DESC" in query
    assert args == (21, cursor_time, article_id, "weekly_insight")


@pytest.mark.asyncio
async def test_repository_rejects_unbounded_candidate_and_search_inputs_before_db():
    pool = RecordingPool()
    repo = _repo(pool)
    with pytest.raises(ValueError):
        await repo.curation_candidate_rows(limit=7, since_days=30)
    with pytest.raises(ValueError):
        await repo.search_article_maps(query="a", limit=12)
    with pytest.raises(ValueError):
        await repo.published_article_maps_by_ids(article_ids=[])
    with pytest.raises(ValueError):
        await repo.published_article_maps_by_ids(article_ids=["same", "same"])
    with pytest.raises(ValueError):
        await repo.published_article_maps_by_ids(article_ids=[str(index) for index in range(6)])
    with pytest.raises(ValueError):
        await repo.published_editorial_maps(publication_kind="editorial", limit=22)
    with pytest.raises(ValueError):
        await repo.published_editorial_maps(
            publication_kind="editorial",
            limit=20,
            cursor_published_at=datetime.now(UTC),
        )
    with pytest.raises(ValueError):
        await repo.published_editorial_maps(publication_kind="weekly", limit=20)
    with pytest.raises(ValueError):
        await repo.curation_pool_state(since_days=0)
    with pytest.raises(ValueError):
        await repo.curation_search_rows(
            query="a", offset=0, limit=12, since_days=30
        )
    with pytest.raises(ValueError):
        await repo.curation_search_rows(
            query="", offset=201, limit=12, since_days=30
        )
    with pytest.raises(ValueError):
        await repo.curation_detail_rows(
            article_ids=["same", "same"], since_days=30
        )
    assert pool.calls == []


class CurationClaimConnection:
    """Minimal connection double for the advisory-locked claim transaction."""

    def __init__(self, *, latest=None, existing=None):
        self.latest = latest
        self.existing = existing
        self.calls: list[tuple[str, str, tuple]] = []

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        return "OK"

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", query, args))
        if "SELECT now()" in query:
            return datetime(2026, 8, 3, 12, tzinfo=UTC)
        return None

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        if "status='published' AND generated_at IS NOT NULL" in query:
            return self.latest
        if "WHERE input_hash=$1" in query:
            return self.existing
        if "INSERT INTO article_curation_catalogs" in query:
            return {"id": CATALOG_ID, "status": "processing"}
        if "SET status='processing'" in query:
            return {"id": CATALOG_ID, "status": "processing"}
        raise AssertionError(f"unexpected fetchrow query: {query}")


class CurationClaimPool:
    def __init__(self, **kwargs):
        self.connection = CurationClaimConnection(**kwargs)

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


@pytest.mark.asyncio
async def test_curation_claim_looks_up_the_existing_row_by_input_hash_only():
    # article_curation_catalogs has no candidate_slot column; referring to one
    # made every poll raise NameError before touching the database.
    pool = CurationClaimPool()

    claimed = await _repo(pool).claim_curation_catalog(
        editorial_day=date(2026, 8, 3),
        input_hash="a" * 64,
        candidate_count=5000,
        min_interval_s=21600,
        max_attempts=3,
        lease_s=900,
    )

    assert claimed == {"id": CATALOG_ID, "status": "processing"}
    lookups = [
        query
        for kind, query, _args in pool.connection.calls
        if kind == "fetchrow" and "FROM article_curation_catalogs" in query
    ]
    assert any("WHERE input_hash=$1" in query for query in lookups)
    assert not any("candidate_slot" in query for query in lookups)
    assert any(
        "INSERT INTO article_curation_catalogs" in query
        for _kind, query, _args in pool.connection.calls
    )


@pytest.mark.asyncio
async def test_curation_claim_reclaims_a_queued_row_for_the_same_input_hash():
    pool = CurationClaimPool(
        existing={
            "id": CATALOG_ID,
            "status": "queued",
            "next_attempt_at": datetime(2026, 8, 3, 11, tzinfo=UTC),
            "attempt_count": 1,
            "max_attempts": 3,
        }
    )

    claimed = await _repo(pool).claim_curation_catalog(
        editorial_day=date(2026, 8, 3),
        input_hash="a" * 64,
        candidate_count=5000,
        min_interval_s=21600,
        max_attempts=3,
        lease_s=900,
    )

    assert claimed == {"id": CATALOG_ID, "status": "processing"}
    assert not any(
        "INSERT INTO article_curation_catalogs" in query
        for _kind, query, _args in pool.connection.calls
    )


@pytest.mark.asyncio
async def test_curation_claim_respects_the_minimum_refresh_interval():
    pool = CurationClaimPool(
        latest={"generated_at": datetime(2026, 8, 3, 11, tzinfo=UTC)}
    )

    claimed = await _repo(pool).claim_curation_catalog(
        editorial_day=date(2026, 8, 3),
        input_hash="a" * 64,
        candidate_count=5000,
        min_interval_s=21600,
        max_attempts=3,
        lease_s=900,
    )

    assert claimed is None
    assert not any(
        "INSERT INTO article_curation_catalogs" in query
        for _kind, query, _args in pool.connection.calls
    )
