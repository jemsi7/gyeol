"""Service-level tests for challenge binding, replay state and cooldown."""
from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.app_attest import VerifiedAssertion, VerifiedAttestation
from app.app_service import AppClientError, AppClientService
from app.config import Settings


class FakeVerifier:
    def __init__(self):
        self.client_data: list[bytes] = []

    def verify_attestation(self, **kwargs):
        assert len(kwargs["challenge"]) == 32
        return VerifiedAttestation(
            public_key_pem=b"public-key",
            receipt=b"receipt",
            environment="development",
            validation_category=3,
            bundle_version="1",
        )

    def verify_assertion(self, **kwargs):
        self.client_data.append(kwargs["client_data"])
        assert any(
            path in kwargs["client_data"]
            for path in (
                b'"path":"/v1/app/feed"',
                b'"path":"/v1/app/discovery"',
                b'"path":"/v1/app/push/registrations"',
                b'"path":"/v1/app/privacy/consents"',
                b'"path":"/v1/app/article-views"',
            )
        )
        return VerifiedAssertion(
            counter=kwargs["previous_counter"] + 1,
            validation_category=3,
            bundle_version="1",
        )


class FakeRepository:
    def __init__(self):
        self.challenges: dict[UUID, dict] = {}
        self.keys: dict[str, dict] = {}
        self.scoped_cooldowns: dict[tuple[str, str], datetime] = {}
        self.registration_calls: list[dict] = []

    async def issue_app_challenge(
        self, *, installation_id, key_id, purpose, ttl_s
    ):
        challenge_id = uuid4()
        row = {
            "id": challenge_id,
            "installation_id": installation_id,
            "key_id": key_id,
            "purpose": purpose,
            "challenge": challenge_id.bytes + challenge_id.bytes,
            "expires_at": datetime.now(UTC) + timedelta(seconds=ttl_s),
            "used_at": None,
            "created_at": datetime.now(UTC),
        }
        self.challenges[challenge_id] = row
        return row

    async def get_app_challenge(self, challenge_id):
        return self.challenges.get(challenge_id)

    async def register_app_attest_key(self, **kwargs):
        self.registration_calls.append(kwargs)
        row = self.challenges.get(kwargs["challenge_id"])
        if not row or row["used_at"] is not None:
            return False
        existing = next(
            (
                value
                for value in self.keys.values()
                if value["installation_id"] == kwargs["installation_id"]
                and value["status"] == "active"
            ),
            None,
        )
        if existing and existing["key_id"] != kwargs["key_id"]:
            return False
        row["used_at"] = datetime.now(UTC)
        self.keys[kwargs["key_id"]] = {
            **kwargs,
            "sign_count": 0,
            "status": "active",
            "last_feed_at": None,
        }
        return True

    async def get_app_attest_context(self, *, key_id, installation_id, challenge_id):
        key = self.keys.get(key_id)
        challenge = self.challenges.get(challenge_id)
        if (
            not challenge
            or challenge["key_id"] != key_id
            or challenge["installation_id"] != installation_id
        ):
            return None
        return {
            **(key or {}),
            "key_id": key_id,
            "installation_id": installation_id,
            "status": key.get("status") if key else None,
            "challenge_id": challenge_id,
            "challenge": challenge["challenge"],
            "purpose": challenge["purpose"],
            "expires_at": challenge["expires_at"],
            "used_at": challenge["used_at"],
        }

    async def commit_verified_assertion(
        self, *, key_id, challenge_id, expected_counter, new_counter
    ):
        key = self.keys.get(key_id)
        row = self.challenges.get(challenge_id)
        if (
            not key
            or not row
            or row["used_at"] is not None
            or key["sign_count"] < expected_counter
            or new_counter <= key["sign_count"]
        ):
            return False
        row["used_at"] = datetime.now(UTC)
        key["sign_count"] = new_counter
        return True

    async def claim_feed_cooldown(self, *, key_id, installation_id, cooldown_s):
        key = self.keys.get(key_id)
        if not key or key["installation_id"] != installation_id:
            raise LookupError
        now = datetime.now(UTC)
        if key["last_feed_at"] is not None:
            remaining = cooldown_s - (now - key["last_feed_at"]).total_seconds()
            if remaining > 0:
                return remaining
        key["last_feed_at"] = now
        return 0.0

    async def claim_app_request_cooldown(
        self, *, key_id, installation_id, scope, cooldown_s
    ):
        key = self.keys.get(key_id)
        if not key or key["installation_id"] != installation_id:
            raise LookupError
        now = datetime.now(UTC)
        cooldown_key = (key_id, scope)
        if cooldown_key in self.scoped_cooldowns:
            remaining = cooldown_s - (
                now - self.scoped_cooldowns[cooldown_key]
            ).total_seconds()
            if remaining > 0:
                return remaining
        self.scoped_cooldowns[cooldown_key] = now
        return 0.0


