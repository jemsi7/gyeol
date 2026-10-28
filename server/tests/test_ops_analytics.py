"""Repository contracts for aggregate-only /ops member analytics."""
from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.repository import Repository


NOW = datetime(2026, 8, 2, 3, 0, tzinfo=UTC)


class AnalyticsPool:
    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, query, *args):
        normalized = " ".join(query.split())
        self.calls.append((normalized, args))
        if "ops:analytics-anchor" in normalized:
            return {
                "generated_at": NOW,
                "session_activity_started_on": date(2026, 7, 20),
                "session_activity_through_on": date(2026, 8, 2),
                "total_members": 12,
                "today_dau": 3,
                "current_month_mau": 6,
                "total_article_views": 9876,
            }
        if "ops:analytics-activity" in normalized:
            return {
                "period_start": date(2026, 7, 20),
                "period_end": date(2026, 8, 2),
                "active_members": 4,
                "search_count": 1,
                "article_opened_count": 4,
                "scene_viewed_count": 2,
                "article_completed_count": 1,
            }
        raise AssertionError(normalized)

    async def fetch(self, query, *args):
        normalized = " ".join(query.split())
        self.calls.append((normalized, args))
        if "ops:analytics-user-trend" in normalized:
            return [
                {"date": date(2026, 8, 1), "new_members": 2, "cumulative_members": 10},
                {"date": date(2026, 8, 2), "new_members": 2, "cumulative_members": 12},
            ]
        if "ops:analytics-dau" in normalized:
            return [
                {"date": date(2026, 8, 1), "active_members": 2},
                {"date": date(2026, 8, 2), "active_members": 3},
            ]
        if "ops:analytics-mau" in normalized:
            return [
                {"month": date(2026, 7, 1), "active_members": 4, "is_partial": True},
                {"month": date(2026, 8, 1), "active_members": 6, "is_partial": True},
            ]
        if "ops:analytics-popular" in normalized:
            return [
                {
                    "rank": 1,
                    "article_id": "article-1",
                    "headline": "가장 많이 읽힌 기사",
                    "domain": "news.example",
                    "published_at": NOW,
                    "view_count": 500,
                },
                {
                    "rank": 2,
                    "article_id": "article-2",
                    "headline": "두 번째 기사",
                    "domain": None,
                    "published_at": None,
                    "view_count": 300,
                },
            ]
        if "ops:analytics-view-rate" in normalized:
            return [
                {
                    "direction": "high",
                    "rank": 1,
                    "article_id": "article-high",
                    "headline": "노출보다 조회가 많은 기사",
                    "domain": "high.example",
                    "published_at": NOW,
                    "view_count": 9,
                    "impression_count": 6,
                    "view_rate_percent": 150.0,
                    "first_impressed_at": NOW,
                },
                {
                    "direction": "low",
                    "rank": 1,
                    "article_id": "article-low",
                    "headline": "아직 조회되지 않은 기사",
                    "domain": None,
                    "published_at": None,
                    "view_count": 0,
                    "impression_count": 5,
                    "view_rate_percent": 0.0,
                    "first_impressed_at": NOW,
                },
            ]
        raise AssertionError(normalized)


class FakeDB:
    def __init__(self, pool):
        self.pool = pool


