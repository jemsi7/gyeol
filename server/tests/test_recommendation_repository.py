from __future__ import annotations

import inspect
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.db import SCHEMA_SQL
from app.db_bootstrap import REQUIRED_INDEXES, REQUIRED_TABLES
from app.repository import Repository


NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


class FakePool:
    def __init__(self):
        self.fetch_results: list[list[dict]] = []
        self.fetchrow_results: list[dict | None] = []
        self.fetchval_results: list[object] = []
        self.calls: list[tuple[str, str, tuple]] = []

    async def execute(self, sql, *args):
        self.calls.append(("execute", " ".join(sql.split()), args))
        return "INSERT 0 1"

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", " ".join(sql.split()), args))
        return self.fetch_results.pop(0) if self.fetch_results else []

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", " ".join(sql.split()), args))
        return self.fetchrow_results.pop(0) if self.fetchrow_results else None

    async def fetchval(self, sql, *args):
        self.calls.append(("fetchval", " ".join(sql.split()), args))
        return self.fetchval_results.pop(0) if self.fetchval_results else None


def _repository(pool: FakePool) -> Repository:
    return Repository(SimpleNamespace(pool=pool))


def test_recommendation_feature_schema_constraints_indexes_and_no_personal_profile():
    assert "CREATE TABLE IF NOT EXISTS article_recommendation_features" in SCHEMA_SQL
    section = SCHEMA_SQL.split(
        "CREATE TABLE IF NOT EXISTS article_recommendation_features", 1
    )[1].split("CREATE TABLE IF NOT EXISTS editor_operators", 1)[0]
    assert "REFERENCES articles(id) ON DELETE CASCADE" in section
    assert "CHECK (feature_version = 1)" in section
    assert "CHECK (cardinality(terms) <= 64)" in section
    assert "CHECK (jsonb_typeof(term_weights) = 'object')" in section
    assert "CHECK (quality_score BETWEEN 0 AND 1)" in section
    assert "USING GIN (terms)" in section
    assert "article_recommendation_features" in REQUIRED_TABLES
    assert "idx_article_recommendation_terms" in REQUIRED_INDEXES
    assert "idx_article_recommendation_fresh" in REQUIRED_INDEXES
    assert "account_recommendation_profile" not in SCHEMA_SQL
    assert "recommendation_serve_history" not in SCHEMA_SQL


@pytest.mark.asyncio
async def test_map_save_paths_refresh_shared_feature_without_breaking_map_write():
    pool = FakePool()
    pool.fetchrow_results = [
        {"domain": "news.example", "published_at": NOW},
        {"domain": "news.example", "published_at": NOW},
    ]
    repository = _repository(pool)
    map_data = {
        "headline": "AI 도시",
        "summary": "인공지능과 도시",
        "keywords": ["AI", "도시"],
        "scenes": [{"title": "변화", "summary": "새 흐름"}],
    }
    article_id = "a" * 64

    await repository.upsert_map(
        article_id,
        format_version=3,
        model="test",
        status="ok",
        data=map_data,
        error=None,
    )
    await repository.save_map_and_mark_done(
        article_id,
        format_version=3,
        model="test",
        status="ok",
        data=map_data,
        error=None,
    )

    sql = "\n".join(call[1] for call in pool.calls)
    assert sql.count("INSERT INTO article_recommendation_features") == 2
    assert "_best_effort_refresh_article_recommendation_feature" in inspect.getsource(
        Repository.publish_editorial_draft
    )


@pytest.mark.asyncio
async def test_signal_query_is_account_scoped_180_day_500_bound():
    pool = FakePool()
    repository = _repository(pool)
    account_id = uuid4()
    since = NOW - timedelta(days=180)

    await repository.recommendation_signal_rows(
        account_id,
        snapshot_at=NOW,
        since=since,
        limit=500,
    )

    _, sql, args = pool.calls[-1]
    assert "WHERE account_id=$1" in sql
    assert "occurred_at >= $2" in sql
    assert "received_at <= $3" in sql
    assert "LIMIT $4" in sql
    assert args == (account_id, since, NOW, 500)


@pytest.mark.asyncio
async def test_candidate_lanes_share_feed_eligibility_and_cap_300():
    pool = FakePool()
    repository = _repository(pool)

    await repository.recommendation_candidates(
        profile_terms=["ai"],
        short_terms=["로봇"],
        saved_terms=["기후"],
        context_terms=["반도체"],
        snapshot_at=NOW,
        editorial_day=date(2026, 7, 26),
    )

    _, sql, args = pool.calls[-1]
    for source in (
        "fresh",
        "profile_matches",
        "short_matches",
        "saved_matches",
        "context_matches",
        "today_editorial",
    ):
        assert source in sql
    assert "m.status IN ('ok','degraded')" in sql
    assert "(p.draft_id IS NULL OR p.state='published')" in sql
    assert "LIMIT 120" in sql
    assert "LIMIT 80" in sql
    assert "LIMIT 20" in sql
    assert sql.endswith("LIMIT 300")
    assert "source_summary AS" in sql
    assert "GROUP BY article_id" in sql
    assert "WHERE source.article_id=first_source.article_id" not in sql
    assert args[0] == ["ai"]
    assert args[1] == ["로봇"]
    assert args[2] == ["기후"]
    assert args[3] == ["반도체"]


