from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.repository import Repository


class RecordingPool:
    def __init__(self, rows):
        self.rows = rows
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, query, *args):
        self.calls.append((query, args))
        return self.rows


def _repo(pool: RecordingPool) -> Repository:
    return Repository(SimpleNamespace(pool=pool))


@pytest.mark.asyncio
async def test_image_discovery_uses_downloaded_at_id_keyset_and_bound():
    now = datetime(2026, 7, 19, tzinfo=timezone.utc)
    pool = RecordingPool([{"id": "b", "downloaded_at": now}])

    rows = await _repo(pool).list_images_after(
        downloaded_at=now,
        image_id="a",
        limit=99,
    )

    assert rows == [{"id": "b", "downloaded_at": now}]
    query, args = pool.calls[0]
    assert "downloaded_at > $1" in query
    assert "downloaded_at = $1 AND id > $2" in query
    assert "ORDER BY downloaded_at, id" in query
    assert args == (now, "a", 50)


@pytest.mark.asyncio
async def test_candidate_lookup_excludes_any_current_link_and_selects_one_origin():
    pool = RecordingPool([])

    rows = await _repo(pool).rag_candidate_records(
        ["image-b", "image-a", "image-b"],
        exclude_article_id="current-article",
    )

    assert rows == []
    query, args = pool.calls[0]
    assert "JOIN LATERAL" in query
    assert "a.id <> $2" in query
    assert "NOT EXISTS" in query
    assert "current_link.article_id = $2" in query
    assert "array_position($1::text[], i.id)" in query
    assert args == (["image-b", "image-a"], "current-article")