@pytest.fixture
def configured_service():
    settings = Settings(
        APP_ATTEST_ENABLED=True,
        APP_ATTEST_APP_ID_PREFIX="TEAM123456",
        APP_ATTEST_BUNDLE_ID="com.example.news",
        APP_ATTEST_CLOCK_SKEW_S=30,
        APP_FEED_COOLDOWN_S=1,
    )
    repository = FakeRepository()
    return AppClientService(settings, repository, FakeVerifier()), repository


def _device(installation_id: str = "11111111-1111-1111-1111-111111111111"):
    return {
        "installation_id": installation_id,
        "vendor_id": None,
        "platform": "ipados",
        "model": "iPad14,5",
        "os_version": "26.0",
        "app_version": "1.0",
        "build_number": "1",
        "locale": "ko-KR",
    }


async def _register(service, *, key_id="apple-key", device=None):
    device = device or _device()
    challenge = await service.issue_challenge(
        purpose="attestation", key_id=key_id, device=device
    )
    return await service.register(
        challenge_id=UUID(challenge["challenge_id"]),
        key_id=key_id,
        attestation_object=b"attestation",
        device=device,
    )


@pytest.mark.asyncio
async def test_challenge_is_single_use_and_bound_to_installation(configured_service):
    service, _ = configured_service
    device = _device()
    challenge = await service.issue_challenge(
        purpose="attestation", key_id="key", device=device
    )
    wrong_device = _device("22222222-2222-2222-2222-222222222222")
    with pytest.raises(AppClientError) as wrong:
        await service.register(
            challenge_id=UUID(challenge["challenge_id"]),
            key_id="key",
            attestation_object=b"attestation",
            device=wrong_device,
        )
    assert wrong.value.code == "invalid_or_expired_challenge"

    await service.register(
        challenge_id=UUID(challenge["challenge_id"]),
        key_id="key",
        attestation_object=b"attestation",
        device=device,
    )
    with pytest.raises(AppClientError) as replay:
        await service.register(
            challenge_id=UUID(challenge["challenge_id"]),
            key_id="key",
            attestation_object=b"attestation",
            device=device,
        )
    assert replay.value.code == "invalid_or_expired_challenge"


@pytest.mark.asyncio
async def test_register_forwards_authenticated_recovery_identity(configured_service):
    service, repository = configured_service
    device = _device()
    challenge = await service.issue_challenge(
        purpose="attestation", key_id="replacement-key", device=device
    )
    account_id = uuid4()
    session_id = uuid4()

    result = await service.register(
        challenge_id=UUID(challenge["challenge_id"]),
        key_id="replacement-key",
        attestation_object=b"attestation",
        device=device,
        recovery_account_id=account_id,
        recovery_session_id=session_id,
    )

    assert result == {"registered": True, "key_id": "replacement-key"}
    assert repository.registration_calls[-1]["recovery_account_id"] == account_id
    assert repository.registration_calls[-1]["recovery_session_id"] == session_id


