from __future__ import annotations

import base64
import json
from uuid import uuid4

import httpx
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from app.apns import APNsProvider
from app.config import Settings


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _settings(private_key: ec.EllipticCurvePrivateKey, **overrides) -> Settings:
    values = {
        "PUSH_NOTIFICATIONS_ENABLED": True,
        "APNS_TEAM_ID": "TEAM123456",
        "APNS_KEY_ID": "KEY1234567",
        "APNS_PRIVATE_KEY": private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
        "APNS_TOPIC": "com.example.textshorts",
        "APPLE_TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode(),
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.mark.asyncio
async def test_apns_request_has_verified_es256_token_exact_headers_and_payload():
    key = ec.generate_private_key(ec.SECP256R1())
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, headers={"apns-id": str(uuid4())})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = APNsProvider(_settings(key), client, clock=lambda: 1_700_000_000)
    apns_id = uuid4()

    result = await provider.send(
        device_token=bytes(range(32)),
        environment="development",
        apns_id=apns_id,
        headline="새 에디토리얼",
        body="현장 취재의 핵심",
        article_id="article-id",
    )

    assert result.kind == "sent"
    request = seen[0]
    assert request.url.host == "api.sandbox.push.apple.com"
    assert request.url.path == f"/3/device/{bytes(range(32)).hex()}"
    assert request.headers["apns-topic"] == "com.example.textshorts"
    assert request.headers["apns-push-type"] == "alert"
    assert request.headers["apns-priority"] == "10"
    assert request.headers["apns-id"] == str(apns_id)
    payload = json.loads(request.content)
    assert payload == {
        "aps": {
            "alert": {
                "title": "새 Editorial이 발행되었습니다.",
                "body": "새 에디토리얼",
            },
            "sound": "default",
        },
        "kind": "editorial_published",
        "article_id": "article-id",
    }
    token = request.headers["authorization"].removeprefix("bearer ")
    header, claims, signature = token.split(".")
    assert json.loads(_b64decode(header)) == {"alg": "ES256", "kid": "KEY1234567"}
    assert json.loads(_b64decode(claims)) == {"iat": 1_700_000_000, "iss": "TEAM123456"}
    raw_signature = _b64decode(signature)
    r = int.from_bytes(raw_signature[:32], "big")
    s = int.from_bytes(raw_signature[32:], "big")
    try:
        key.public_key().verify(
            encode_dss_signature(r, s),
            f"{header}.{claims}".encode(),
            ec.ECDSA(hashes.SHA256()),
        )
    except InvalidSignature:
        pytest.fail("provider token signature did not verify")
    await client.aclose()


@pytest.mark.asyncio
async def test_production_registration_selects_production_apns_host():
    key = ec.generate_private_key(ec.SECP256R1())
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = APNsProvider(_settings(key), client)
    await provider.send(
        device_token=b"production-token",
        environment="production",
        apns_id=uuid4(),
        headline="제목",
        body="본문",
        article_id="article-id",
    )

    assert seen[0].url.host == "api.push.apple.com"
    await client.aclose()


@pytest.mark.asyncio
async def test_weekly_insight_uses_distinct_bounded_deep_link_kind():
    key = ec.generate_private_key(ec.SECP256R1())
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = APNsProvider(_settings(key), client)
    result = await provider.send(
        device_token=b"weekly-token",
        environment="development",
        apns_id=uuid4(),
        headline="주간인사이트",
        body="이번 주의 핵심",
        article_id="weekly-article",
        publication_kind="weekly_insight",
    )

    assert result.kind == "sent"
    payload = json.loads(seen[0].content)
    assert payload["aps"]["alert"] == {
        "title": "새 주간인사이트가 발행되었습니다.",
        "body": "주간인사이트",
    }
    assert payload["kind"] == "weekly_insight_published"
    assert payload["article_id"] == "weekly-article"
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("update_kind", "expected_body"),
    [
        ("inserted", "사건이 갱신되었습니다."),
        ("occurred", "사건이 추가되었습니다."),
    ],
)
async def test_timeline_update_uses_exact_korean_copy_and_deep_link_payload(
    update_kind, expected_body
):
    key = ec.generate_private_key(ec.SECP256R1())
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = APNsProvider(_settings(key), client)
    event_id = uuid4()
    node_id = uuid4()
    result = await provider.send_timeline_update(
        device_token=b"timeline-token",
        environment="development",
        apns_id=uuid4(),
        stable_key="cluster:policy-and-market-2026",
        node_id=node_id,
        update_kind=update_kind,
        event_id=event_id,
    )

    assert result.kind == "sent"
    payload = json.loads(seen[0].content)
    assert payload == {
        "aps": {
            "alert": {
                "title": "타임라인 업데이트",
                "body": expected_body,
            },
            "sound": "default",
        },
        "kind": "timeline_updated",
        "event_id": str(event_id),
        "timeline_stable_key": "cluster:policy-and-market-2026",
        "timeline_node_id": str(node_id),
        "timeline_update_kind": update_kind,
    }
    assert len(seen[0].content) <= 4096
    await client.aclose()


