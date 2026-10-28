from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.curation_audience import build_curation_audience_aggregate
from app.repository import Repository


NOW = datetime(2026, 7, 26, 12, tzinfo=UTC)
USER_1 = UUID("11111111-1111-1111-1111-111111111111")
USER_2 = UUID("22222222-2222-2222-2222-222222222222")


def _article_signal(
    account_id: UUID,
    *,
    signal_type: str,
    article_id: str,
    keyword: str,
    occurred_at: datetime = NOW,
    local_date: date | None = None,
    domain: str = "example.com",
    scene_index: int | None = None,
    scene_count: int | None = None,
):
    return {
        "account_id": account_id,
        "signal_type": signal_type,
        "occurred_at": occurred_at,
        "local_date": local_date or occurred_at.date(),
        "query": None,
        "article_id": article_id,
        "scene_index": scene_index,
        "scene_count": scene_count,
        "feedback_reason": None,
        "domain": domain,
        "map_data": {"headline": keyword, "keywords": [keyword]},
    }


def _search(account_id: UUID, query: str, *, local_date: date):
    return {
        "account_id": account_id,
        "signal_type": "search",
        "occurred_at": NOW,
        "local_date": local_date,
        "query": query,
        "article_id": None,
        "scene_index": None,
        "scene_count": None,
        "feedback_reason": None,
        "domain": None,
        "map_data": None,
    }


def test_aggregate_normalizes_each_user_before_global_average():
    rows = [
        _article_signal(
            USER_1,
            signal_type="article_opened",
            article_id=f"ai-{index}",
            keyword="인공지능",
            local_date=(NOW - timedelta(days=index)).date(),
            occurred_at=NOW - timedelta(days=index),
        )
        for index in range(10)
    ]
    rows.append(
        _article_signal(
            USER_2,
            signal_type="article_opened",
            article_id="energy-1",
            keyword="에너지",
        )
    )

    aggregate = build_curation_audience_aggregate(
        rows,
        eligible_user_count=2,
        sampled_user_count=2,
        snapshot_at=NOW,
    )
    by_name = {item.name: item for item in aggregate.interests}

    assert by_name["인공지능"].score == pytest.approx(0.5)
    assert by_name["에너지"].score == pytest.approx(0.5)
    assert by_name["인공지능"].user_count == 1
    assert by_name["에너지"].user_share == 0.5


def test_search_terms_need_two_users_and_sensitive_free_text_is_dropped():
    rows = [
        _search(USER_1, "공통관심 비밀취향", local_date=NOW.date()),
        _search(USER_2, "공통관심", local_date=NOW.date()),
        _search(USER_1, "person@example.com", local_date=(NOW - timedelta(days=1)).date()),
        _search(USER_2, "https://private.example/path", local_date=NOW.date()),
    ]

    aggregate = build_curation_audience_aggregate(
        rows,
        eligible_user_count=2,
        sampled_user_count=2,
        snapshot_at=NOW,
    )
    names = {item.name for item in aggregate.interests}

    assert "공통관심" in names
    assert "비밀취향" not in names
    assert all("example" not in name for name in names)