@pytest.mark.asyncio
async def test_assertion_replay_and_one_second_cooldown(configured_service):
    service, repository = configured_service
    device = _device()
    await _register(service, device=device)
    payload = {"device": device, "limit": 5, "cursor": None}
    issued_at = int(datetime.now(UTC).timestamp())

    challenge = await service.issue_challenge(
        purpose="assertion", key_id="apple-key", device=device
    )
    authenticated = await service.authenticate_feed(
        payload=payload,
        challenge_id=UUID(challenge["challenge_id"]),
        key_id="apple-key",
        assertion_object=b"assertion",
        issued_at=issued_at,
    )
    assert authenticated.installation_id == device["installation_id"]
    assert repository.keys["apple-key"]["sign_count"] == 1

    with pytest.raises(AppClientError) as replay:
        await service.authenticate_feed(
            payload=payload,
            challenge_id=UUID(challenge["challenge_id"]),
            key_id="apple-key",
            assertion_object=b"assertion",
            issued_at=issued_at,
        )
    assert replay.value.code == "invalid_or_expired_challenge"

    second = await service.issue_challenge(
        purpose="assertion", key_id="apple-key", device=device
    )
    allowed_at = repository.keys["apple-key"]["last_feed_at"]
    with pytest.raises(AppClientError) as cooldown:
        await service.authenticate_feed(
            payload=payload,
            challenge_id=UUID(second["challenge_id"]),
            key_id="apple-key",
            assertion_object=b"assertion-2",
            issued_at=int(datetime.now(UTC).timestamp()),
        )
    assert cooldown.value.status_code == 429
    assert 0 < cooldown.value.retry_after_seconds <= 1
    assert repository.keys["apple-key"]["last_feed_at"] == allowed_at

    repository.keys["apple-key"]["last_feed_at"] -= timedelta(seconds=1.1)
    third = await service.issue_challenge(
        purpose="assertion", key_id="apple-key", device=device
    )
    await service.authenticate_feed(
        payload=payload,
        challenge_id=UUID(third["challenge_id"]),
        key_id="apple-key",
        assertion_object=b"assertion-3",
        issued_at=int(datetime.now(UTC).timestamp()),
    )
    assert repository.keys["apple-key"]["sign_count"] == 3


