"""Protected operations snapshot API contract tests."""

from datetime import UTC, datetime

import httpx
import pytest

from app.api import app, state
from app.config import Settings


NOW = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)


class FakeOpsRepo:
    def __init__(self):
        self.queue_args = None
        self.available_count_calls = 0
        self.analytics_args = None
        self.analytics_error = None

    async def worker_telemetry_rows(self, window_minutes):
        return {"now": NOW, "instances": [], "activities": []}

    async def available_article_count(self):
        self.available_count_calls += 1
        return 37

    async def queue_stats(self):
        return {"queued": 2, "processing": 1, "done": 5, "dead": 1, "due_now": 1}

    async def queue_items(self, limit, status=None):
        self.queue_args = (limit, status)
        return [
            {
                "article_id": "a" * 64,
                "title": "대기 중인 기사",
                "domain": "news.example",
                "status": status or "queued",
                "attempts": 1,
                "max_attempts": 3,
                "next_attempt_at": NOW,
                "last_error": "provider timeout",
                "created_at": NOW,
                "updated_at": NOW,
            }
        ]

    async def push_delivery_stats(self):
        return {
            "queued": 3,
            "processing": 1,
            "sent": 11,
            "failed": 2,
            "cancelled": 4,
            "retry_attempts": 5,
            "active_registrations": 7,
            "invalid_registrations": 1,
            "last_sent_at": NOW,
            "last_failed_at": NOW,
            "last_retry_at": NOW,
        }

    async def last_runs(self, limit):
        return [{"id": 1, "job_name": "climate", "started_at": NOW, "ok": True}]

    async def map_budget_status(self, *, default_limit, default_share, **_kwargs):
        self.budget_defaults = (
            default_limit,
            default_share,
            _kwargs.get("default_run_count"),
        )
        return {
            "daily_map_limit": 60,
            "fresh_share_percent": 75,
            "daily_run_count": 1,
            "updated_at": NOW,
            "source": "db",
            "editorial_day": NOW.date(),
            "current_run": 1,
            "current_run_allowance": 60,
            "current_allowance": 60,
            "current_remaining": 48,
            "next_run_at": None,
            "buckets": {
                "fresh": {"quota": 45, "consumed": 10, "remaining": 35},
                "backlog": {"quota": 15, "consumed": 2, "remaining": 13},
            },
            "total_consumed": 12,
            "total_remaining": 48,
            "triage": {"scored": 30, "pending": 4},
        }

    async def ops_analytics(self, *, daily_days, monthly_months):
        self.analytics_args = (daily_days, monthly_months)
        if self.analytics_error:
            raise self.analytics_error
        return {
            "generated_at": NOW,
            "timezone": "Asia/Seoul",
            "coverage": {
                "session_activity_started_on": NOW.date(),
                "session_activity_through_on": NOW.date(),
            },
            "summary": {
                "total_members": 12,
                "today_dau": 3,
                "current_month_mau": 6,
                "dau_mau_ratio": 50.0,
                "total_article_views": 9876,
            },
            "user_trend": [],
            "dau_trend": [],
            "mau_trend": [],
            "activity": {
                "period_start": NOW.date(),
                "period_end": NOW.date(),
                "active_members": 3,
                "total_events": 4,
                "events_per_active_member": 1.33,
                "event_types": [],
            },
            "popular_articles": [
                {
                    "rank": 1,
                    "article_id": "article-1",
                    "headline": "인기 기사",
                    "domain": "news.example",
                    "published_at": NOW,
                    "view_count": 500,
                }
            ],
            "high_view_rate_articles": [
                {
                    "rank": 1,
                    "article_id": "article-high",
                    "headline": "높은 기사",
                    "domain": "news.example",
                    "published_at": NOW,
                    "view_count": 9,
                    "impression_count": 6,
                    "view_rate_percent": 150.0,
                    "first_impressed_at": NOW,
                }
            ],
            "low_view_rate_articles": [
                {
                    "rank": 1,
                    "article_id": "article-low",
                    "headline": "낮은 기사",
                    "domain": None,
                    "published_at": None,
                    "view_count": 0,
                    "impression_count": 5,
                    "view_rate_percent": 0.0,
                    "first_impressed_at": NOW,
                }
            ],
        }


