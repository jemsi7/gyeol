"""Retention for candidates the daily Map budget never reached."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.repository import Repository


class RecordingPool:
    def __init__(self, value=0):
        self.value = value
        self.calls: list[tuple[str, tuple]] = []

    async def fetchval(self, query, *args):
        self.calls.append((query, args))
        return self.value


def _repo(pool) -> Repository:
    return Repository(SimpleNamespace(pool=pool))


@pytest.mark.asyncio
async def test_purge_targets_only_expired_never_mapped_candidates():
    pool = RecordingPool(value=4)

    result = await _repo(pool).purge_expired_candidates(ttl_days=3, limit=500)

    assert result == {"deleted": 4, "limit": 500}
    query, args = pool.calls[0]
    assert args == (3.0, 500)
    assert "q.status = 'queued'" in query
    assert "interval '1 day'" in query
    assert "DELETE FROM articles" in query
    assert "ORDER BY a.first_seen_at ASC" in query


@pytest.mark.asyncio
async def test_purge_protects_mapped_published_curated_and_user_referenced_rows():
    pool = RecordingPool()

    await _repo(pool).purge_expired_candidates(ttl_days=3, limit=10)

    query, _args = pool.calls[0]
    for table in (
        "article_maps",
        "editorial_publications",
        "article_curation_items",
        "app_article_feedback",
        "app_account_reading_states",
    ):
        assert f"FROM {table}" in query
    assert "m.status IN ('ok', 'degraded')" in query
    assert "s.saved_articles ? a.id" in query
    assert query.count("NOT EXISTS") == 5


@pytest.mark.asyncio
async def test_purge_rejects_invalid_bounds_before_touching_the_database():
    pool = RecordingPool()
    repo = _repo(pool)

    with pytest.raises(ValueError, match="ttl_days"):
        await repo.purge_expired_candidates(ttl_days=0, limit=10)
    with pytest.raises(ValueError, match="limit"):
        await repo.purge_expired_candidates(ttl_days=3, limit=0)

    assert pool.calls == []


@pytest.mark.asyncio
async def test_pending_rows_and_score_writes_stay_bounded():
    class Pool(RecordingPool):
        def __init__(self):
            super().__init__(value=2)
            self.fetches: list[tuple[str, tuple]] = []

        async def fetch(self, query, *args):
            self.fetches.append((query, args))
            return []

    pool = Pool()
    repo = _repo(pool)

    await repo.triage_pending_rows(limit=30, max_attempts=3)
    query, args = pool.fetches[0]
    assert args == (30, 3, 21600.0, None)
    assert "q.triage_scored_at IS NULL" in query
    assert "q.triage_attempts < $2" in query
    assert "q.updated_at <= now()" in query
    assert "a.coverage_domain = 'crypto'" in query

    saved = await repo.save_triage_scores(
        [{"article_id": "a", "score": 70, "signals": {"penalty": 0}}],
        model="cheap/model-1",
    )
    assert saved == 2
    query, args = pool.calls[0]
    assert args[0] == ["a"]
    assert args[1] == [70]
    assert args[3] == "cheap/model-1"

    with pytest.raises(ValueError, match="score"):
        await repo.save_triage_scores(
            [{"article_id": "a", "score": 101, "signals": {}}],
            model="cheap/model-1",
        )


@pytest.mark.asyncio
async def test_duplicate_lookup_requires_aligned_inputs():
    class Pool(RecordingPool):
        async def fetch(self, query, *args):
            self.calls.append((query, args))
            return [{"id": "b"}]

    pool = Pool()
    repo = _repo(pool)

    with pytest.raises(ValueError, match="align"):
        await repo.duplicate_title_ids(
            article_ids=["a"], title_keys=[], window_hours=48.0
        )
    with pytest.raises(ValueError, match="window_hours"):
        await repo.duplicate_title_ids(
            article_ids=["a"], title_keys=["k"], window_hours=0
        )
    assert await repo.duplicate_title_ids(
        article_ids=[], title_keys=[], window_hours=48.0
    ) == set()

    duplicates = await repo.duplicate_title_ids(
        article_ids=["a", "b"], title_keys=["k", "k"], window_hours=48.0
    )
    assert duplicates == {"b"}
    query, args = pool.calls[0]
    assert "earlier.first_seen_at < self_row.first_seen_at" in query
    assert args[2] == 48.0