@pytest.mark.asyncio
async def test_two_outstanding_assertion_challenges_remain_independently_usable(
    configured_service,
):
    service, repository = configured_service
    device = _device()
    await _register(service, device=device)
    payload = {"device": device, "limit": 5, "cursor": None}
    first = await service.issue_challenge(
        purpose="assertion", key_id="apple-key", device=device
    )
    second = await service.issue_challenge(
        purpose="assertion", key_id="apple-key", device=device
    )

    for challenge in (first, second):
        await service.authenticate_feed(
            payload=payload,
            challenge_id=UUID(challenge["challenge_id"]),
            key_id="apple-key",
            assertion_object=b"assertion",
            issued_at=int(datetime.now(UTC).timestamp()),
        )
        repository.keys["apple-key"]["last_feed_at"] -= timedelta(seconds=1.1)

    assert repository.keys["apple-key"]["sign_count"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("key_state", ["missing", "revoked"])
async def test_valid_challenge_with_inactive_key_has_distinct_error(
    configured_service,
    key_state,
):
    service, repository = configured_service
    device = _device()
    await _register(service, device=device)
    challenge = await service.issue_challenge(
        purpose="assertion", key_id="apple-key", device=device
    )
    if key_state == "missing":
        repository.keys.pop("apple-key")
    else:
        repository.keys["apple-key"]["status"] = "revoked"

    with pytest.raises(AppClientError) as error:
        await service.authenticate_feed(
            payload={"device": device, "limit": 5, "cursor": None},
            challenge_id=UUID(challenge["challenge_id"]),
            key_id="apple-key",
            assertion_object=b"assertion",
            issued_at=int(datetime.now(UTC).timestamp()),
        )

    assert error.value.code == "attested_key_not_active"


@pytest.mark.asyncio
async def test_push_registration_assertion_has_dedicated_path_and_no_feed_cooldown(
    configured_service,
):
    service, repository = configured_service
    device = _device()
    await _register(service, device=device)
    payload = {
        "installation_id": device["installation_id"],
        "device_token": "A" * 43,
        "environment": "development",
        "device": device,
    }

    for expected_counter in (1, 2):
        challenge = await service.issue_challenge(
            purpose="assertion", key_id="apple-key", device=device
        )
        client = await service.authenticate_push_registration(
            payload=payload,
            challenge_id=UUID(challenge["challenge_id"]),
            key_id="apple-key",
            assertion_object=b"assertion",
            issued_at=int(datetime.now(UTC).timestamp()),
        )
        assert client.installation_id == device["installation_id"]
        assert repository.keys["apple-key"]["sign_count"] == expected_counter

    assert repository.keys["apple-key"]["last_feed_at"] is None
    assert b'"path":"/v1/app/push/registrations"' in service.verifier.client_data[-1]


@pytest.mark.asyncio
async def test_privacy_consent_assertion_has_dedicated_path_and_no_feed_cooldown(
    configured_service,
):
    service, repository = configured_service
    device = _device()
    await _register(service, device=device)
    payload = {"policy_version": "1.5.0", "device": device}

    for expected_counter in (1, 2):
        challenge = await service.issue_challenge(
            purpose="assertion", key_id="apple-key", device=device
        )
        client = await service.authenticate_privacy_consent(
            payload=payload,
            challenge_id=UUID(challenge["challenge_id"]),
            key_id="apple-key",
            assertion_object=b"assertion",
            issued_at=int(datetime.now(UTC).timestamp()),
        )
        assert client.installation_id == device["installation_id"]
        assert repository.keys["apple-key"]["sign_count"] == expected_counter

    assert repository.keys["apple-key"]["last_feed_at"] is None
    assert b'"path":"/v1/app/privacy/consents"' in service.verifier.client_data[-1]


@pytest.mark.asyncio
async def test_article_view_assertion_has_dedicated_path_and_no_cooldown(
    configured_service,
):
    service, repository = configured_service
    device = _device()
    await _register(service, device=device)
    payload = {
        "event_id": str(uuid4()),
        "article_id": "a" * 64,
        "device": device,
    }

    for expected_counter in (1, 2):
        challenge = await service.issue_challenge(
            purpose="assertion", key_id="apple-key", device=device
        )
        client = await service.authenticate_article_view(
            payload=payload,
            challenge_id=UUID(challenge["challenge_id"]),
            key_id="apple-key",
            assertion_object=b"assertion",
            issued_at=int(datetime.now(UTC).timestamp()),
        )
        assert client.installation_id == device["installation_id"]
        assert repository.keys["apple-key"]["sign_count"] == expected_counter

    assert repository.keys["apple-key"]["last_feed_at"] is None
    assert repository.scoped_cooldowns == {}
    assert b'"path":"/v1/app/article-views"' in service.verifier.client_data[-1]


@pytest.mark.asyncio
async def test_initial_and_continuation_share_one_second_cooldown(
    configured_service,
):
    service, repository = configured_service
    device = _device()
    await _register(service, device=device)
    payload = {"device": device, "limit": 5, "cursor": "continuation"}

    first = await service.issue_challenge(
        purpose="assertion", key_id="apple-key", device=device
    )
    await service.authenticate_feed(
        payload=payload,
        challenge_id=UUID(first["challenge_id"]),
        key_id="apple-key",
        assertion_object=b"assertion-1",
        issued_at=int(datetime.now(UTC).timestamp()),
        cooldown_s=1.0,
    )

    repository.keys["apple-key"]["last_feed_at"] -= timedelta(seconds=1.1)
    second = await service.issue_challenge(
        purpose="assertion", key_id="apple-key", device=device
    )
    await service.authenticate_feed(
        payload=payload,
        challenge_id=UUID(second["challenge_id"]),
        key_id="apple-key",
        assertion_object=b"assertion-2",
        issued_at=int(datetime.now(UTC).timestamp()),
        cooldown_s=1.0,
    )

    third = await service.issue_challenge(
        purpose="assertion", key_id="apple-key", device=device
    )
    with pytest.raises(AppClientError) as first_page:
        await service.authenticate_feed(
            payload={"device": device, "limit": 5, "cursor": None},
            challenge_id=UUID(third["challenge_id"]),
            key_id="apple-key",
            assertion_object=b"assertion-3",
            issued_at=int(datetime.now(UTC).timestamp()),
        )
    assert first_page.value.status_code == 429
    assert 0 < first_page.value.retry_after_seconds <= 1

    repository.keys["apple-key"]["last_feed_at"] -= timedelta(seconds=1.1)
    fourth = await service.issue_challenge(
        purpose="assertion", key_id="apple-key", device=device
    )
    await service.authenticate_feed(
        payload={"device": device, "limit": 5, "cursor": None},
        challenge_id=UUID(fourth["challenge_id"]),
        key_id="apple-key",
        assertion_object=b"assertion-4",
        issued_at=int(datetime.now(UTC).timestamp()),
    )


@pytest.mark.asyncio
async def test_random_feed_uses_scoped_cooldown_and_feed_error_code(
    configured_service,
):
    service, repository = configured_service
    device = _device()
    await _register(service, device=device)
    payload = {
        "device": device,
        "limit": 5,
        "cursor": None,
        "mode": "random",
        "exclude_article_ids": [],
    }

    first = await service.issue_challenge(
        purpose="assertion", key_id="apple-key", device=device
    )
    await service.authenticate_feed(
        payload=payload,
        challenge_id=UUID(first["challenge_id"]),
        key_id="apple-key",
        assertion_object=b"assertion-1",
        issued_at=int(datetime.now(UTC).timestamp()),
        cooldown_s=1.0,
        cooldown_scope="feed_random",
    )

    second = await service.issue_challenge(
        purpose="assertion", key_id="apple-key", device=device
    )
    with pytest.raises(AppClientError) as error:
        await service.authenticate_feed(
            payload=payload,
            challenge_id=UUID(second["challenge_id"]),
            key_id="apple-key",
            assertion_object=b"assertion-2",
            issued_at=int(datetime.now(UTC).timestamp()),
            cooldown_s=1.0,
            cooldown_scope="feed_random",
        )
    assert error.value.status_code == 429
    assert error.value.code == "feed_cooldown"
    assert ("apple-key", "feed_random") in repository.scoped_cooldowns


@pytest.mark.asyncio
async def test_out_of_range_request_time_is_rejected(configured_service):
    service, _ = configured_service
    device = _device()
    await _register(service, device=device)
    challenge = await service.issue_challenge(
        purpose="assertion", key_id="apple-key", device=device
    )
    with pytest.raises(AppClientError) as error:
        await service.authenticate_feed(
            payload={"device": device, "limit": 5, "cursor": None},
            challenge_id=UUID(challenge["challenge_id"]),
            key_id="apple-key",
            assertion_object=b"assertion",
            issued_at=0,
        )
    assert error.value.code == "request_time_out_of_range"


@pytest.mark.asyncio
async def test_disabled_service_fails_closed():
    service = AppClientService(
        Settings(_env_file=None), FakeRepository(), FakeVerifier()
    )
    with pytest.raises(AppClientError) as error:
        await service.issue_challenge(
            purpose="attestation", key_id="key", device=_device()
        )
    assert error.value.status_code == 503


def test_challenge_encoding_is_urlsafe(configured_service):
    value = base64.urlsafe_b64encode(bytes(32)).rstrip(b"=").decode()
    assert "+" not in value and "/" not in value and "=" not in value