@pytest.mark.asyncio
async def test_saved_and_exposure_reads_are_account_scoped_and_bounded():
    pool = FakePool()
    repository = _repository(pool)
    account_id = uuid4()

    await repository.recommendation_saved_rows(account_id, limit=100)
    await repository.recommendation_exposure_rows(
        account_id,
        snapshot_at=NOW,
        continuation=False,
        limit=500,
    )

    saved_sql = pool.calls[-2][1]
    exposure_sql = pool.calls[-1][1]
    assert "jsonb_each_text(state.saved_articles)" in saved_sql
    assert "state.account_id=$1" in saved_sql
    assert "page_kind='initial'" in exposure_sql
    assert "interval '72 hours'" in exposure_sql
    assert "FROM app_article_list_exposures" in exposure_sql
    assert "response_id AS request_id" in exposure_sql
    assert "'library'::text AS lane" in exposure_sql
    assert pool.calls[-1][2] == (account_id, NOW, False, 500)


@pytest.mark.asyncio
async def test_exposure_write_is_batched_and_retention_bounded():
    pool = FakePool()
    pool.fetchval_results = [2]
    repository = _repository(pool)
    account_id = uuid4()
    request_id = uuid4()

    recorded = await repository.record_recommendation_exposures(
        account_id,
        request_id=request_id,
        ranking_version="content-v1",
        surface="home",
        page_kind="initial",
        served_at=NOW,
        items=[
            {"article_id": "a" * 64, "position": 0, "lane": "core"},
            {"article_id": "b" * 64, "position": 1, "lane": "now"},
        ],
    )

    assert recorded == 2
    _, sql, args = pool.calls[-1]
    assert pool.calls[-1][0] == "fetchval"
    assert "jsonb_to_recordset" in sql
    assert "interval '90 days'" in sql
    assert "$6::timestamptz" in sql
    assert "RETURNING article_id, served_at" in sql
    assert "INSERT INTO article_feed_impression_counts" in sql
    assert "SELECT count(*)::bigint FROM inserted" in sql
    assert args[:6] == (
        request_id,
        account_id,
        "content-v1",
        "home",
        "initial",
        NOW,
    )


@pytest.mark.asyncio
async def test_selected_feed_hydration_preserves_rank_order():
    pool = FakePool()
    pool.fetch_results = [
        [
            {"article_id": "b" * 64, "status": "ok", "data": {"headline": "B"}},
            {"article_id": "a" * 64, "status": "ok", "data": {"headline": "A"}},
        ]
    ]
    repository = _repository(pool)

    rows = await repository.recommendation_feed_rows(["b" * 64, "a" * 64])

    assert [row["article_id"] for row in rows] == ["b" * 64, "a" * 64]
    assert "WITH ORDINALITY" in pool.calls[-1][1]
    assert "ORDER BY r.ordinal" in pool.calls[-1][1]


@pytest.mark.asyncio
async def test_current_consent_link_is_idempotent_and_rejects_stale():
    pool = FakePool()
    account_id = uuid4()
    consent_id = uuid4()
    pool.fetchrow_results = [
        {
            "consent_id": consent_id,
            "account_id": account_id,
            "policy_version": "1.5.0",
            "policy_url": "https://example.test/privacy",
            "accepted_at": NOW,
            "linked_at": NOW,
        },
        None,
    ]
    repository = _repository(pool)

    linked = await repository.link_account_privacy_consent(
        account_id, consent_id, "1.5.0"
    )
    stale = await repository.link_account_privacy_consent(
        account_id, consent_id, "1.4.0"
    )

    assert linked and linked["policy_version"] == "1.5.0"
    assert stale is None
    sql = pool.calls[0][1]
    assert "ON CONFLICT (consent_id,account_id) DO UPDATE SET" in sql
    assert "linked_at=app_privacy_consent_accounts.linked_at" in sql
    assert "FROM eligible JOIN linked" in sql
    assert "JOIN app_privacy_consent_accounts link" not in sql


def test_account_delete_cascades_inputs_but_shared_features_remain_article_scoped():
    activity = SCHEMA_SQL.split(
        "CREATE TABLE IF NOT EXISTS app_account_activity_events", 1
    )[1]
    feedback = SCHEMA_SQL.split(
        "CREATE TABLE IF NOT EXISTS app_article_feedback", 1
    )[1].split("CREATE TABLE IF NOT EXISTS app_account_reading_states", 1)[0]
    feature = SCHEMA_SQL.split(
        "CREATE TABLE IF NOT EXISTS article_recommendation_features", 1
    )[1].split("CREATE TABLE IF NOT EXISTS editor_operators", 1)[0]
    assert "REFERENCES app_accounts(id) ON DELETE CASCADE" in activity
    assert "REFERENCES app_accounts(id) ON DELETE CASCADE" in feedback
    assert "app_accounts" not in feature
