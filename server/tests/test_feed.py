"""Feed cursor, keyset query and absolute map resolution tests."""
from datetime import UTC, date, datetime, timedelta

import pytest

from app.config import Settings
from app.feed import (
    FeedCursor,
    InvalidFeedCursor,
    build_feed_page,
    decode_cursor,
    encode_cursor,
    feed_order_key,
    seoul_editorial_day,
)
from app.repository import Repository


ARTICLE_A = "a" * 64
ARTICLE_B = "b" * 64


def _map():
    return {
        "format_version": 1,
        "language": "ko",
        "headline": "제목",
        "subheadline": "",
        "summary": "요약입니다.",
        "keywords": ["뉴스"],
        "body_markdown": "첫 문단\n<img:0>\n다음 문단",
        "image_placements": [{"ref": 0, "caption": "현장 사진"}],
        "images": [
            {
                "ref": 0,
                "image_id": "image-hash",
                "source_url": "https://origin.example/image.jpg",
                "role": "content",
            }
        ],
    }


def _row(article_id=ARTICLE_A, sort_at=None):
    sort_at = sort_at or datetime(2026, 7, 10, 10, 0, tzinfo=UTC)
    return {
        "article_id": article_id,
        "source_url": "https://news.example/article",
        "domain": "news.example",
        "published_at": sort_at,
        "source_language": "Korean",
        "sourcecountry": "South Korea",
        "sort_at": sort_at,
        "status": "ok",
        "data": _map(),
    }


def test_cursor_roundtrip_and_invalid_cursor():
    original = FeedCursor(
        sort_at=datetime(2026, 7, 10, 10, 30, 45, 123000, tzinfo=UTC),
        article_id=ARTICLE_A,
    )
    assert decode_cursor(encode_cursor(original)) == original
    for invalid in ("", "not-base64", "e30", "x" * 1025):
        with pytest.raises(InvalidFeedCursor):
            decode_cursor(invalid)


def test_v2_cursor_roundtrip_and_seoul_day_is_anchored_across_midnight():
    before_midnight = datetime(2026, 7, 19, 14, 59, tzinfo=UTC)
    after_midnight = datetime(2026, 7, 19, 15, 0, tzinfo=UTC)
    assert seoul_editorial_day(before_midnight) == date(2026, 7, 19)
    assert seoul_editorial_day(after_midnight) == date(2026, 7, 20)

    original = FeedCursor(
        sort_at=before_midnight,
        article_id=ARTICLE_A,
        editorial_day=date(2026, 7, 19),
        priority_bucket=0,
        featured_bucket=1,
        order_bucket=0,
        display_order=3,
    )
    assert decode_cursor(encode_cursor(original)) == original


def test_feed_page_has_absolute_images_and_limit_5():
    now = datetime(2026, 7, 10, 10, 0, tzinfo=UTC)
    rows = [
        _row(f"{index:064x}", now - timedelta(seconds=index))
        for index in range(6)
    ]
    items, cursor = build_feed_page(
        rows,
        limit=5,
        settings=Settings(IMAGE_BACKEND="db", PUBLIC_BASE_URL="https://api.example"),
        request_base_url="http://ignored.test/",
    )
    assert len(items) == 5
    assert items[0]["map"]["images"][0]["url"] == (
        "https://api.example/images/image-hash"
    )
    assert "![현장 사진](https://api.example/images/image-hash)" in items[0][
        "map"
    ]["body_resolved"]
    decoded = decode_cursor(cursor)
    assert decoded.article_id == rows[4]["article_id"]


def test_feed_page_always_uses_stable_app_image_url():
    items, _ = build_feed_page(
        [_row()],
        limit=5,
        settings=Settings(
            IMAGE_BACKEND="s3",
            PUBLIC_BASE_URL="https://api.example",
        ),
        request_base_url="http://ignored.test/",
    )
    assert items[0]["map"]["images"][0]["url"] == (
        "https://api.example/images/image-hash"
    )


class FakePool:
    def __init__(self, rows):
        self.rows = rows
        self.query = None
        self.args = None

    async def fetch(self, query, *args):
        self.query = query
        self.args = args
        return self.rows


class FakeDB:
    def __init__(self, rows):
        self.pool = FakePool(rows)


@pytest.mark.asyncio
async def test_repository_uses_keyset_status_filter_and_limit_plus_one():
    db = FakeDB([_row(ARTICLE_A), _row(ARTICLE_B)])
    repo = Repository(db)
    cursor_time = datetime(2026, 7, 10, tzinfo=UTC)
    rows = await repo.recent_article_maps(
        limit=6,
        cursor_sort_at=cursor_time,
        cursor_article_id=ARTICLE_A,
        editorial_day=date(2026, 7, 10),
    )
    assert len(rows) == 2
    assert "m.status IN ('ok', 'degraded')" in db.pool.query
    assert "LEFT JOIN editorial_publications" in db.pool.query
    assert "p.state='published'" in db.pool.query
    assert "ORDER BY priority_bucket ASC" in db.pool.query
    assert "article_id < $7" in db.pool.query
    assert db.pool.args == (
        date(2026, 7, 10),
        1,
        1,
        1,
        2_147_483_647,
        cursor_time,
        ARTICLE_A,
        6,
    )