def test_repetition_progress_decay_saved_feedback_and_prompt_are_aggregate_only():
    rows = [
        _article_signal(
            USER_1,
            signal_type="article_opened",
            article_id="article-1",
            keyword="반도체",
        ),
        _article_signal(
            USER_1,
            signal_type="article_opened",
            article_id="article-1",
            keyword="반도체",
        ),
        _article_signal(
            USER_1,
            signal_type="scene_viewed",
            article_id="article-1",
            keyword="반도체",
            scene_index=0,
            scene_count=4,
        ),
        _article_signal(
            USER_1,
            signal_type="scene_viewed",
            article_id="article-1",
            keyword="반도체",
            scene_index=3,
            scene_count=4,
        ),
        _article_signal(
            USER_1,
            signal_type="article_completed",
            article_id="article-1",
            keyword="반도체",
        ),
        _article_signal(
            USER_2,
            signal_type="saved",
            article_id="article-2",
            keyword="전력망",
            occurred_at=NOW - timedelta(days=30),
        ),
        {
            "account_id": USER_2,
            "signal_type": "feedback",
            "occurred_at": NOW,
            "local_date": NOW.date(),
            "query": None,
            "article_id": "article-3",
            "scene_index": None,
            "scene_count": None,
            "feedback_reason": "not_relevant",
            "domain": "ignored.example",
            "map_data": {"headline": "무관", "keywords": ["무관"]},
        },
    ]

    aggregate = build_curation_audience_aggregate(
        rows,
        eligible_user_count=2,
        sampled_user_count=2,
        snapshot_at=NOW,
    )
    payload = aggregate.prompt_dict()
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)

    assert payload["engagement"]["opened_per_user"] == 0.5
    assert payload["engagement"]["progressed_per_user"] == 0.5
    assert payload["engagement"]["completed_per_user"] == 0.5
    assert payload["engagement"]["saved_per_user"] == 0.5
    assert payload["feedback"]["not_relevant"] == 0.5
    assert "account_id" not in serialized
    assert str(USER_1) not in serialized
    assert "query" not in serialized
    assert "article-1" not in serialized


def test_empty_aggregate_and_invalid_boundaries():
    aggregate = build_curation_audience_aggregate(
        [],
        eligible_user_count=0,
        sampled_user_count=0,
        snapshot_at=NOW,
    )
    assert aggregate.interests == ()
    assert aggregate.domains == ()
    assert set(aggregate.feedback.values()) == {0.0}

    with pytest.raises(ValueError):
        build_curation_audience_aggregate(
            [],
            eligible_user_count=100,
            sampled_user_count=101,
            snapshot_at=NOW,
        )
    with pytest.raises(ValueError):
        build_curation_audience_aggregate(
            [],
            eligible_user_count=0,
            sampled_user_count=0,
            snapshot_at=NOW.replace(tzinfo=None),
        )


class AudiencePool:
    def __init__(self):
        self.fetchrow_calls = []
        self.fetch_calls = []

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        return {
            "eligible_user_count": 101,
            "account_ids": [USER_1, USER_2],
        }

    async def fetch(self, query, *args):
        self.fetch_calls.append((query, args))
        return []


@pytest.mark.asyncio
async def test_repository_sampling_and_signal_queries_are_seeded_and_bounded():
    pool = AudiencePool()
    repo = Repository(SimpleNamespace(pool=pool))

    sampled = await repo.curation_sample_accounts(
        snapshot_at=NOW,
        since_days=180,
        limit=100,
        seed=42,
    )
    assert sampled == {
        "eligible_user_count": 101,
        "account_ids": [USER_1, USER_2],
    }
    sample_sql, sample_args = pool.fetchrow_calls[0]
    assert "hashtextextended(account_id::text,$3)" in sample_sql
    assert "LIMIT $4" in sample_sql
    assert sample_args[2:] == (42, 100)

    assert (
        await repo.curation_audience_signal_rows(
            account_ids=[USER_1, USER_1, USER_2],
            snapshot_at=NOW,
        )
        == []
    )
    signal_sql, signal_args = pool.fetch_calls[0]
    assert "row_number() OVER" in signal_sql
    assert "event_rank <= $4" in signal_sql
    assert "jsonb_each_text(s.saved_articles)" in signal_sql
    assert "app_article_feedback" in signal_sql
    assert signal_args[0] == [USER_1, USER_2]
    assert signal_args[3] == 500


@pytest.mark.asyncio
async def test_repository_rejects_unbounded_audience_inputs_before_db():
    pool = AudiencePool()
    repo = Repository(SimpleNamespace(pool=pool))
    with pytest.raises(ValueError):
        await repo.curation_sample_accounts(
            snapshot_at=NOW, limit=101, seed=1
        )
    with pytest.raises(ValueError):
        await repo.curation_audience_signal_rows(
            account_ids=[UUID(int=index + 1) for index in range(101)],
            snapshot_at=NOW,
        )
    with pytest.raises(ValueError):
        await repo.curation_sample_accounts(
            snapshot_at=NOW.replace(tzinfo=None), seed=1
        )
    assert pool.fetchrow_calls == []
    assert pool.fetch_calls == []
