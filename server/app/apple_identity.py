"""Strict Sign in with Apple token verification and token lifecycle client."""
from __future__ import annotations

import base64
import hashlib
import json
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Mapping

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from .config import Settings

APPLE_ISSUER = "https://appleid.apple.com"
MAX_JWT_BYTES = 16 * 1024
MAX_JWKS_BYTES = 1024 * 1024


class AppleIdentityError(RuntimeError):
    """A redacted, stable failure safe to project through the account API."""

    def __init__(self, code: str, *, unavailable: bool = False):
        self.code = code
        self.unavailable = unavailable
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class AppleIdentityClaims:
    subject: str
    email: str | None
    email_verified: bool


@dataclass(frozen=True, slots=True)
class AppleTokenSet:
    refresh_token: str
    identity_token: str


class TokenCipher:
    def __init__(self, key: str):
        try:
            self._fernet = Fernet(key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise AppleIdentityError("apple_token_cipher_not_configured") from exc

    def encrypt(self, value: str) -> bytes:
        if not value:
            raise AppleIdentityError("apple_token_missing")
        return self._fernet.encrypt(value.encode("utf-8"))

    def decrypt(self, value: bytes) -> str:
        try:
            decoded = self._fernet.decrypt(value).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise AppleIdentityError("apple_token_decryption_failed") from exc
        if not decoded:
            raise AppleIdentityError("apple_token_decryption_failed")
        return decoded


class AppleIdentityProvider:
    def __init__(
        self,
        settings: Settings,
        http_client: httpx.AsyncClient,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.settings = settings
        self.http = http_client
        self.now = now
        self.monotonic = monotonic
        self._jwks: dict[str, rsa.RSAPublicKey] = {}
        self._jwks_loaded_at = 0.0

    async def verify_identity_token(
        self, token: str, raw_nonce: str
    ) -> AppleIdentityClaims:
        header, payload, signature, signing_input = _parse_jws(token)
        if header.get("alg") != "RS256":
            raise AppleIdentityError("invalid_apple_identity_token")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid or len(kid) > 128:
            raise AppleIdentityError("invalid_apple_identity_token")
        key = await self._key_for(kid)
        try:
            key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
        except InvalidSignature as exc:
            raise AppleIdentityError("invalid_apple_identity_token") from exc

        now_ts = int(self.now().timestamp())
        if payload.get("iss") != APPLE_ISSUER:
            raise AppleIdentityError("invalid_apple_identity_token")
        if payload.get("aud") != self.settings.apple_sign_in_client_id:
            raise AppleIdentityError("invalid_apple_identity_token")
        exp = _numeric_date(payload.get("exp"))
        iat = _numeric_date(payload.get("iat"))
        if exp <= now_ts - 30 or iat > now_ts + 30:
            raise AppleIdentityError("invalid_apple_identity_token")
        subject = payload.get("sub")
        if not isinstance(subject, str) or not subject or len(subject) > 255:
            raise AppleIdentityError("invalid_apple_identity_token")
        if not raw_nonce or len(raw_nonce) > 512:
            raise AppleIdentityError("invalid_apple_nonce")
        expected_nonce = hashlib.sha256(raw_nonce.encode("utf-8")).hexdigest()
        if payload.get("nonce") != expected_nonce:
            raise AppleIdentityError("invalid_apple_nonce")

        email = payload.get("email")
        if not isinstance(email, str) or not email or len(email) > 254:
            email = None
        verified = payload.get("email_verified")
        email_verified = verified is True or (
            isinstance(verified, str) and verified.lower() == "true"
        )
        return AppleIdentityClaims(subject, email, email_verified)

    async def exchange_code(self, code: str) -> AppleTokenSet:
        if not code or len(code) > MAX_JWT_BYTES:
            raise AppleIdentityError("invalid_apple_authorization_code")
        response = await self._post_form(
            self.settings.apple_sign_in_token_url,
            {
                "client_id": self.settings.apple_sign_in_client_id,
                "client_secret": self.create_client_secret(),
                "code": code,
                "grant_type": "authorization_code",
            },
            failure_code="invalid_apple_authorization_code",
        )
        refresh = response.get("refresh_token")
        identity = response.get("id_token")
        if (
            not isinstance(refresh, str)
            or not refresh
            or len(refresh) > MAX_JWT_BYTES
            or not isinstance(identity, str)
            or not identity
            or len(identity) > MAX_JWT_BYTES
        ):
            raise AppleIdentityError("invalid_apple_token_response")
        return AppleTokenSet(refresh, identity)

    async def revoke(self, refresh_token: str) -> bool:
        if not refresh_token or len(refresh_token) > MAX_JWT_BYTES:
            return False
        try:
            await self._post_form(
                self.settings.apple_sign_in_revoke_url,
                {
                    "client_id": self.settings.apple_sign_in_client_id,
                    "client_secret": self.create_client_secret(),
                    "token": refresh_token,
                    "token_type_hint": "refresh_token",
                },
                failure_code="apple_token_revoke_failed",
                expect_json=False,
            )
            return True
        except AppleIdentityError:
            return False

    def create_client_secret(self) -> str:
        private_pem = self.settings.apple_sign_in_private_key.get_secret_value()
        private_pem = private_pem.replace("\\n", "\n").strip().encode("utf-8")
        try:
            key = serialization.load_pem_private_key(private_pem, password=None)
        except (ValueError, TypeError) as exc:
            raise AppleIdentityError("apple_private_key_invalid") from exc
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(
            key.curve, ec.SECP256R1
        ):
            raise AppleIdentityError("apple_private_key_invalid")
        now = self.now()
        header = {
            "alg": "ES256",
            "kid": self.settings.apple_sign_in_key_id,
            "typ": "JWT",
        }
        payload = {
            "iss": self.settings.apple_sign_in_team_id,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
            "aud": APPLE_ISSUER,
            "sub": self.settings.apple_sign_in_client_id,
        }
        encoded_header = _encode_json(header)
        encoded_payload = _encode_json(payload)
        signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
        der = key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        return f"{encoded_header}.{encoded_payload}.{_b64url_encode(signature)}"

    async def _key_for(self, kid: str) -> rsa.RSAPublicKey:
        if self.monotonic() - self._jwks_loaded_at >= 3600 or not self._jwks:
            await self._load_jwks()
        key = self._jwks.get(kid)
        if key is None:
            await self._load_jwks()
            key = self._jwks.get(kid)
        if key is None:
            raise AppleIdentityError("invalid_apple_identity_token")
        return key

    async def _load_jwks(self) -> None:
        try:
            response = await self.http.get(self.settings.apple_sign_in_jwks_url)
            response.raise_for_status()
        except (httpx.HTTPError, ValueError) as exc:
            raise AppleIdentityError(
                "apple_identity_service_unavailable", unavailable=True
            ) from exc
        if len(response.content) > MAX_JWKS_BYTES:
            raise AppleIdentityError("invalid_apple_jwks")
        try:
            payload = response.json()
        except ValueError as exc:
            raise AppleIdentityError("invalid_apple_jwks") from exc
        rows = payload.get("keys") if isinstance(payload, Mapping) else None
        if not isinstance(rows, list):
            raise AppleIdentityError("invalid_apple_jwks")
        parsed: dict[str, rsa.RSAPublicKey] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            kid, alg = row.get("kid"), row.get("alg")
            if not isinstance(kid, str) or alg != "RS256" or row.get("kty") != "RSA":
                continue
            try:
                n = int.from_bytes(_b64url_decode(str(row.get("n", ""))), "big")
                e = int.from_bytes(_b64url_decode(str(row.get("e", ""))), "big")
                parsed[kid] = rsa.RSAPublicNumbers(e, n).public_key()
            except (ValueError, TypeError):
                continue
        if not parsed:
            raise AppleIdentityError("invalid_apple_jwks")
        self._jwks = parsed
        self._jwks_loaded_at = self.monotonic()

    async def _post_form(
        self,
        url: str,
        form: Mapping[str, str],
        *,
        failure_code: str,
        expect_json: bool = True,
    ) -> dict[str, Any]:
        try:
            response = await self.http.post(
                url,
                data=form,
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as exc:
            raise AppleIdentityError(
                "apple_identity_service_unavailable", unavailable=True
            ) from exc
        if response.status_code >= 500:
            raise AppleIdentityError(
                "apple_identity_service_unavailable", unavailable=True
            )
        if response.status_code != 200:
            raise AppleIdentityError(failure_code)
        if not expect_json:
            return {}
        if len(response.content) > 64 * 1024:
            raise AppleIdentityError("invalid_apple_token_response")
        try:
            payload = response.json()
        except ValueError as exc:
            raise AppleIdentityError("invalid_apple_token_response") from exc
        if not isinstance(payload, dict):
            raise AppleIdentityError("invalid_apple_token_response")
        return payload


def _parse_jws(token: str) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes]:
    if not isinstance(token, str) or not token or len(token.encode("utf-8")) > MAX_JWT_BYTES:
        raise AppleIdentityError("invalid_apple_identity_token")
    parts = token.split(".")
    if len(parts) != 3 or any(not part for part in parts):
        raise AppleIdentityError("invalid_apple_identity_token")
    try:
        header = json.loads(_b64url_decode(parts[0]))
        payload = json.loads(_b64url_decode(parts[1]))
        signature = _b64url_decode(parts[2])
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AppleIdentityError("invalid_apple_identity_token") from exc
    if not isinstance(header, dict) or not isinstance(payload, dict) or not signature:
        raise AppleIdentityError("invalid_apple_identity_token")
    return header, payload, signature, f"{parts[0]}.{parts[1]}".encode("ascii")


def _numeric_date(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AppleIdentityError("invalid_apple_identity_token")
    if not math.isfinite(float(value)):
        raise AppleIdentityError("invalid_apple_identity_token")
    return int(value)


def _b64url_decode(value: str) -> bytes:
    if not value or len(value) > MAX_JWT_BYTES * 2:
        raise ValueError("invalid base64url")
    return base64.b64decode(
        value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
    )


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _encode_json(value: Mapping[str, Any]) -> str:
    return _b64url_encode(
        json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
