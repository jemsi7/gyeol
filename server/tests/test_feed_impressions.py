from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.db import SCHEMA_SQL
from app.repository import Repository


def test_c11800_schema_separates_anonymous_aggregate_and_account_ledger():
    aggregate = SCHEMA_SQL.split(
        "CREATE TABLE IF NOT EXISTS article_feed_impression_counts", 1
    )[1].split("CREATE TABLE IF NOT EXISTS article_image_links", 1)[0]
    assert "article_id          TEXT PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE" in aggregate
    assert "impression_count    BIGINT NOT NULL DEFAULT 0" in aggregate
    assert "CHECK (impression_count >= 0)" in aggregate
    assert "first_impressed_at <= last_impressed_at" in aggregate

    account = SCHEMA_SQL.split(
        "CREATE TABLE IF NOT EXISTS app_article_list_exposures", 1
    )[1].split("INSERT INTO article_view_counts", 1)[0]
    assert "account_id  UUID NOT NULL REFERENCES app_accounts(id) ON DELETE CASCADE" in account
    assert "article_id  TEXT NOT NULL REFERENCES articles(id) ON DELETE CASCADE" in account
    assert "surface IN ('library')" in account
    assert "PRIMARY KEY (response_id, position)" in account
    assert "installation_id" not in account
    assert "idx_article_list_exposure_account_time" in account
    assert "idx_article_list_exposure_account_article" in account


class RecordingPool:
    def __init__(self, result: int = 0):
        self.result = result
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetchval(self, query: str, *args: object):
        self.calls.append((query, args))
        return self.result


class FakeDB:
    def __init__(self, pool: RecordingPool):
        self.pool = pool


@pytest.mark.asyncio
async def test_c11800_empty_impression_batch_is_a_noop():
    pool = RecordingPool()
    repository = Repository(FakeDB(pool))

    recorded = await repository.record_article_list_impressions(
        article_ids=[],
        served_at=datetime(2026, 8, 2, tzinfo=UTC),
    )

    assert recorded == 0
    assert pool.calls == []


@pytest.mark.asyncio
async def test_c11800_anonymous_multiset_uses_one_atomic_upsert():
    pool = RecordingPool(result=3)
    repository = Repository(FakeDB(pool))
    article_a = "a" * 64
    article_b = "b" * 64
    served_at = datetime(2026, 8, 2, 12, tzinfo=UTC)

    recorded = await repository.record_article_list_impressions(
        article_ids=[article_a, article_a, article_b],
        served_at=served_at,
    )

    assert recorded == 3
    assert len(pool.calls) == 1
    query, args = pool.calls[0]
    assert "WITH requested AS" in query
    assert "WITH ORDINALITY" in query
    assert "ON CONFLICT (article_id) DO UPDATE" in query
    assert "impression_count=" in query
    assert "app_article_list_exposures" not in query
    assert args == ([article_a, article_a, article_b], served_at)


@pytest.mark.asyncio
async def test_c11802_account_batch_requires_complete_account_context():
    repository = Repository(FakeDB(RecordingPool()))
    when = datetime(2026, 8, 2, tzinfo=UTC)

    with pytest.raises(ValueError):
        await repository.record_article_list_impressions(
            article_ids=["a" * 64],
            served_at=when,
            account_id=UUID("11111111-1111-1111-1111-111111111111"),
        )
    with pytest.raises(ValueError):
        await repository.record_article_list_impressions(
            article_ids=["a" * 64],
            served_at=when,
            surface="library",
        )


@pytest.mark.asyncio
async def test_c11802_account_ledger_and_aggregate_share_one_statement():
    pool = RecordingPool(result=2)
    repository = Repository(FakeDB(pool))
    account_id = UUID("11111111-1111-1111-1111-111111111111")
    response_id = UUID("22222222-2222-2222-2222-222222222222")
    served_at = datetime(2026, 8, 2, 12, tzinfo=UTC)

    recorded = await repository.record_article_list_impressions(
        article_ids=["a" * 64, "b" * 64],
        served_at=served_at,
        account_id=account_id,
        surface="library",
        response_id=response_id,
    )

    assert recorded == 2
    assert len(pool.calls) == 1
    query, args = pool.calls[0]
    assert "DELETE FROM app_article_list_exposures" in query
    assert "INSERT INTO app_article_list_exposures" in query
    assert "RETURNING article_id, served_at" in query
    assert "INSERT INTO article_feed_impression_counts" in query
    assert args == (
        ["a" * 64, "b" * 64],
        served_at,
        account_id,
        "library",
        response_id,
    )


@pytest.mark.asyncio
async def test_c11800_rejects_naive_impression_time():
    repository = Repository(FakeDB(RecordingPool()))
    with pytest.raises(ValueError):
        await repository.record_article_list_impressions(
            article_ids=["a" * 64],
            served_at=datetime(2026, 8, 2),
        )
