"""Token-authenticated HTTP/2 client for Apple Push Notification service."""
from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from typing import Callable, Literal
from uuid import UUID

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from .config import Settings


APNS_REASON_ALLOWLIST = {
    "BadCertificateEnvironment",
    "BadCollapseId",
    "BadDeviceToken",
    "BadEnvironmentKeyInToken",
    "BadExpirationDate",
    "BadMessageId",
    "BadPriority",
    "BadTopic",
    "DeviceTokenNotForTopic",
    "DuplicateHeaders",
    "ExpiredProviderToken",
    "Forbidden",
    "IdleTimeout",
    "InvalidProviderToken",
    "MissingDeviceToken",
    "MissingTopic",
    "PayloadEmpty",
    "PayloadTooLarge",
    "Shutdown",
    "TooManyProviderTokenUpdates",
    "TooManyRequests",
    "TopicDisallowed",
    "Unregistered",
}


PUBLICATION_ALERT_TITLES = {
    "editorial": "새 Editorial이 발행되었습니다.",
    "weekly_insight": "새 주간인사이트가 발행되었습니다.",
}


@dataclass(frozen=True, slots=True)
class APNsResult:
    kind: Literal["sent", "transient", "permanent", "invalid_token"]
    apns_id: UUID
    code: str


class APNsProvider:
    token_ttl_seconds = 50 * 60

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ):
        settings.validate_apns()
        key = serialization.load_pem_private_key(
            settings.apns_private_key.get_secret_value().encode("utf-8"),
            password=None,
        )
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(
            key.curve, ec.SECP256R1
        ):
            raise ValueError("APNS_PRIVATE_KEY must be an EC P-256 private key")
        self.settings = settings
        self.private_key = key
        self.clock = clock
        self._client = client or httpx.AsyncClient(
            http2=True,
            timeout=settings.apns_timeout_s,
        )
        self._owns_client = client is None
        self._cached_token: str | None = None
        self._token_issued_at: int | None = None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def send(
        self,
        *,
        device_token: bytes,
        environment: str,
        apns_id: UUID,
        headline: str,
        body: str,
        article_id: str,
        publication_kind: Literal["editorial", "weekly_insight"] = "editorial",
    ) -> APNsResult:
        if not device_token or len(device_token) > 256:
            return APNsResult("invalid_token", apns_id, "invalid_device_token")
        if environment not in {"development", "production"}:
            return APNsResult("permanent", apns_id, "invalid_environment")
        if publication_kind not in {"editorial", "weekly_insight"}:
            return APNsResult("permanent", apns_id, "invalid_publication_kind")
        alert = {"title": PUBLICATION_ALERT_TITLES[publication_kind]}
        subject = headline.strip() or body.strip()
        if subject:
            alert["body"] = subject
        payload = {
            "aps": {
                "alert": alert,
                "sound": "default",
            },
            "kind": (
                "weekly_insight_published"
                if publication_kind == "weekly_insight"
                else "editorial_published"
            ),
            "article_id": article_id,
        }
        return await self._send_payload(
            device_token=device_token,
            environment=environment,
            apns_id=apns_id,
            payload=payload,
        )

    async def send_timeline_update(
        self,
        *,
        device_token: bytes,
        environment: str,
        apns_id: UUID,
        stable_key: str,
        node_id: UUID,
        update_kind: Literal["inserted", "occurred"],
        event_id: UUID,
    ) -> APNsResult:
        if update_kind not in {"inserted", "occurred"}:
            return APNsResult("permanent", apns_id, "invalid_timeline_update_kind")
        if not 1 <= len(stable_key) <= 128:
            return APNsResult("permanent", apns_id, "invalid_timeline_stable_key")
        payload = {
            "aps": {
                "alert": {
                    "title": "타임라인 업데이트",
                    "body": (
                        "사건이 갱신되었습니다."
                        if update_kind == "inserted"
                        else "사건이 추가되었습니다."
                    ),
                },
                "sound": "default",
            },
            "kind": "timeline_updated",
            "event_id": str(event_id),
            "timeline_stable_key": stable_key,
            "timeline_node_id": str(node_id),
            "timeline_update_kind": update_kind,
        }
        return await self._send_payload(
            device_token=device_token,
            environment=environment,
            apns_id=apns_id,
            payload=payload,
        )

    async def _send_payload(
        self,
        *,
        device_token: bytes,
        environment: str,
        apns_id: UUID,
        payload: dict,
    ) -> APNsResult:
        if not device_token or len(device_token) > 256:
            return APNsResult("invalid_token", apns_id, "invalid_device_token")
        if environment not in {"development", "production"}:
            return APNsResult("permanent", apns_id, "invalid_environment")
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > 4096:
            return APNsResult("permanent", apns_id, "payload_too_large")
        host = (
            "https://api.sandbox.push.apple.com"
            if environment == "development"
            else "https://api.push.apple.com"
        )
        headers = {
            "authorization": f"bearer {self._provider_token()}",
            "apns-topic": self.settings.apns_topic,
            "apns-push-type": "alert",
            "apns-priority": "10",
            "apns-id": str(apns_id),
            "content-type": "application/json",
        }
        try:
            response = await self._client.post(
                f"{host}/3/device/{device_token.hex()}",
                headers=headers,
                content=encoded,
            )
        except httpx.TransportError:
            return APNsResult("transient", apns_id, "apns_transport_error")
        code = _response_code(response)
        if response.status_code == 200:
            return APNsResult("sent", apns_id, "ok")
        if response.status_code == 410 or code in {
            "BadDeviceToken",
            "DeviceTokenNotForTopic",
            "Unregistered",
        }:
            return APNsResult("invalid_token", apns_id, code)
        if response.status_code in {429, 500, 503}:
            return APNsResult("transient", apns_id, code)
        return APNsResult("permanent", apns_id, code)

    def _provider_token(self) -> str:
        now = int(self.clock())
        if (
            self._cached_token is not None
            and self._token_issued_at is not None
            and 0 <= now - self._token_issued_at < self.token_ttl_seconds
        ):
            return self._cached_token
        header = _base64url_json(
            {"alg": "ES256", "kid": self.settings.apns_key_id}
        )
        claims = _base64url_json({"iss": self.settings.apns_team_id, "iat": now})
        signing_input = f"{header}.{claims}".encode("ascii")
        der = self.private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        signature = _base64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
        self._cached_token = f"{header}.{claims}.{signature}"
        self._token_issued_at = now
        return self._cached_token


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_json(value: dict) -> str:
    return _base64url(
        json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )


def _response_code(response: httpx.Response) -> str:
    try:
        reason = response.json().get("reason")
    except (ValueError, AttributeError):
        reason = None
    if isinstance(reason, str) and reason in APNS_REASON_ALLOWLIST:
        return reason
    return f"apns_http_{response.status_code}"
