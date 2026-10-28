from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.repository import Repository


class Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class Acquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class Pool:
    def __init__(self):
        self.calls: list[tuple[str, str, tuple]] = []
        self.retry_status = "queued"

    def acquire(self):
        return Acquire(self)

    def transaction(self):
        return Transaction()

    async def fetch(self, query, *args):
        self.calls.append(("fetch", " ".join(query.split()), args))
        return []

    async def fetchrow(self, query, *args):
        normalized = " ".join(query.split())
        self.calls.append(("fetchrow", normalized, args))
        if "AS retry_attempts" in normalized:
            now = datetime(2026, 7, 22, 10, 0, tzinfo=UTC)
            return {
                "queued": 2,
                "processing": 1,
                "sent": 8,
                "failed": 3,
                "cancelled": 4,
                "retry_attempts": 6,
                "active_registrations": 9,
                "invalid_registrations": 2,
                "last_sent_at": now,
                "last_failed_at": now,
                "last_retry_at": now,
            }
        return {"status": self.retry_status}

    async def execute(self, query, *args):
        normalized = " ".join(query.split())
        self.calls.append(("execute", normalized, args))
        if normalized.startswith("UPDATE app_push_registrations"):
            return "UPDATE 1"
        if normalized.startswith("UPDATE editorial_push_deliveries delivery"):
            return "UPDATE 2"
        return "UPDATE 1"


class DB:
    def __init__(self, pool):
        self.pool = pool


@pytest.mark.asyncio
async def test_claim_is_fifo_single_lease_and_only_active_unexpired_registration():
    pool = Pool()
    repository = Repository(DB(pool))

    assert await repository.claim_push_deliveries(limit=500, lease_seconds=60) == []

    method, query, args = pool.calls[0]
    assert method == "fetch"
    assert args == (100, 60)
    assert "FOR UPDATE OF delivery SKIP LOCKED" in query
    assert "ORDER BY delivery.next_attempt_at, delivery.created_at" in query
    assert "registration.status='active'" in query
    assert "session.expires_at > now()" in query
    assert "attempt_count=attempt_count+1" in query


@pytest.mark.asyncio
async def test_cancel_retry_complete_and_invalidate_keep_terminal_state_bounded():
    pool = Pool()
    repository = Repository(DB(pool))
    notification_id, registration_id, apns_id = uuid4(), uuid4(), uuid4()

    assert await repository.cancel_ineligible_push_deliveries() == 2
    assert await repository.complete_push_delivery(
        notification_id=notification_id,
        registration_id=registration_id,
        apns_id=apns_id,
    ) is True
    status = await repository.retry_push_delivery(
        notification_id=notification_id,
        registration_id=registration_id,
        error_code="TooManyRequests",
        transient=True,
        max_attempts=5,
        delay_seconds=20,
    )
    assert status == "queued"
    assert await repository.invalidate_push_registration(
        registration_id=registration_id,
        error_code="BadDeviceToken",
    ) is True

    all_sql = " ".join(query for _, query, _ in pool.calls)
    assert "status='cancelled'" in all_sql
    assert "status='sent'" in all_sql
    assert "attempt_count >= $5" in all_sql
    assert "status='invalid'" in all_sql
    assert "status='failed'" in all_sql
    assert "TooManyRequests" not in all_sql


@pytest.mark.asyncio
async def test_push_delivery_stats_are_aggregate_only_and_count_retries_after_first_attempt():
    pool = Pool()
    repository = Repository(DB(pool))

    stats = await repository.push_delivery_stats()

    assert stats["sent"] == 8
    assert stats["retry_attempts"] == 6
    assert stats["failed"] == 3
    assert stats["active_registrations"] == 9
    assert stats["invalid_registrations"] == 2
    method, query, args = pool.calls[0]
    assert method == "fetchrow"
    assert args == ()
    assert "greatest(delivery.attempt_count - 1, 0)" in query
    assert "delivery.status='queued' AND delivery.attempt_count > 0" in query
    assert "AS last_sent_at" in query
    assert "AS last_failed_at" in query
    assert "AS last_retry_at" in query
    for sensitive_column in (
        "token_hash",
        "token_ciphertext",
        "headline",
        "article_id",
        "account_id",
    ):
        assert sensitive_column not in query


@pytest.mark.asyncio
async def test_timeline_claim_is_fifo_leased_and_rechecks_subscription_preference_session():
    pool = Pool()
    repository = Repository(DB(pool))

    assert await repository.claim_timeline_push_deliveries(
        limit=500, lease_seconds=60
    ) == []

    method, query, args = pool.calls[0]
    assert method == "fetch"
    assert args == (100, 60)
    assert "FOR UPDATE OF delivery SKIP LOCKED" in query
    assert "app_timeline_subscriptions" in query
    assert "notification.update_kind='inserted'" in query
    assert "registration.timeline_refreshes_enabled" in query
    assert "notification.update_kind='occurred'" in query
    assert "registration.timeline_additions_enabled" in query
    assert "session.expires_at > now()" in query
    assert "attempt_count=attempt_count+1" in query


@pytest.mark.asyncio
async def test_timeline_cancel_complete_retry_and_invalidation_share_terminal_rules():
    pool = Pool()
    repository = Repository(DB(pool))
    notification_id, registration_id, apns_id = uuid4(), uuid4(), uuid4()

    assert await repository.cancel_ineligible_timeline_push_deliveries() == 1
    assert await repository.complete_timeline_push_delivery(
        notification_id=notification_id,
        registration_id=registration_id,
        apns_id=apns_id,
    ) is True
    assert await repository.retry_timeline_push_delivery(
        notification_id=notification_id,
        registration_id=registration_id,
        error_code="TooManyRequests",
        transient=True,
        max_attempts=5,
        delay_seconds=20,
    ) == "queued"
    assert await repository.invalidate_push_registration(
        registration_id=registration_id,
        error_code="BadDeviceToken",
    ) is True

    sql = " ".join(query for _, query, _ in pool.calls)
    assert "UPDATE explore_timeline_push_deliveries delivery" in sql
    assert "status='cancelled'" in sql
    assert "UPDATE explore_timeline_push_deliveries SET status='sent'" in sql
    assert "attempt_count >= $5" in sql
    assert "subscription.timeline_stable_key" in sql
