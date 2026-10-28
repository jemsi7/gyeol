from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.repository import Repository


NOW = datetime(2026, 8, 2, 12, tzinfo=UTC)


class RecordingConnection:
    def __init__(self, *, existing=False, count=0, timeline_exists=True):
        self.existing = existing
        self.count = count
        self.timeline_exists = timeline_exists
        self.calls: list[tuple[str, tuple]] = []

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def fetchrow(self, query, *args):
        normalized = " ".join(query.split())
        self.calls.append((normalized, args))
        if "FROM app_accounts" in normalized and "FOR UPDATE" in normalized:
            return {"id": args[0]}
        if normalized.startswith("SELECT timeline_stable_key"):
            if self.existing:
                return {
                    "timeline_stable_key": args[1],
                    "created_at": NOW,
                    "updated_at": NOW,
                }
            return None
        if "INSERT INTO app_timeline_subscriptions" in normalized:
            if not self.timeline_exists:
                return None
            return {
                "timeline_stable_key": args[1],
                "created_at": NOW,
                "updated_at": NOW,
            }
        return None

    async def fetchval(self, query, *args):
        normalized = " ".join(query.split())
        self.calls.append((normalized, args))
        return self.count

    async def fetch(self, query, *args):
        normalized = " ".join(query.split())
        self.calls.append((normalized, args))
        return []

    async def execute(self, query, *args):
        normalized = " ".join(query.split())
        self.calls.append((normalized, args))
        if normalized.startswith("DELETE FROM app_timeline_subscriptions"):
            return "DELETE 1"
        return "UPDATE 1"


class Pool:
    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection

    async def fetch(self, query, *args):
        return await self.connection.fetch(query, *args)


def repo(connection) -> Repository:
    return Repository(SimpleNamespace(pool=Pool(connection)))


@pytest.mark.asyncio
async def test_subscription_upsert_locks_account_accepts_current_or_grace_and_is_idempotent():
    account_id = uuid4()
    connection = RecordingConnection(existing=True, count=64)

    row = await repo(connection).upsert_timeline_subscription(
        account_id=account_id,
        stable_key="cluster:policy-and-market-2026",
        stale_hours=72,
        max_subscriptions=64,
    )

    assert row["timeline_stable_key"] == "cluster:policy-and-market-2026"
    sql = " ".join(query for query, _ in connection.calls)
    assert "FROM app_accounts" in sql and "FOR UPDATE" in sql
    assert "INSERT INTO app_timeline_subscriptions" not in sql

    fresh = RecordingConnection(count=0, timeline_exists=True)
    inserted = await repo(fresh).upsert_timeline_subscription(
        account_id=account_id,
        stable_key="cluster:policy-and-market-2026",
        stale_hours=72,
        max_subscriptions=64,
    )
    assert inserted["timeline_stable_key"] == "cluster:policy-and-market-2026"
    insert_sql = next(
        query for query, _ in fresh.calls
        if "INSERT INTO app_timeline_subscriptions" in query
    )
    assert "valid_until > now()-$3::interval" in insert_sql
    event_sql = next(
        query
        for query, _ in fresh.calls
        if "INSERT INTO app_timeline_subscription_events" in query
    )
    assert "'subscribe'" in event_sql


@pytest.mark.asyncio
async def test_subscription_upsert_enforces_64_limit_and_current_catalog_existence():
    account_id = uuid4()
    limited = RecordingConnection(count=64)
    with pytest.raises(OverflowError):
        await repo(limited).upsert_timeline_subscription(
            account_id=account_id,
            stable_key="valid-key",
            max_subscriptions=64,
        )

    missing = RecordingConnection(count=0, timeline_exists=False)
    assert await repo(missing).upsert_timeline_subscription(
        account_id=account_id,
        stable_key="missing-key",
        stale_hours=72,
    ) is None
    insert_sql = next(
        query for query, _ in missing.calls
        if "INSERT INTO app_timeline_subscriptions" in query
    )
    assert "valid_until > now()-$3::interval" in insert_sql
    assert "status='published'" in insert_sql


@pytest.mark.asyncio
async def test_unsubscribe_is_atomic_and_cancels_pending_timeline_deliveries():
    connection = RecordingConnection()
    deleted = await repo(connection).delete_timeline_subscription(
        account_id=uuid4(), stable_key="valid-key"
    )

    assert deleted is True
    sql = " ".join(query for query, _ in connection.calls)
    assert "DELETE FROM app_timeline_subscriptions" in sql
    assert "UPDATE explore_timeline_push_deliveries" in sql
    assert "subscription_disabled" in sql
    assert "status IN ('queued','processing')" in sql
    assert "INSERT INTO app_timeline_subscription_events" in sql
    assert "'unsubscribe'" in sql


@pytest.mark.asyncio
async def test_subscription_history_joins_latest_metadata_and_current_state():
    connection = RecordingConnection()

    history = await repo(connection).timeline_subscription_history(uuid4(), limit=100)

    assert history == []
    sql, args = connection.calls[-1]
    assert "FROM app_timeline_subscription_events event" in sql
    assert "catalog.status IN ('published','superseded')" in sql
    assert "is_currently_subscribed" in sql
    assert "ORDER BY event.occurred_at DESC,event.id DESC" in sql
    assert args[1] == 100
