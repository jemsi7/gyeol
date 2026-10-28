"""Application service for App Attest registration and feed authentication."""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping, Protocol
from uuid import UUID

from .app_attest import (
    AppAttestVerificationError,
    AppAttestVerifier,
    VerifiedAssertion,
    VerifiedAttestation,
    build_app_client_data,
)
from .config import Settings


class AppClientRepository(Protocol):
    async def issue_app_challenge(self, **kwargs: Any) -> dict: ...
    async def get_app_challenge(self, challenge_id: UUID) -> dict | None: ...
    async def register_app_attest_key(self, **kwargs: Any) -> bool: ...
    async def get_app_attest_context(self, **kwargs: Any) -> dict | None: ...
    async def commit_verified_assertion(self, **kwargs: Any) -> bool: ...
    async def claim_feed_cooldown(self, **kwargs: Any) -> float: ...
    async def claim_app_request_cooldown(self, **kwargs: Any) -> float: ...


class AppClientError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        code: str,
        *,
        retry_after_seconds: float | None = None,
    ):
        self.status_code = status_code
        self.code = code
        self.retry_after_seconds = retry_after_seconds
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class AuthenticatedClient:
    key_id: str
    installation_id: str
    device: Mapping[str, Any]


class AppClientService:
    def __init__(
        self,
        settings: Settings,
        repository: AppClientRepository,
        verifier: AppAttestVerifier | None,
    ):
        self.settings = settings
        self.repository = repository
        self.verifier = verifier

    async def issue_challenge(
        self, *, purpose: str, key_id: str, device: Mapping[str, Any]
    ) -> dict:
        self._ensure_configured()
        assert self.verifier is not None
        if purpose not in {"attestation", "assertion"}:
            raise AppClientError(400, "invalid_challenge_purpose")
        installation_id = str(device.get("installation_id") or "")
        if not installation_id or not key_id:
            raise AppClientError(400, "invalid_client_identity")
        row = await self.repository.issue_app_challenge(
            installation_id=installation_id,
            key_id=key_id,
            purpose=purpose,
            ttl_s=self.settings.app_attest_challenge_ttl_s,
        )
        return {
            "challenge_id": str(row["id"]),
            "challenge": _base64url_encode(row["challenge"]),
            "expires_at": row["expires_at"],
        }

    async def register(
        self,
        *,
        challenge_id: UUID,
        key_id: str,
        attestation_object: bytes,
        device: Mapping[str, Any],
        recovery_account_id: UUID | None = None,
        recovery_session_id: UUID | None = None,
    ) -> dict:
        self._ensure_configured()
        assert self.verifier is not None
        installation_id = str(device.get("installation_id") or "")
        row = await self.repository.get_app_challenge(challenge_id)
        if not _valid_challenge(
            row,
            purpose="attestation",
            installation_id=installation_id,
            key_id=key_id,
        ):
            raise AppClientError(401, "invalid_or_expired_challenge")
        try:
            verified: VerifiedAttestation = self.verifier.verify_attestation(
                attestation_object=attestation_object,
                key_id=key_id,
                challenge=row["challenge"],
            )
        except AppAttestVerificationError as exc:
            raise AppClientError(401, exc.code) from exc
        saved = await self.repository.register_app_attest_key(
            challenge_id=challenge_id,
            installation_id=installation_id,
            key_id=key_id,
            public_key_pem=verified.public_key_pem,
            receipt=verified.receipt,
            environment=verified.environment,
            device=dict(device),
            validation_category=verified.validation_category,
            bundle_version=verified.bundle_version,
            recovery_account_id=recovery_account_id,
            recovery_session_id=recovery_session_id,
        )
        if not saved:
            raise AppClientError(409, "attestation_registration_conflict")
        return {"registered": True, "key_id": key_id}

    async def authenticate_feed(
        self,
        *,
        payload: Mapping[str, Any],
        challenge_id: UUID,
        key_id: str,
        assertion_object: bytes,
        issued_at: int,
        cooldown_s: float | None = None,
        cooldown_scope: str | None = None,
    ) -> AuthenticatedClient:
        return await self._authenticate_request(
            payload=payload,
            challenge_id=challenge_id,
            key_id=key_id,
            assertion_object=assertion_object,
            issued_at=issued_at,
            path="/v1/app/feed",
            cooldown_s=(
                self.settings.app_feed_cooldown_s
                if cooldown_s is None
                else cooldown_s
            ),
            cooldown_scope=cooldown_scope,
        )

    async def authenticate_discovery(
        self,
        *,
        payload: Mapping[str, Any],
        challenge_id: UUID,
        key_id: str,
        assertion_object: bytes,
        issued_at: int,
        query_present: bool,
    ) -> AuthenticatedClient:
        return await self._authenticate_request(
            payload=payload,
            challenge_id=challenge_id,
            key_id=key_id,
            assertion_object=assertion_object,
            issued_at=issued_at,
            path="/v1/app/discovery",
            cooldown_s=(
                self.settings.app_discovery_search_cooldown_s
                if query_present
                else self.settings.app_discovery_cooldown_s
            ),
            cooldown_scope=(
                "discovery_search" if query_present else "discovery_catalog"
            ),
        )

    async def authenticate_push_registration(
        self,
        *,
        payload: Mapping[str, Any],
        challenge_id: UUID,
        key_id: str,
        assertion_object: bytes,
        issued_at: int,
    ) -> AuthenticatedClient:
        return await self._authenticate_request(
            payload=payload,
            challenge_id=challenge_id,
            key_id=key_id,
            assertion_object=assertion_object,
            issued_at=issued_at,
            path="/v1/app/push/registrations",
            cooldown_s=None,
            cooldown_scope=None,
        )

    async def authenticate_privacy_consent(
        self,
        *,
        payload: Mapping[str, Any],
        challenge_id: UUID,
        key_id: str,
        assertion_object: bytes,
        issued_at: int,
    ) -> AuthenticatedClient:
        return await self._authenticate_request(
            payload=payload,
            challenge_id=challenge_id,
            key_id=key_id,
            assertion_object=assertion_object,
            issued_at=issued_at,
            path="/v1/app/privacy/consents",
            cooldown_s=None,
            cooldown_scope=None,
        )

    async def authenticate_article_view(
        self,
        *,
        payload: Mapping[str, Any],
        challenge_id: UUID,
        key_id: str,
        assertion_object: bytes,
        issued_at: int,
    ) -> AuthenticatedClient:
        return await self._authenticate_request(
            payload=payload,
            challenge_id=challenge_id,
            key_id=key_id,
            assertion_object=assertion_object,
            issued_at=issued_at,
            path="/v1/app/article-views",
            cooldown_s=None,
            cooldown_scope=None,
        )

    async def _authenticate_request(
        self,
        *,
        payload: Mapping[str, Any],
        challenge_id: UUID,
        key_id: str,
        assertion_object: bytes,
        issued_at: int,
        path: str,
        cooldown_s: float | None,
        cooldown_scope: str | None,
    ) -> AuthenticatedClient:
        self._ensure_configured()
        assert self.verifier is not None
        now = datetime.now(UTC)
        if abs(int(now.timestamp()) - issued_at) > self.settings.app_attest_clock_skew_s:
            raise AppClientError(401, "request_time_out_of_range")
        device = payload.get("device")
        if not isinstance(device, Mapping):
            raise AppClientError(400, "invalid_device")
        installation_id = str(device.get("installation_id") or "")
        context = await self.repository.get_app_attest_context(
            key_id=key_id,
            installation_id=installation_id,
            challenge_id=challenge_id,
        )
        if not _valid_challenge(
            context,
            purpose="assertion",
            installation_id=installation_id,
            key_id=key_id,
        ):
            raise AppClientError(401, "invalid_or_expired_challenge")
        if context.get("status") != "active":
            raise AppClientError(401, "attested_key_not_active")
        client_data = build_app_client_data(
            challenge_id=str(challenge_id),
            challenge=context["challenge"],
            payload=payload,
            issued_at=issued_at,
            path=path,
        )
        try:
            verified: VerifiedAssertion = self.verifier.verify_assertion(
                assertion_object=assertion_object,
                client_data=client_data,
                public_key_pem=context["public_key_pem"],
                previous_counter=context["sign_count"],
            )
        except AppAttestVerificationError as exc:
            raise AppClientError(401, exc.code) from exc
        committed = await self.repository.commit_verified_assertion(
            key_id=key_id,
            challenge_id=challenge_id,
            expected_counter=context["sign_count"],
            new_counter=verified.counter,
        )
        if not committed:
            raise AppClientError(409, "assertion_replay_or_race")
        try:
            if cooldown_s is None:
                remaining = 0.0
            elif cooldown_scope is None:
                remaining = await self.repository.claim_feed_cooldown(
                    key_id=key_id,
                    installation_id=installation_id,
                    cooldown_s=cooldown_s,
                )
            else:
                remaining = await self.repository.claim_app_request_cooldown(
                    key_id=key_id,
                    installation_id=installation_id,
                    scope=cooldown_scope,
                    cooldown_s=cooldown_s,
                )
        except LookupError as exc:
            raise AppClientError(401, "attested_key_not_active") from exc
        if remaining > 0:
            raise AppClientError(
                429,
                (
                    "discovery_cooldown"
                    if path == "/v1/app/discovery"
                    else "feed_cooldown"
                ),
                retry_after_seconds=remaining,
            )
        return AuthenticatedClient(
            key_id=key_id,
            installation_id=installation_id,
            device=device,
        )

    def _ensure_configured(self) -> None:
        try:
            self.settings.validate_app_attest()
            if self.verifier is None:
                raise ValueError("App Attest verifier is unavailable")
        except ValueError as exc:
            raise AppClientError(503, "app_attest_not_configured") from exc


def decode_base64url(value: str, *, max_bytes: int) -> bytes:
    if len(value) > max_bytes * 2:
        raise AppClientError(400, "invalid_base64")
    try:
        raw = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, binascii.Error) as exc:
        raise AppClientError(400, "invalid_base64") from exc
    if len(raw) > max_bytes:
        raise AppClientError(400, "invalid_base64")
    return raw


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _valid_challenge(
    row: Mapping[str, Any] | None,
    *,
    purpose: str,
    installation_id: str,
    key_id: str,
) -> bool:
    if not row:
        return False
    expires_at = row.get("expires_at")
    if not isinstance(expires_at, datetime):
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return bool(
        row.get("used_at") is None
        and expires_at > datetime.now(UTC)
        and row.get("purpose") == purpose
        and row.get("installation_id") == installation_id
        and row.get("key_id") == key_id
    )