@pytest.mark.asyncio
async def test_timeline_update_rejects_invalid_kind_without_network():
    key = ec.generate_private_key(ec.SECP256R1())
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = APNsProvider(_settings(key), client)
    result = await provider.send_timeline_update(
        device_token=b"timeline-token",
        environment="development",
        apns_id=uuid4(),
        stable_key="stable-key",
        node_id=uuid4(),
        update_kind="existing",
        event_id=uuid4(),
    )

    assert result.kind == "permanent"
    assert result.code == "invalid_timeline_update_kind"
    assert seen == []
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headline", "body", "expected_body"),
    [
        ("기사 제목", "부제목", "기사 제목"),
        ("", "부제목", "부제목"),
        ("   ", "  부제목  ", "부제목"),
        ("", "", None),
    ],
)
async def test_alert_title_is_fixed_and_body_falls_back_to_available_copy(
    headline, body, expected_body
):
    key = ec.generate_private_key(ec.SECP256R1())
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = APNsProvider(_settings(key), client)
    await provider.send(
        device_token=b"token",
        environment="development",
        apns_id=uuid4(),
        headline=headline,
        body=body,
        article_id="article-id",
    )

    alert = json.loads(seen[0].content)["aps"]["alert"]
    assert alert["title"] == "새 Editorial이 발행되었습니다."
    if expected_body is None:
        assert "body" not in alert
    else:
        assert alert["body"] == expected_body
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "reason", "kind"),
    [
        (400, "BadDeviceToken", "invalid_token"),
        (400, "DeviceTokenNotForTopic", "invalid_token"),
        (410, "Unregistered", "invalid_token"),
        (429, "TooManyRequests", "transient"),
        (500, None, "transient"),
        (503, "Shutdown", "transient"),
        (403, "InvalidProviderToken", "permanent"),
        (403, "BadEnvironmentKeyInToken", "permanent"),
        (403, "BadCertificateEnvironment", "permanent"),
        (400, "PayloadEmpty", "permanent"),
    ],
)
async def test_apns_status_matrix_is_redacted_and_stable(status, reason, kind):
    key = ec.generate_private_key(ec.SECP256R1())

    async def handler(_request):
        return httpx.Response(status, json={"reason": reason} if reason else {})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = APNsProvider(_settings(key), client)
    result = await provider.send(
        device_token=b"token",
        environment="production",
        apns_id=uuid4(),
        headline="제목",
        body="본문",
        article_id="article-id",
    )
    assert result.kind == kind
    assert result.code == (reason or f"apns_http_{status}")
    assert len(result.code) < 64
    await client.aclose()


@pytest.mark.asyncio
async def test_transport_error_is_retryable_and_provider_token_is_cached_for_50_minutes():
    key = ec.generate_private_key(ec.SECP256R1())
    now = [1_700_000_000.0]
    authorizations: list[str] = []

    async def handler(request):
        authorizations.append(request.headers["authorization"])
        if len(authorizations) == 3:
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = APNsProvider(_settings(key), client, clock=lambda: now[0])
    arguments = dict(
        device_token=b"token",
        environment="production",
        headline="제목",
        body="본문",
        article_id="article-id",
    )
    await provider.send(apns_id=uuid4(), **arguments)
    now[0] += 2_999
    await provider.send(apns_id=uuid4(), **arguments)
    now[0] += 1
    result = await provider.send(apns_id=uuid4(), **arguments)
    assert authorizations[0] == authorizations[1]
    assert authorizations[1] != authorizations[2]
    assert result.kind == "transient"
    assert result.code == "apns_transport_error"
    await client.aclose()


def test_apns_configuration_is_disabled_by_default_and_fails_closed_when_partial():
    settings = Settings(_env_file=None)
    assert settings.push_notifications_enabled is False
    with pytest.raises(ValueError, match="disabled"):
        settings.validate_apns()
    with pytest.raises(ValueError, match="APNS_KEY_ID"):
        Settings(
            _env_file=None,
            PUSH_NOTIFICATIONS_ENABLED=True,
            APNS_TEAM_ID="TEAM123456",
        ).validate_apns()
