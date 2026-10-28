from __future__ import annotations

import base64
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet

from app.apns import APNsResult
from app.apple_identity import TokenCipher
from app.config import Settings
from app.push_worker import EditorialPushWorker


class FakeProvider:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    async def send(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return APNsResult(outcome, kwargs["apns_id"], f"code_{outcome}")

    async def send_timeline_update(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return APNsResult(outcome, kwargs["apns_id"], f"code_{outcome}")


class FakeRepo:
    def __init__(self, rows, timeline_rows=None):
        self.rows = rows
        self.timeline_rows = list(timeline_rows or [])
        self.completed: list[dict] = []
        self.retried: list[dict] = []
        self.invalidated: list[dict] = []
        self.claims: list[dict] = []

    async def cancel_ineligible_push_deliveries(self):
        return 2

    async def cancel_ineligible_timeline_push_deliveries(self):
        return 0

    async def claim_push_deliveries(self, **kwargs):
        self.claims.append(kwargs)
        rows, self.rows = self.rows, []
        return rows

    async def claim_timeline_push_deliveries(self, **kwargs):
        self.claims.append(kwargs)
        rows, self.timeline_rows = self.timeline_rows, []
        return rows[: kwargs["limit"]]

    async def complete_push_delivery(self, **kwargs):
        self.completed.append(kwargs)
        return True

    async def complete_timeline_push_delivery(self, **kwargs):
        self.completed.append({**kwargs, "timeline": True})
        return True

    async def retry_push_delivery(self, **kwargs):
        self.retried.append(kwargs)
        return "queued" if kwargs["transient"] and kwargs["max_attempts"] > 1 else "failed"

    async def retry_timeline_push_delivery(self, **kwargs):
        self.retried.append({**kwargs, "timeline": True})
        return "queued" if kwargs["transient"] and kwargs["max_attempts"] > 1 else "failed"

    async def invalidate_push_registration(self, **kwargs):
        self.invalidated.append(kwargs)
        return True


def _row(cipher: TokenCipher, *, attempt_count: int = 1, valid=True):
    token = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode()
    return {
        "notification_id": uuid4(),
        "registration_id": uuid4(),
        "attempt_count": attempt_count,
        "headline": "새 에디토리얼",
        "body": "본문",
        "article_id": "article-id",
        "publication_kind": "editorial",
        "token_ciphertext": cipher.encrypt(token) if valid else b"invalid-ciphertext",
        "environment": "development",
    }


def _timeline_row(cipher: TokenCipher, *, update_kind="inserted"):
    token = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode()
    notification_id = uuid4()
    return {
        "notification_id": notification_id,
        "event_id": notification_id,
        "registration_id": uuid4(),
        "attempt_count": 1,
        "timeline_stable_key": "cluster:policy-and-market-2026",
        "node_id": uuid4(),
        "update_kind": update_kind,
        "token_ciphertext": cipher.encrypt(token),
        "environment": "development",
    }


@pytest.mark.asyncio
async def test_worker_isolates_sent_retry_permanent_invalid_and_decrypt_failure():
    key = Fernet.generate_key().decode()
    cipher = TokenCipher(key)
    rows = [_row(cipher) for _ in range(4)] + [_row(cipher, valid=False)]
    provider = FakeProvider(["sent", "transient", "permanent", "invalid_token"])
    repository = FakeRepo(rows)
    worker = EditorialPushWorker(
        Settings(
            _env_file=None,
            PUSH_NOTIFICATIONS_ENABLED=True,
            PUSH_MAX_ATTEMPTS=5,
        ),
        repository,
        provider,
        cipher,
    )

    result = await worker.run_once()

    assert result == {
        "claimed": 5,
        "sent": 1,
        "retried": 1,
        "failed": 1,
        "invalid": 2,
        "cancelled": 2,
        "skipped": 0,
    }
    assert len(repository.completed) == 1
    assert [call["transient"] for call in repository.retried] == [True, False]
    assert repository.retried[0]["delay_seconds"] == 5.0
    assert len(repository.invalidated) == 2
    assert provider.calls[0]["device_token"] == bytes(range(32))
    assert isinstance(provider.calls[0]["apns_id"], UUID)
    assert provider.calls[0]["publication_kind"] == "editorial"


@pytest.mark.asyncio
async def test_worker_max_attempt_and_provider_exception_become_terminal_or_retry():
    key = Fernet.generate_key().decode()
    cipher = TokenCipher(key)
    rows = [_row(cipher, attempt_count=5), _row(cipher)]
    repository = FakeRepo(rows)
    provider = FakeProvider(["transient", RuntimeError("secret provider body")])
    worker = EditorialPushWorker(
        Settings(
            _env_file=None,
            PUSH_NOTIFICATIONS_ENABLED=True,
            PUSH_MAX_ATTEMPTS=5,
        ),
        repository,
        provider,
        cipher,
    )
    await worker.run_once()
    assert repository.retried[0]["max_attempts"] == 5
    assert repository.retried[0]["delay_seconds"] == 80.0
    assert repository.retried[1]["error_code"] == "apns_delivery_unavailable"


@pytest.mark.asyncio
async def test_worker_uses_one_batch_editorial_first_then_timeline_remaining_capacity():
    cipher = TokenCipher(Fernet.generate_key().decode())
    editorial = [_row(cipher) for _ in range(2)]
    timeline = [_timeline_row(cipher, update_kind="occurred")]
    repository = FakeRepo(editorial, timeline)
    provider = FakeProvider(["sent", "sent", "sent"])
    worker = EditorialPushWorker(
        Settings(
            _env_file=None,
            PUSH_NOTIFICATIONS_ENABLED=True,
            PUSH_MAX_ATTEMPTS=5,
        ),
        repository,
        provider,
        cipher,
    )
    worker.batch_limit = 3

    result = await worker.run_once()

    assert result["claimed"] == 3
    assert result["sent"] == 3
    assert repository.claims == [
        {"limit": 3, "lease_seconds": worker.settings.push_lease_seconds},
        {"limit": 1, "lease_seconds": worker.settings.push_lease_seconds},
    ]
    assert provider.calls[-1]["update_kind"] == "occurred"
    assert provider.calls[-1]["stable_key"] == "cluster:policy-and-market-2026"
    assert repository.completed[-1]["timeline"] is True
