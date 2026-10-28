from __future__ import annotations

from uuid import uuid4

import pytest

from app.db import SCHEMA_SQL
from app.repository import Repository


class NoPoolDB:
    pool = None


class OutboxConnection:
    def __init__(self, *, inserted: bool = True, recipients: int = 2):
        self.inserted = inserted
        self.recipients = recipients
        self.fetchrow_calls: list[tuple[str, tuple]] = []
        self.fetchval_calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((" ".join(query.split()), args))
        return {"id": args[0]} if self.inserted else None

    async def fetchval(self, query, *args):
        self.fetchval_calls.append((" ".join(query.split()), args))
        return self.recipients


class PreferencePool:
    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, query, *args):
        self.calls.append((" ".join(query.split()), args))
        return {
            "installation_id": args[1],
            "environment": "development",
            "editorial_enabled": args[2],
            "weekly_insight_enabled": args[3],
        }


class PreferenceDB:
    def __init__(self, pool: PreferencePool):
        self.pool = pool


@pytest.mark.asyncio
async def test_first_publish_snapshots_only_active_unexpired_recipients_with_bounded_copy():
    repository = Repository(NoPoolDB())
    connection = OutboxConnection(recipients=3)
    notification_id, draft_id, revision_id = uuid4(), uuid4(), uuid4()
    headline = "  새\n   에디토리얼 " + "가" * 300
    subheadline = "  현장\n 취재의   핵심 " + "나" * 300

    count = await repository._enqueue_first_editorial_push(
        connection,
        notification_id=notification_id,
        draft_id=draft_id,
        article_id="article-id",
        revision_id=revision_id,
        revision_number=1,
        document={
            "headline": headline,
            "subheadline": subheadline,
            "summary": "fallback",
        },
    )

    assert count == 3
    notification_args = connection.fetchrow_calls[0][1]
    assert notification_args[:4] == (
        notification_id,
        draft_id,
        "article-id",
        revision_id,
    )
    assert notification_args[4] == "editorial"
    assert notification_args[5].startswith("새 에디토리얼")
    assert "\n" not in notification_args[5]
    assert len(notification_args[5]) == 200
    assert notification_args[6].startswith("현장 취재의 핵심")
    assert len(notification_args[6]) == 240
    delivery_sql = connection.fetchval_calls[0][0]
    assert "r.status='active'" in delivery_sql
    assert "s.expires_at > now()" in delivery_sql
    assert "r.editorial_enabled" in delivery_sql
    assert "r.weekly_insight_enabled" in delivery_sql
    assert "INSERT INTO editorial_push_deliveries" in delivery_sql


@pytest.mark.asyncio
async def test_republish_and_duplicate_first_publish_do_not_duplicate_deliveries():
    repository = Repository(NoPoolDB())
    revision_two = OutboxConnection()
    count = await repository._enqueue_first_editorial_push(
        revision_two,
        notification_id=uuid4(),
        draft_id=uuid4(),
        article_id="article-id",
        revision_id=uuid4(),
        revision_number=2,
        document={"headline": "수정 발행"},
    )
    assert count == 0
    assert revision_two.fetchrow_calls == revision_two.fetchval_calls == []

    duplicate = OutboxConnection(inserted=False)
    count = await repository._enqueue_first_editorial_push(
        duplicate,
        notification_id=uuid4(),
        draft_id=uuid4(),
        article_id="article-id",
        revision_id=uuid4(),
        revision_number=1,
        document={"headline": "첫 발행", "subheadline": "", "summary": "요약 본문"},
    )
    assert count == 0
    assert duplicate.fetchval_calls == []
    assert duplicate.fetchrow_calls[0][1][6] == "요약 본문"


@pytest.mark.asyncio
async def test_weekly_insight_outbox_persists_kind_and_filters_matching_preference():
    repository = Repository(NoPoolDB())
    connection = OutboxConnection(recipients=2)

    count = await repository._enqueue_first_editorial_push(
        connection,
        notification_id=uuid4(),
        draft_id=uuid4(),
        article_id="weekly-article",
        revision_id=uuid4(),
        revision_number=1,
        document={
            "publication_kind": "weekly_insight",
            "headline": "이번 주의 핵심",
            "summary": "한 주를 정리합니다",
        },
    )

    assert count == 2
    assert connection.fetchrow_calls[0][1][4] == "weekly_insight"
    delivery_sql = connection.fetchval_calls[0][0]
    assert "n.publication_kind='weekly_insight'" in delivery_sql
    assert "r.weekly_insight_enabled" in delivery_sql


@pytest.mark.asyncio
async def test_preference_update_is_account_scoped_and_cancels_disabled_kind_deliveries():
    pool = PreferencePool()
    repository = Repository(PreferenceDB(pool))
    account_id = uuid4()

    result = await repository.update_push_preferences(
        account_id=account_id,
        installation_id="11111111-1111-1111-1111-111111111111",
        editorial_enabled=False,
        weekly_insight_enabled=True,
    )

    assert result == {
        "installation_id": "11111111-1111-1111-1111-111111111111",
        "environment": "development",
        "editorial_enabled": False,
        "weekly_insight_enabled": True,
    }
    query, args = pool.calls[0]
    assert args == (
        account_id,
        "11111111-1111-1111-1111-111111111111",
        False,
        True,
        False,
        False,
        False,
    )
    assert "WHERE account_id=$1 AND installation_id=$2" in query
    assert "delivery.status IN ('queued','processing')" in query
    assert "last_error='preference_disabled'" in query
    assert "NOT updated.editorial_enabled" in query
    assert "NOT updated.weekly_insight_enabled" in query
    assert "NOT updated.timeline_refreshes_enabled" in query
    assert "NOT updated.timeline_additions_enabled" in query


def test_editorial_push_outbox_schema_is_durable_and_token_free():
    assert "CREATE TABLE IF NOT EXISTS editorial_push_notifications" in SCHEMA_SQL
    assert "draft_id    UUID NOT NULL UNIQUE" in SCHEMA_SQL
    assert "CREATE TABLE IF NOT EXISTS editorial_push_deliveries" in SCHEMA_SQL
    assert "PRIMARY KEY (notification_id, registration_id)" in SCHEMA_SQL
    assert "idx_editorial_push_deliveries_due" in SCHEMA_SQL
    assert "publication_kind TEXT NOT NULL DEFAULT 'editorial'" in SCHEMA_SQL
    assert "editorial_enabled BOOLEAN NOT NULL DEFAULT TRUE" in SCHEMA_SQL
    assert "weekly_insight_enabled BOOLEAN NOT NULL DEFAULT FALSE" in SCHEMA_SQL
    section = SCHEMA_SQL.split("CREATE TABLE IF NOT EXISTS editorial_push_notifications", 1)[1]
    assert "token_ciphertext" not in section
    assert "device_token" not in section