class FakeArticleEmbeddingWorker:
    def __init__(self):
        self.progress_calls = []

    async def progress(self, total_articles=None):
        self.progress_calls.append(total_articles)
        return {
            "status": "ready",
            "embedded_count": 23,
            "total_count": total_articles,
            "percent": 62.2,
            "updated_at": NOW,
        }


@pytest.fixture
def ops_state(monkeypatch):
    repo = FakeOpsRepo()
    article_embeddings = FakeArticleEmbeddingWorker()
    monkeypatch.setattr(state, "repo", repo, raising=False)
    monkeypatch.setattr(
        state,
        "article_embedding_worker",
        article_embeddings,
        raising=False,
    )
    repo.article_embeddings = article_embeddings
    return repo


@pytest.mark.asyncio
async def test_ops_snapshot_fails_closed_when_api_token_is_unset(
    monkeypatch, ops_state
):
    monkeypatch.setattr(
        state, "settings", Settings(_env_file=None, API_TOKEN=""), raising=False
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/ops/api/snapshot")

    assert response.status_code == 503
    assert response.json()["detail"] == "ops_token_not_configured"


@pytest.mark.asyncio
async def test_ops_snapshot_rejects_missing_and_invalid_token(monkeypatch, ops_state):
    monkeypatch.setattr(
        state, "settings", Settings(_env_file=None, API_TOKEN="ops-secret"), raising=False
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        missing = await client.get("/ops/api/snapshot")
        invalid = await client.get(
            "/ops/api/snapshot", headers={"X-API-Token": "wrong"}
        )

    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert "ops-secret" not in missing.text + invalid.text


@pytest.mark.asyncio
async def test_ops_snapshot_returns_bounded_filtered_payload(monkeypatch, ops_state):
    monkeypatch.setattr(
        state,
        "settings",
        Settings(
            _env_file=None,
            API_TOKEN="ops-secret",
            OPS_WINDOW_MINUTES=15,
            OPS_STALE_AFTER_SECONDS=20,
        ),
        raising=False,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/ops/api/snapshot?status=dead&limit=25",
            headers={"X-API-Token": "ops-secret"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["generated_at"] == "2026-07-11T12:00:00+00:00"
    assert body["window_minutes"] == 15
    assert body["available_articles"] == 37
    assert body["article_embeddings"] == {
        "status": "ready",
        "embedded_count": 23,
        "total_count": 37,
        "percent": 62.2,
        "updated_at": "2026-07-11T12:00:00+00:00",
    }
    assert body["queue"]["stats"]["due_now"] == 1
    assert body["queue"]["items"][0]["title"] == "대기 중인 기사"
    assert [worker["kind"] for worker in body["workers"]] == [
        "ingest",
        "image",
        "llm_map",
        "retry_drain",
    ]
    assert body["push"] == {
        "enabled": False,
        "deliveries": {
            "queued": 3,
            "processing": 1,
            "sent": 11,
            "failed": 2,
            "cancelled": 4,
            "retry_attempts": 5,
        },
        "registrations": {"active": 7, "invalid": 1},
        "last_sent_at": "2026-07-11T12:00:00+00:00",
        "last_failed_at": "2026-07-11T12:00:00+00:00",
        "last_retry_at": "2026-07-11T12:00:00+00:00",
    }
    assert ops_state.queue_args == (25, "dead")
    assert ops_state.available_count_calls == 1
    assert ops_state.article_embeddings.progress_calls == [37]


@pytest.mark.asyncio
async def test_ops_snapshot_keeps_core_payload_when_article_progress_fails(
    monkeypatch, ops_state
):
    class UnavailableArticleEmbeddings:
        async def progress(self, total_articles=None):
            raise RuntimeError("vector credentials must not leak")

    monkeypatch.setattr(
        state,
        "settings",
        Settings(_env_file=None, API_TOKEN="ops-secret"),
        raising=False,
    )
    monkeypatch.setattr(
        state,
        "article_embedding_worker",
        UnavailableArticleEmbeddings(),
        raising=False,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/ops/api/snapshot",
            headers={"X-API-Token": "ops-secret"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["available_articles"] == 37
    assert body["article_embeddings"] == {
        "status": "unavailable",
        "embedded_count": None,
        "total_count": None,
        "percent": None,
        "updated_at": None,
    }
    assert "credentials" not in response.text


@pytest.mark.asyncio
async def test_ops_snapshot_exposes_disabled_article_embedding_state(
    monkeypatch, ops_state
):
    class DisabledArticleEmbeddings:
        async def progress(self, total_articles=None):
            return {
                "status": "disabled",
                "embedded_count": None,
                "total_count": None,
                "percent": None,
                "updated_at": None,
            }

    monkeypatch.setattr(
        state,
        "settings",
        Settings(_env_file=None, API_TOKEN="ops-secret"),
        raising=False,
    )
    monkeypatch.setattr(
        state,
        "article_embedding_worker",
        DisabledArticleEmbeddings(),
        raising=False,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/ops/api/snapshot",
            headers={"X-API-Token": "ops-secret"},
        )

    assert response.status_code == 200
    assert response.json()["article_embeddings"] == {
        "status": "disabled",
        "embedded_count": None,
        "total_count": None,
        "percent": None,
        "updated_at": None,
    }


@pytest.mark.asyncio
async def test_ops_snapshot_validates_filter_and_limit(monkeypatch, ops_state):
    monkeypatch.setattr(
        state, "settings", Settings(_env_file=None, API_TOKEN="ops-secret"), raising=False
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        bad_status = await client.get(
            "/ops/api/snapshot?status=unknown",
            headers={"X-API-Token": "ops-secret"},
        )
        bad_limit = await client.get(
            "/ops/api/snapshot?limit=201",
            headers={"X-API-Token": "ops-secret"},
        )

    assert bad_status.status_code == 422
    assert bad_limit.status_code == 422


@pytest.mark.asyncio
async def test_ops_analytics_is_protected_bounded_and_aggregate_only(
    monkeypatch, ops_state
):
    monkeypatch.setattr(
        state, "settings", Settings(_env_file=None, API_TOKEN="ops-secret"), raising=False
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        missing = await client.get("/ops/api/analytics")
        invalid = await client.get(
            "/ops/api/analytics?daily_days=31&monthly_months=24",
            headers={"X-API-Token": "ops-secret"},
        )
        response = await client.get(
            "/ops/api/analytics?daily_days=365&monthly_months=6",
            headers={"X-API-Token": "ops-secret"},
        )

    assert missing.status_code == 401
    assert invalid.status_code == 422
    assert response.status_code == 200
    assert ops_state.analytics_args == (365, 6)
    body = response.json()
    assert body["summary"]["total_members"] == 12
    assert body["popular_articles"][0]["rank"] == 1
    assert body["high_view_rate_articles"][0]["view_rate_percent"] == 150.0
    assert body["low_view_rate_articles"][0]["impression_count"] == 5
    serialized = response.text.lower()
    assert "account_id" not in serialized
    assert "apple_subject" not in serialized
    assert "token" not in serialized


@pytest.mark.asyncio
async def test_ops_analytics_maps_repository_failure_to_safe_503(
    monkeypatch, ops_state
):
    monkeypatch.setattr(
        state, "settings", Settings(_env_file=None, API_TOKEN="ops-secret"), raising=False
    )
    ops_state.analytics_error = RuntimeError("database password must not leak")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/ops/api/analytics",
            headers={"X-API-Token": "ops-secret"},
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "ops_analytics_unavailable"
    assert "password" not in response.text


class FakeTimelineOpsRepo:
    """Only the two reads /ops/api/timelines performs."""

    def __init__(self, *, metrics_error=None, catalog=None):
        self.metrics_error = metrics_error
        self.catalog = catalog
        self.metrics_calls = []
        self.catalog_calls = []

    async def explore_timeline_ops_metrics(self, *, candidate_days, recent_limit=8):
        self.metrics_calls.append((candidate_days, recent_limit))
        if self.metrics_error is not None:
            raise self.metrics_error
        return {
            "totals": {
                "timeline_editions": 24,
                "distinct_timelines": 11,
                "nodes": 192,
                "catalogs": 14,
                "published_catalogs": 1,
                "failed_catalogs": 2,
                "superseded_catalogs": 9,
                "pending_catalogs": 0,
                "subscriptions": 6,
            },
            "recent_catalogs": [
                {
                    "id": "catalog-1",
                    "status": "published",
                    "error_code": None,
                    "candidate_slot": "2026-07-11T12:00:00Z",
                    "timeline_count": 8,
                }
            ],
            "candidate_pool": {
                "candidate_count": 5819,
                "candidate_days": 16,
                "newest_day": NOW.date(),
                "oldest_day": NOW.date(),
            },
        }

    async def current_explore_timeline_catalog(self, *, now=None, stale_hours=72):
        self.catalog_calls.append((now, stale_hours))
        return self.catalog


def _timeline_catalog_fixture():
    return {
        "id": "catalog-1",
        "timeline_status": "fresh",
        "generated_at": NOW,
        "valid_until": NOW,
        "model": "cohere/rerank-4-pro",
        "candidate_slot": "2026-07-11T12:00:00Z",
        "cluster_slot": "2026-07-11T12:00:00Z",
        "published_timeline_count": 5,
        "timelines": [
            {
                "rank": 1,
                "stable_key": "stable-1",
                "title": "주요 사건 흐름",
                "topic": "주제",
                "started_at": NOW,
                "ended_at": NOW,
                "nodes": [
                    {
                        "lane": "main" if index <= 4 else "branch",
                        "published_at_snapshot": datetime(
                            2026, 7, 8 + index, 3, tzinfo=UTC
                        ),
                        "domain": f"news{index}.example",
                    }
                    for index in range(1, 7)
                ],
            }
        ],
    }


class FakeTimelineWorker:
    def __init__(self, status):
        self.status = status


@pytest.mark.asyncio
async def test_ops_timelines_reports_worker_totals_and_current_edition(monkeypatch):
    repo = FakeTimelineOpsRepo(catalog=_timeline_catalog_fixture())
    worker_status = {
        "enabled": True,
        "rerank_configured": True,
        "running": True,
        "started": True,
        "poll_seconds": 300.0,
        "candidate_day_span": 14,
        "candidate_limit": 500,
        "rerank_model": "cohere/rerank-4-pro",
        "last_run_at": NOW,
        "last_run": {"kind": "published", "candidates": 457, "timelines": 8},
        "last_cluster_run": {"kind": "published", "clusters": 8},
        "last_error_code": None,
    }
    monkeypatch.setattr(state, "settings", Settings(API_TOKEN="t" * 32), raising=False)
    monkeypatch.setattr(state, "repo", repo, raising=False)
    monkeypatch.setattr(
        state, "explore_timeline_worker", FakeTimelineWorker(worker_status), raising=False
    )
    monkeypatch.setattr(state, "article_embedding_worker", None, raising=False)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/ops/api/timelines", headers={"X-API-Token": "t" * 32}
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["worker"]["running"] is True
    assert body["totals"]["distinct_timelines"] == 11
    assert body["totals"]["nodes"] == 192
    assert body["current"]["readable_timeline_count"] == 1
    assert body["current"]["published_timeline_count"] == 5
    timeline = body["current"]["timelines"][0]
    assert timeline["node_count"] == 6
    assert timeline["main_node_count"] == 4
    # Four main nodes sit on four distinct KST days, which is the invariant the
    # dashboard exists to make visible.
    assert timeline["main_day_count"] == 4
    assert timeline["domain_count"] == 6
    assert repo.metrics_calls == [(30, 8)]
    assert body["cluster_snapshot"] is None


@pytest.mark.asyncio
async def test_ops_timelines_degrades_when_metrics_fail(monkeypatch):
    repo = FakeTimelineOpsRepo(
        metrics_error=RuntimeError("relation missing"), catalog=None
    )
    monkeypatch.setattr(state, "settings", Settings(API_TOKEN="t" * 32), raising=False)
    monkeypatch.setattr(state, "repo", repo, raising=False)
    monkeypatch.setattr(state, "explore_timeline_worker", None, raising=False)
    monkeypatch.setattr(state, "article_embedding_worker", None, raising=False)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/ops/api/timelines", headers={"X-API-Token": "t" * 32}
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["metrics_error"] == "timeline_metrics_unavailable"
    assert body["current"] is None
    assert body["worker"]["running"] is False


@pytest.mark.asyncio
async def test_ops_timelines_requires_the_ops_token(monkeypatch):
    monkeypatch.setattr(state, "settings", Settings(API_TOKEN="t" * 32), raising=False)
    monkeypatch.setattr(state, "repo", FakeTimelineOpsRepo(), raising=False)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/ops/api/timelines")

    assert response.status_code == 401
