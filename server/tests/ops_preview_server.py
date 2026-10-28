"""Local-only fixture app for browser QA of the operations dashboard."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID
from zoneinfo import ZoneInfo

from app.api import app, state
from app.config import Settings


class PreviewRepository:
    def __init__(self):
        self.daily_map_limit = 60
        self.fresh_share_percent = 75
        self.daily_run_count = 5

    def _now(self):
        return datetime.now(UTC)

    async def available_article_count(self):
        return 184

    async def worker_telemetry_rows(self, window_minutes):
        now = self._now()
        return {
            "now": now,
            "instances": [
                {
                    "instance_id": "preview-worker",
                    "capacities": {
                        "ingest": 1,
                        "image": 6,
                        "llm_map": 3,
                        "retry_drain": 1,
                    },
                    "started_at": now - timedelta(hours=2),
                    "last_seen_at": now,
                    "stopped_at": None,
                }
            ],
            "activities": [
                {
                    "instance_id": "preview-worker",
                    "worker_kind": "image",
                    "started_at": now - timedelta(minutes=2),
                    "finished_at": None,
                },
                {
                    "instance_id": "preview-worker",
                    "worker_kind": "image",
                    "started_at": now - timedelta(seconds=35),
                    "finished_at": None,
                },
                {
                    "instance_id": "preview-worker",
                    "worker_kind": "llm_map",
                    "started_at": now - timedelta(minutes=4),
                    "finished_at": None,
                },
                {
                    "instance_id": "preview-worker",
                    "worker_kind": "ingest",
                    "started_at": now - timedelta(minutes=12),
                    "finished_at": now - timedelta(minutes=6),
                },
                {
                    "instance_id": "preview-worker",
                    "worker_kind": "retry_drain",
                    "started_at": now - timedelta(minutes=9),
                    "finished_at": now - timedelta(minutes=8),
                },
            ],
        }

    async def queue_stats(self):
        return {"queued": 5, "processing": 2, "done": 18, "dead": 1, "due_now": 3}

    async def queue_items(self, limit, status=None):
        now = self._now()
        statuses = ["processing", "processing", "queued", "queued", "queued", "done", "dead", "done"]
        rows = []
        for index, item_status in enumerate(statuses):
            if status and item_status != status:
                continue
            rows.append(
                {
                    "article_id": f"{index + 1:064x}",
                    "title": [
                        "서울 도심 폭염 대응을 위한 새로운 그늘 정책",
                        "AI 반도체 공급망이 다시 움직이기 시작했다",
                        "태평양 수온 변화가 여름 기후에 미치는 영향",
                        "지역 언론의 디지털 구독 실험",
                        "전기차 충전 인프라 확충 계획",
                        "도시 공원의 야간 이용이 늘어난 이유",
                        "원문 응답 형식 검증에 실패한 기사",
                        "새로운 해양 보호 구역 지정",
                    ][index],
                    "domain": "news.example.kr" if index % 2 == 0 else "wire.example.com",
                    "status": item_status,
                    "attempts": 3 if item_status == "dead" else min(index, 2),
                    "max_attempts": 3,
                    "next_attempt_at": now + timedelta(minutes=index + 1),
                    "last_error": (
                        "OpenRouter provider timeout after 90 seconds"
                        if item_status == "queued"
                        else "format validation failed: body_markdown image reference out of range"
                        if item_status == "dead"
                        else None
                    ),
                    "created_at": now - timedelta(minutes=45 + index),
                    "updated_at": now - timedelta(seconds=15 + index * 41),
                }
            )
        return rows[:limit]

    async def push_delivery_stats(self):
        now = self._now()
        return {
            "queued": 4,
            "processing": 1,
            "sent": 128,
            "failed": 3,
            "cancelled": 7,
            "retry_attempts": 9,
            "active_registrations": 42,
            "invalid_registrations": 2,
            "last_sent_at": now - timedelta(minutes=2),
            "last_failed_at": now - timedelta(hours=4),
            "last_retry_at": now - timedelta(minutes=17),
        }

    async def map_budget_status(
        self, *, default_limit, default_share, default_run_count
    ):
        limit = self.daily_map_limit
        fresh_quota = limit * self.fresh_share_percent // 100
        backlog_quota = limit - fresh_quota
        return {
            "daily_map_limit": limit,
            "fresh_share_percent": self.fresh_share_percent,
            "daily_run_count": self.daily_run_count,
            "updated_at": self._now() - timedelta(minutes=18),
            "source": "preview",
            "editorial_day": self._now().date(),
            "current_run": 3,
            "current_run_allowance": limit // self.daily_run_count,
            "current_allowance": limit * 3 // self.daily_run_count,
            "current_remaining": max(0, limit * 3 // self.daily_run_count - 16),
            "next_run_at": self._now() + timedelta(hours=4),
            "buckets": {
                "fresh": {"quota": fresh_quota, "consumed": 12, "remaining": max(0, fresh_quota - 12)},
                "backlog": {"quota": backlog_quota, "consumed": 4, "remaining": max(0, backlog_quota - 4)},
            },
            "total_consumed": 16,
            "total_remaining": max(0, limit - 16),
            "triage": {"scored": 42, "pending": 5},
        }

    async def set_map_budget(
        self, *, daily_map_limit, fresh_share_percent, daily_run_count
    ):
        self.daily_map_limit = daily_map_limit
        self.fresh_share_percent = fresh_share_percent
        self.daily_run_count = daily_run_count

    async def last_runs(self, limit):
        now = self._now()
        return [
            {
                "id": index,
                "job_name": name,
                "query": query,
                "started_at": now - timedelta(minutes=8 * index + 2),
                "finished_at": now - timedelta(minutes=8 * index),
                "ok": index != 3,
                "fetched": 250 - index * 17,
                "mapped": 42 - index * 3,
                "maps_failed": 1 if index in {1, 3} else 0,
            }
            for index, (name, query) in enumerate(
                [
                    ("ai-technology", "artificial intelligence"),
                    ("korea-politics", '"South Korea" election'),
                    ("climate", "climate change"),
                    ("korea-domestic", "한국"),
                ],
                start=1,
            )
        ][:limit]

    async def ops_analytics(self, *, daily_days, monthly_months):
        now = self._now()
        today = now.astimezone(ZoneInfo("Asia/Seoul")).date()
        coverage_start = today - timedelta(days=72)
        user_trend = []
        cumulative = 680
        for offset in range(daily_days - 1, -1, -1):
            day = today - timedelta(days=offset)
            new_members = (day.toordinal() * 7 % 11) + (3 if day.weekday() < 5 else 0)
            cumulative += new_members
            user_trend.append(
                {
                    "date": day,
                    "new_members": new_members,
                    "cumulative_members": cumulative,
                }
            )
        dau_trend = []
        first_dau = max(today - timedelta(days=daily_days - 1), coverage_start)
        for offset in range((today - first_dau).days + 1):
            day = first_dau + timedelta(days=offset)
            active = 76 + (day.toordinal() * 13 % 46) + (12 if day.weekday() < 5 else -8)
            dau_trend.append({"date": day, "active_members": max(18, active)})

        def shift_month(month: date, delta: int) -> date:
            number = month.year * 12 + month.month - 1 + delta
            return date(number // 12, number % 12 + 1, 1)

        current_month = today.replace(day=1)
        first_month = max(shift_month(current_month, -(monthly_months - 1)), coverage_start.replace(day=1))
        mau_trend = []
        month = first_month
        while month <= current_month:
            mau_trend.append(
                {
                    "month": month,
                    "active_members": 240 + (month.month * 37 % 85),
                    "is_partial": month in {first_month, current_month},
                }
            )
            month = shift_month(month, 1)
        popular = [
            {
                "rank": rank,
                "article_id": f"preview-article-{rank:03d}",
                "headline": (
                    f"{rank}번째 인기 기사 — 기술과 사회의 변화를 오래 읽는 방법"
                    if rank % 7 else
                    f"{rank}번째 인기 기사 — 아주 긴 제목에서도 표의 열과 페이지 이동이 안정적으로 유지되는지 확인하는 예시"
                ),
                "domain": "news.example.kr" if rank % 2 else "journal.example.com",
                "published_at": now - timedelta(days=rank),
                "view_count": 48_000 - rank * 317,
            }
            for rank in range(1, 101)
        ]
        return {
            "generated_at": now,
            "timezone": "Asia/Seoul",
            "coverage": {
                "session_activity_started_on": coverage_start,
                "session_activity_through_on": today,
            },
            "summary": {
                "total_members": cumulative,
                "today_dau": dau_trend[-1]["active_members"],
                "current_month_mau": mau_trend[-1]["active_members"],
                "dau_mau_ratio": round(
                    dau_trend[-1]["active_members"] * 100
                    / mau_trend[-1]["active_members"],
                    1,
                ),
                "total_article_views": 2_847_193,
            },
            "user_trend": user_trend,
            "dau_trend": dau_trend,
            "mau_trend": mau_trend,
            "activity": {
                "period_start": first_dau,
                "period_end": today,
                "active_members": 384,
                "total_events": 18_942,
                "events_per_active_member": 49.33,
                "event_types": [
                    {"event_type": "search", "count": 2_846, "share": 15.0},
                    {"event_type": "article_opened", "count": 7_664, "share": 40.5},
                    {"event_type": "scene_viewed", "count": 6_922, "share": 36.5},
                    {"event_type": "article_completed", "count": 1_510, "share": 8.0},
                ],
            },
            "popular_articles": popular,
        }


    async def explore_timeline_ops_metrics(self, *, candidate_days, recent_limit=8):
        now = self._now()
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
                    "id": UUID("00000000-0000-4000-8000-0000000000a1"),
                    "status": "published",
                    "error_code": None,
                    "attempt_count": 1,
                    "max_attempts": 3,
                    "candidate_count": 457,
                    "candidate_slot": "2026-08-03T12:00:00Z",
                    "cluster_slot": "2026-08-03T12:00:00Z",
                    "model": "cohere/rerank-4-pro",
                    "generated_at": now - timedelta(minutes=8),
                    "valid_until": now + timedelta(hours=36),
                    "next_attempt_at": now,
                    "created_at": now - timedelta(minutes=9),
                    "updated_at": now - timedelta(minutes=8),
                    "timeline_count": 8,
                },
                {
                    "id": UUID("00000000-0000-4000-8000-0000000000a2"),
                    "status": "failed",
                    "error_code": "provider_permanent",
                    "attempt_count": 1,
                    "max_attempts": 3,
                    "candidate_count": 457,
                    "candidate_slot": "2026-08-03T08:00:00Z",
                    "cluster_slot": "2026-08-03T06:00:00Z",
                    "model": None,
                    "generated_at": None,
                    "valid_until": None,
                    "next_attempt_at": now - timedelta(hours=4),
                    "created_at": now - timedelta(hours=4),
                    "updated_at": now - timedelta(hours=4),
                    "timeline_count": 0,
                },
            ],
            "candidate_pool": {
                "candidate_count": 5819,
                "candidate_days": 16,
                "newest_day": date(2026, 8, 3),
                "oldest_day": date(2026, 7, 19),
            },
        }

    async def current_explore_timeline_catalog(self, *, now=None, stale_hours=72):
        moment = now or self._now()
        timelines = []
        for rank in range(1, 9):
            timelines.append(
                {
                    "rank": rank,
                    "stable_key": f"stable-{rank}",
                    "title": f"주요 사건 흐름 {rank}",
                    "topic": f"주제 {rank}",
                    "started_at": moment - timedelta(days=12),
                    "ended_at": moment - timedelta(days=1),
                    "nodes": [
                        {
                            "lane": "main" if index <= 4 else "branch",
                            "published_at_snapshot": moment - timedelta(days=index * 2),
                            "domain": f"news{index}.example",
                        }
                        for index in range(1, 9)
                    ],
                }
            )
        return {
            "id": UUID("00000000-0000-4000-8000-0000000000a1"),
            "timeline_status": "fresh",
            "generated_at": moment - timedelta(minutes=8),
            "valid_until": moment + timedelta(hours=36),
            "model": "cohere/rerank-4-pro",
            "candidate_slot": "2026-08-03T12:00:00Z",
            "cluster_slot": "2026-08-03T12:00:00Z",
            "published_timeline_count": 8,
            "timelines": timelines,
        }


class PreviewTimelineWorker:
    def __init__(self):
        now = datetime.now(UTC)
        self.refresh_calls = []
        self.last_cluster_run = {
            "kind": "published",
            "clusters": 8,
            "slot": "manual:2026-08-03T07:30Z",
            "algorithm_version": "mutual-knn-medoid-v2-date",
        }
        self.status = {
            "enabled": True,
            "rerank_configured": True,
            "running": True,
            "started": True,
            "poll_seconds": 300.0,
            "candidate_day_span": 14,
            "candidate_limit": 500,
            "rerank_model": "cohere/rerank-4-pro",
            "last_run_at": now - timedelta(minutes=2),
            "last_run": {
                "kind": "published",
                "candidates": 457,
                "timelines": 8,
                "published": 1,
            },
            "last_cluster_run": {
                "kind": "published",
                "candidates": 457,
                "clusters": 8,
            },
            "last_error_code": None,
        }

    async def run_once(self, *, force=False):
        self.refresh_calls.append(force)
        return {
            "kind": "published",
            "candidates": 457,
            "timelines": 8,
            "published": 1,
        }


class PreviewVectorStore:
    async def current_cluster_snapshot(self):
        now = datetime.now(UTC)
        return SimpleNamespace(
            id=UUID("00000000-0000-4000-8000-0000000000b1"),
            slot="2026-08-03T12:00:00Z",
            algorithm_version="mutual-knn-medoid-v2-date",
            article_count=457,
            indexed_count=456,
            coverage=0.9978,
            clusters=tuple(range(8)),
            generated_at=now - timedelta(minutes=10),
            valid_until=now + timedelta(hours=5),
        )


@asynccontextmanager
async def preview_lifespan(_app):
    state.settings = Settings(_env_file=None, API_TOKEN="preview-token")
    state.repo = PreviewRepository()
    state.explore_timeline_worker = PreviewTimelineWorker()
    state.timeline_rerank = SimpleNamespace(configured=True)
    state.article_embedding_worker = SimpleNamespace(vector=PreviewVectorStore())
    yield


app.router.lifespan_context = preview_lifespan
