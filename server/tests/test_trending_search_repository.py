from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.repository import Repository
from app.trending_search import TrendingMetric


AS_OF = datetime(2026, 8, 1, 3, 14, tzinfo=UTC)
BUCKET = datetime(2026, 8, 1, 3, 10, tzinfo=UTC)


class FakeDB:
    def __init__(self, pool):
        self.pool = pool


class RankingPool:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def fetch(self, query, *args):
        self.calls.append((" ".join(query.split()), args))
        return self.rows


@pytest.mark.asyncio
async def test_repository_trending_rankings_use_three_bounded_categories():
    rows = [
        {
            "metric": "view_total",
            "article_id": "article-total",
            "rank": 1,
            "metric_value": 20,
            "recent_count": 0,
            "previous_count": 0,
            "keywords": ["인공지능", "인프라"],
        },
        {
            "metric": "view_growth",
            "article_id": "article-growth",
            "rank": 1,
            "metric_value": 5,
            "recent_count": 5,
            "previous_count": 0,
            "keywords": '["반도체", "공급망"]',
        },
        {
            "metric": "bookmark_total",
            "article_id": "article-bookmark",
            "rank": 1,
            "metric_value": 7,
            "recent_count": 0,
            "previous_count": 0,
            "keywords": ["데이터센터", "전력망"],
        },
    ]
    pool = RankingPool(rows)
    repository = Repository(FakeDB(pool))

    rankings = await repository.trending_article_rankings(as_of=AS_OF)

    assert tuple(rankings) == (
        TrendingMetric.VIEW_TOTAL,
        TrendingMetric.VIEW_GROWTH,
        TrendingMetric.BOOKMARK_TOTAL,
    )
    assert rankings[TrendingMetric.VIEW_GROWTH][0].previous_count == 0
    assert rankings[TrendingMetric.VIEW_GROWTH][0].keywords == ("반도체", "공급망")
    sql, args = pool.calls[0]
    assert "COALESCE(a.seendate,a.first_seen_at) >= $2" in sql
    assert "event.received_at >= $4" in sql
    assert "event.received_at < $1" in sql
    assert "recent_count+previous_count >= $5" in sql
    assert "jsonb_each_text(state.saved_articles)" in sql
    assert "WHERE rank <= $6" in sql
    assert args == (
        AS_OF,
        AS_OF - timedelta(days=30),
        AS_OF - timedelta(hours=2),
        AS_OF - timedelta(hours=1),
        5,
        10,
    )


class Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class Acquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class SnapshotConnection:
    def __init__(self):
        self.select_count = 0
        self.ranking_fetch_count = 0
        self.execute_calls = []
        self.insert_args = None
        self.generated_at = AS_OF + timedelta(seconds=1)

    def transaction(self):
        return Transaction()

    async def fetchrow(self, query, *args):
        normalized = " ".join(query.split())
        if normalized.startswith("SELECT bucket_started_at"):
            self.select_count += 1
            return None
        if normalized.startswith("INSERT INTO trending_search_snapshots"):
            self.insert_args = args
            return {
                "bucket_started_at": args[0],
                "category_keywords": args[1],
                "keyword_pool": args[2],
                "suggestions": args[3],
                "generated_at": args[4],
            }
        raise AssertionError(normalized)

    async def fetch(self, query, *args):
        self.ranking_fetch_count += 1
        return [
            {
                "metric": "view_total",
                "article_id": "article-1",
                "rank": 1,
                "metric_value": 10,
                "recent_count": 0,
                "previous_count": 0,
                "keywords": ["인공지능", "인프라", "전력망"],
            },
            {
                "metric": "view_growth",
                "article_id": "article-2",
                "rank": 1,
                "metric_value": 5,
                "recent_count": 5,
                "previous_count": 0,
                "keywords": ["반도체", "공급망", "로봇 산업"],
            },
            {
                "metric": "bookmark_total",
                "article_id": "article-3",
                "rank": 1,
                "metric_value": 3,
                "recent_count": 0,
                "previous_count": 0,
                "keywords": ["데이터센터", "클라우드", "에너지"],
            },
        ]

    async def fetchval(self, query, *args):
        assert "SELECT now()" in query
        return self.generated_at

    async def execute(self, query, *args):
        normalized = " ".join(query.split())
        self.execute_calls.append((normalized, args))
        return "OK"


class SnapshotPool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return Acquire(self.connection)


@pytest.mark.asyncio
async def test_snapshot_get_or_create_locks_stores_aggregate_only_and_cleans():
    connection = SnapshotConnection()
    repository = Repository(FakeDB(SnapshotPool(connection)))

    snapshot = await repository.current_trending_search_snapshot(as_of=AS_OF)

    assert snapshot.bucket_started_at == BUCKET
    assert len(snapshot.keyword_pool) == 9
    assert len(snapshot.suggestions) == 3
    assert set(snapshot.suggestions).issubset(snapshot.keyword_pool)
    assert connection.select_count == 2
    assert connection.ranking_fetch_count == 1
    assert connection.insert_args is not None
    serialized = " ".join(str(value) for value in connection.insert_args)
    assert "article-1" not in serialized
    assert any("pg_advisory_xact_lock" in query for query, _args in connection.execute_calls)
    assert any("interval '24 hours'" in query for query, _args in connection.execute_calls)


@pytest.mark.asyncio
async def test_repository_trending_bounds_reject_naive_or_changed_contract():
    repository = Repository(FakeDB(RankingPool([])))
    with pytest.raises(ValueError, match="timezone"):
        await repository.trending_article_rankings(as_of=datetime(2026, 8, 1))
    with pytest.raises(ValueError, match="one hour"):
        await repository.trending_article_rankings(
            as_of=AS_OF,
            growth_window=timedelta(minutes=30),
        )