@pytest.mark.asyncio
async def test_repository_random_feed_filters_public_maps_and_excluded_ids():
    db = FakeDB([_row(ARTICLE_B)])
    repo = Repository(db)

    rows = await repo.random_article_maps(
        limit=5,
        excluded_article_ids=[ARTICLE_A],
        random_seed="seed-123",
    )

    assert len(rows) == 1
    assert "m.status IN ('ok', 'degraded')" in db.pool.query
    assert "p.state='published'" in db.pool.query
    assert "NOT (a.id = ANY($1::text[]))" in db.pool.query
    assert "ORDER BY md5(a.id || $2)" in db.pool.query
    assert db.pool.args == ([ARTICLE_A], "seed-123", 5)


@pytest.mark.asyncio
async def test_latest_repository_uses_service_listing_order_and_exclusions():
    db = FakeDB([_row(ARTICLE_B)])
    repo = Repository(db)

    rows = await repo.latest_article_maps(
        limit=5,
        excluded_article_ids=[ARTICLE_A],
    )

    assert len(rows) == 1
    assert "COALESCE(p.published_at, m.created_at) AS listed_at" in db.pool.query
    assert "NOT (a.id = ANY($1::text[]))" in db.pool.query
    assert "ORDER BY listed_at DESC, a.id DESC" in db.pool.query
    assert "a.seendate IS NOT NULL" not in db.pool.query
    assert "a.seendate <= now()" not in db.pool.query
    assert db.pool.args == ([ARTICLE_A], 5)


def test_v2_page_cursor_carries_manual_priority_and_no_duplicate_boundary():
    now = datetime(2026, 7, 19, 8, 0, tzinfo=UTC)
    rows = []
    for index in range(7):
        row = _row(f"{index:064x}", now - timedelta(minutes=index))
        row.update(
            {
                "editorial_day": date(2026, 7, 19),
                "priority_bucket": 0 if index < 3 else 1,
                "featured_bucket": 0 if index == 0 else 1,
                "order_bucket": 0 if index in {1, 2} else 1,
                "cursor_display_order": index - 1 if index in {1, 2} else 2_147_483_647,
            }
        )
        rows.append(row)

    first_items, raw_cursor = build_feed_page(
        rows[:6],
        limit=5,
        settings=Settings(_env_file=None, PUBLIC_BASE_URL="https://api.example"),
        request_base_url="http://ignored.test",
        editorial_day=date(2026, 7, 19),
    )
    cursor = decode_cursor(raw_cursor)
    assert cursor is not None
    assert cursor.editorial_day == date(2026, 7, 19)
    assert cursor.priority_bucket == rows[4]["priority_bucket"]
    assert cursor.display_order == rows[4]["cursor_display_order"]

    second_items, second_cursor = build_feed_page(
        rows[5:],
        limit=5,
        settings=Settings(_env_file=None, PUBLIC_BASE_URL="https://api.example"),
        request_base_url="http://ignored.test",
        editorial_day=cursor.editorial_day,
    )
    first_ids = {item["article_id"] for item in first_items}
    second_ids = {item["article_id"] for item in second_items}
    assert first_ids.isdisjoint(second_ids)
    assert len(first_ids | second_ids) == 7
    assert second_cursor is None


def test_manual_default_reorder_and_featured_keys_precede_automatic():
    now = datetime(2026, 7, 19, 8, 0, tzinfo=UTC)
    automatic = _row("f" * 64, now + timedelta(days=1))
    newest_manual = _row("a" * 64, now)
    older_manual = _row("b" * 64, now - timedelta(hours=1))
    for row in (newest_manual, older_manual):
        row.update(
            priority_bucket=0,
            featured_bucket=1,
            order_bucket=1,
            cursor_display_order=2_147_483_647,
        )
    assert [row["article_id"] for row in sorted(
        [automatic, older_manual, newest_manual], key=feed_order_key
    )] == [newest_manual["article_id"], older_manual["article_id"], automatic["article_id"]]

    older_manual.update(
        featured_bucket=0,
        order_bucket=0,
        cursor_display_order=1,
    )
    newest_manual.update(order_bucket=0, cursor_display_order=0)
    assert [row["article_id"] for row in sorted(
        [automatic, newest_manual, older_manual], key=feed_order_key
    )] == [older_manual["article_id"], newest_manual["article_id"], automatic["article_id"]]