@pytest.mark.asyncio
async def test_ops_analytics_reconciles_summary_trends_activity_and_rank():
    pool = AnalyticsPool()
    result = await Repository(FakeDB(pool)).ops_analytics(
        daily_days=90,
        monthly_months=12,
    )

    assert result["generated_at"] == NOW
    assert result["timezone"] == "Asia/Seoul"
    assert result["coverage"] == {
        "session_activity_started_on": date(2026, 7, 20),
        "session_activity_through_on": date(2026, 8, 2),
    }
    assert result["summary"] == {
        "total_members": 12,
        "today_dau": 3,
        "current_month_mau": 6,
        "dau_mau_ratio": 50.0,
        "total_article_views": 9876,
    }
    assert result["user_trend"][-1] == {
        "date": date(2026, 8, 2),
        "new_members": 2,
        "cumulative_members": 12,
    }
    assert result["dau_trend"][0]["date"] == date(2026, 8, 1)
    assert result["mau_trend"][0]["is_partial"] is True
    assert result["activity"]["total_events"] == 8
    assert result["activity"]["events_per_active_member"] == 2.0
    assert result["activity"]["event_types"] == [
        {"event_type": "search", "count": 1, "share": 12.5},
        {"event_type": "article_opened", "count": 4, "share": 50.0},
        {"event_type": "scene_viewed", "count": 2, "share": 25.0},
        {"event_type": "article_completed", "count": 1, "share": 12.5},
    ]
    assert [row["rank"] for row in result["popular_articles"]] == [1, 2]
    assert all("account_id" not in row for row in result["popular_articles"])
    assert result["high_view_rate_articles"] == [
        {
            "rank": 1,
            "article_id": "article-high",
            "headline": "노출보다 조회가 많은 기사",
            "domain": "high.example",
            "published_at": NOW,
            "view_count": 9,
            "impression_count": 6,
            "view_rate_percent": 150.0,
            "first_impressed_at": NOW,
        }
    ]
    assert result["low_view_rate_articles"][0]["view_count"] == 0
    assert result["low_view_rate_articles"][0]["impression_count"] == 5
    assert all(
        "account_id" not in row
        for key in ("high_view_rate_articles", "low_view_rate_articles")
        for row in result[key]
    )

    sql = " ".join(query for query, _args in pool.calls)
    assert "SUM(view_count)" in sql
    assert "COUNT(*) FROM article_view_events" not in sql
    assert "app_account_session_activity_days" in sql
    assert "AT TIME ZONE 'Asia/Seoul'" in sql
    assert "LIMIT 100" in sql
    assert "map.status IN ('ok','degraded')" in sql
    assert "ops:analytics-view-rate" in sql
    assert "app_guest_session_activity_days" in sql
    assert "impression.impression_count >= 5" in sql
    assert "event.received_at >= eligible.first_impressed_at" in sql
    assert "LEFT JOIN article_view_events" in sql


@pytest.mark.asyncio
async def test_ops_analytics_rejects_unbounded_periods_before_database_access():
    pool = AnalyticsPool()
    repository = Repository(FakeDB(pool))

    with pytest.raises(ValueError, match="daily_days"):
        await repository.ops_analytics(daily_days=31, monthly_months=12)
    with pytest.raises(ValueError, match="monthly_months"):
        await repository.ops_analytics(daily_days=90, monthly_months=24)

    assert pool.calls == []


@pytest.mark.asyncio
async def test_ops_analytics_empty_session_coverage_is_honest_not_zero_history():
    class EmptyPool(AnalyticsPool):
        async def fetchrow(self, query, *args):
            normalized = " ".join(query.split())
            self.calls.append((normalized, args))
            if "ops:analytics-anchor" in normalized:
                return {
                    "generated_at": NOW,
                    "session_activity_started_on": None,
                    "session_activity_through_on": None,
                    "total_members": 0,
                    "today_dau": None,
                    "current_month_mau": None,
                    "total_article_views": 0,
                }
            if "ops:analytics-activity" in normalized:
                return {
                    "period_start": None,
                    "period_end": date(2026, 8, 2),
                    "active_members": 0,
                    "search_count": 0,
                    "article_opened_count": 0,
                    "scene_viewed_count": 0,
                    "article_completed_count": 0,
                }
            raise AssertionError(normalized)

        async def fetch(self, query, *args):
            normalized = " ".join(query.split())
            self.calls.append((normalized, args))
            if "ops:analytics-user-trend" in normalized:
                return []
            if "ops:analytics-popular" in normalized:
                return []
            if "ops:analytics-view-rate" in normalized:
                return []
            if "ops:analytics-dau" in normalized or "ops:analytics-mau" in normalized:
                return []
            raise AssertionError(normalized)

    result = await Repository(FakeDB(EmptyPool())).ops_analytics(
        daily_days=30,
        monthly_months=6,
    )

    assert result["summary"]["today_dau"] is None
    assert result["summary"]["current_month_mau"] is None
    assert result["summary"]["dau_mau_ratio"] is None
    assert result["dau_trend"] == []
    assert result["mau_trend"] == []
    assert result["activity"]["events_per_active_member"] is None
    assert result["high_view_rate_articles"] == []
    assert result["low_view_rate_articles"] == []
