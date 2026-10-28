from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.repository import Repository


CURSOR_AT = datetime(2026, 8, 1, 3, 0, tzinfo=UTC)


class RecordingPool:
    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, query, *args):
        self.calls.append((query, args))
        return self.rows


def _repo(pool: RecordingPool) -> Repository:
    return Repository(SimpleNamespace(pool=pool))


@pytest.mark.asyncio
async def test_c10007_change_feed_uses_publication_aware_keyset_and_bound():
    changed_at = datetime(2026, 8, 1, 3, 1, tzinfo=UTC)
    pool = RecordingPool(
        [
            {
                "article_id": "b" * 64,
                "map_status": "ok",
                "map_data": '{"headline":"publication-aware"}',
                "eligible": True,
                "changed_at": changed_at,
            }
        ]
    )

    rows = await _repo(pool).list_article_embedding_changes(
        changed_at=CURSOR_AT,
        article_id="a" * 64,
        limit=50,
    )

    assert rows == [
        {
            "article_id": "b" * 64,
            "map_status": "ok",
            "map_data": {"headline": "publication-aware"},
            "eligible": True,
            "changed_at": changed_at,
        }
    ]
    query, args = pool.calls[0]
    normalized = " ".join(query.split())
    assert "LEFT JOIN editorial_publications" in normalized
    assert (
        "GREATEST(map.updated_at, "
        "COALESCE(publication.updated_at, map.updated_at)) AS changed_at"
    ) in normalized
    assert "(changed_at, article_id) > ($1::timestamptz, $2)" in normalized
    assert "ORDER BY changed_at, article_id" in normalized
    assert "LIMIT $3" in normalized
    assert args == (CURSOR_AT, "a" * 64, 50)

    with pytest.raises(ValueError, match="timezone-aware"):
        await _repo(RecordingPool([])).list_article_embedding_changes(
            changed_at=CURSOR_AT.replace(tzinfo=None),
            article_id="",
            limit=1,
        )
    for invalid_limit in (0, 51):
        with pytest.raises(ValueError, match="limit"):
            await _repo(RecordingPool([])).list_article_embedding_changes(
                changed_at=None,
                article_id="",
                limit=invalid_limit,
            )


@pytest.mark.asyncio
async def test_c10007_change_feed_returns_ineligible_changes_for_deactivation():
    pool = RecordingPool(
        [
            {
                "article_id": "f" * 64,
                "map_status": "failed",
                "map_data": None,
                "eligible": False,
                "changed_at": CURSOR_AT,
            },
            {
                "article_id": "w" * 64,
                "map_status": "degraded",
                "map_data": '{"headline":"withdrawn"}',
                "eligible": False,
                "changed_at": CURSOR_AT,
            },
        ]
    )

    rows = await _repo(pool).list_article_embedding_changes(
        changed_at=None,
        article_id="",
        limit=2,
    )

    assert [row["article_id"] for row in rows] == ["f" * 64, "w" * 64]
    assert [row["eligible"] for row in rows] == [False, False]
    assert rows[1]["map_data"] == {"headline": "withdrawn"}
    query, args = pool.calls[0]
    normalized = " ".join(query.split())
    assert "FROM article_maps AS map" in normalized
    assert "map.status IN ('ok', 'degraded')" in normalized
    assert "map.data IS NOT NULL" in normalized
    assert (
        "publication.article_id IS NULL OR publication.state = 'published'"
        in normalized
    )
    assert args == (None, "", 2)


@pytest.mark.asyncio
async def test_c10007_change_feed_preserves_malformed_payload_for_worker_dead_letter():
    pool = RecordingPool(
        [
            {
                "article_id": "m" * 64,
                "map_status": "ok",
                "map_data": "{malformed-json",
                "eligible": True,
                "changed_at": CURSOR_AT,
            }
        ]
    )

    rows = await _repo(pool).list_article_embedding_changes(
        changed_at=None,
        article_id="",
        limit=1,
    )

    assert rows[0]["map_data"] == "{malformed-json"
    assert rows[0]["eligible"] is True
