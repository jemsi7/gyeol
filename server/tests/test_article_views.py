"""Anonymous article-view receipt and aggregate-counter contracts."""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.app_api_models import ArticleViewPayload
from app.db import SCHEMA_SQL
from app.repository import Repository


ARTICLE_A = "a" * 64
ARTICLE_B = "b" * 64


class FakeArticleViewPool:
    def __init__(self) -> None:
        self.articles = {ARTICLE_A, ARTICLE_B}
        self.receipts: dict[UUID, str] = {}
        self.counts: dict[str, int] = {}
        self.calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, query, *args):
        normalized = " ".join(query.split())
        self.calls.append((normalized, args))
        event_id, article_id = args
        if article_id not in self.articles:
            return None

        counted = event_id not in self.receipts
        if counted:
            self.receipts[event_id] = article_id
            self.counts[article_id] = self.counts.get(article_id, 0) + 1
        return {
            "article_id": article_id,
            "counted": counted,
            "view_count": self.counts.get(article_id, 0),
        }


class FakeDB:
    def __init__(self, pool: FakeArticleViewPool) -> None:
        self.pool = pool


def _device() -> dict:
    return {
        "installation_id": "11111111-1111-1111-1111-111111111111",
        "vendor_id": None,
        "platform": "ios",
        "model": "iPhone18,1",
        "os_version": "26.0",
        "app_version": "1.0",
        "build_number": "1",
        "locale": "ko-KR",
    }


def test_article_view_model_requires_uuid_sha256_and_forbids_extra_fields():
    payload = ArticleViewPayload(
        event_id=uuid4(),
        article_id=ARTICLE_A,
        device=_device(),
    )
    assert payload.article_id == ARTICLE_A

    for invalid_article_id in ("A" * 64, "a" * 63, "a" * 65):
        with pytest.raises(ValidationError):
            ArticleViewPayload(
                event_id=uuid4(),
                article_id=invalid_article_id,
                device=_device(),
            )
    with pytest.raises(ValidationError):
        ArticleViewPayload.model_validate(
            {
                "event_id": str(uuid4()),
                "article_id": ARTICLE_A,
                "device": _device(),
                "account_id": str(uuid4()),
            }
        )


def test_article_view_schema_has_anonymous_receipt_counter_backfill_and_indexes():
    receipt_table = SCHEMA_SQL.split(
        "CREATE TABLE IF NOT EXISTS article_view_events", 1
    )[1].split(");", 1)[0]
    counter_table = SCHEMA_SQL.split(
        "CREATE TABLE IF NOT EXISTS article_view_counts", 1
    )[1].split(");", 1)[0]

    assert "id          UUID PRIMARY KEY" in receipt_table
    assert "REFERENCES articles(id) ON DELETE CASCADE" in receipt_table
    assert "received_at TIMESTAMPTZ NOT NULL DEFAULT now()" in receipt_table
    assert "account_id" not in receipt_table
    assert "installation_id" not in receipt_table

    assert "view_count      BIGINT NOT NULL DEFAULT 0" in counter_table
    assert "CHECK (view_count >= 0)" in counter_table
    assert "first_viewed_at TIMESTAMPTZ" in counter_table
    assert "first_viewed_at <= last_viewed_at" in counter_table
    assert "account_id" not in counter_table
    assert "installation_id" not in counter_table

    assert "idx_article_view_events_article" in SCHEMA_SQL
    assert "ON article_view_events (article_id, received_at DESC)" in SCHEMA_SQL
    assert "WHERE event.event_type = 'article_opened'" in SCHEMA_SQL
    assert "JOIN articles article ON article.id = event.article_id" in SCHEMA_SQL
    assert "GROUP BY event.article_id" in SCHEMA_SQL
    assert "ON CONFLICT (article_id) DO NOTHING" in SCHEMA_SQL


@pytest.mark.asyncio
async def test_record_article_view_uses_target_receipt_and_atomic_counter_ctes():
    pool = FakeArticleViewPool()
    repository = Repository(FakeDB(pool))
    event_id = uuid4()

    result = await repository.record_article_view(
        event_id=event_id,
        article_id=ARTICLE_A,
    )

    assert result == {
        "article_id": ARTICLE_A,
        "counted": True,
        "view_count": 1,
    }
    assert len(pool.calls) == 1
    query, args = pool.calls[0]
    assert args == (event_id, ARTICLE_A)
    assert "WITH target AS" in query
    assert "INSERT INTO article_view_events(id, article_id)" in query
    assert "ON CONFLICT (id) DO NOTHING" in query
    assert "INSERT INTO article_view_counts(" in query
    assert "view_count=article_view_counts.view_count + 1" in query
    assert "EXISTS(SELECT 1 FROM inserted) AS counted" in query


@pytest.mark.asyncio
async def test_record_article_view_returns_first_duplicate_and_missing_rows():
    pool = FakeArticleViewPool()
    repository = Repository(FakeDB(pool))
    event_id = uuid4()

    first = await repository.record_article_view(
        event_id=event_id,
        article_id=ARTICLE_A,
    )
    duplicate = await repository.record_article_view(
        event_id=event_id,
        article_id=ARTICLE_A,
    )
    duplicate_for_other_article = await repository.record_article_view(
        event_id=event_id,
        article_id=ARTICLE_B,
    )
    missing = await repository.record_article_view(
        event_id=uuid4(),
        article_id="c" * 64,
    )

    assert first == {
        "article_id": ARTICLE_A,
        "counted": True,
        "view_count": 1,
    }
    assert duplicate == {
        "article_id": ARTICLE_A,
        "counted": False,
        "view_count": 1,
    }
    assert duplicate_for_other_article == {
        "article_id": ARTICLE_B,
        "counted": False,
        "view_count": 0,
    }
    assert missing is None
    assert pool.receipts == {event_id: ARTICLE_A}
    assert pool.counts == {ARTICLE_A: 1}
