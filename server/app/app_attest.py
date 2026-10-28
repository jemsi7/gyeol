"""Strict Apple App Attest attestation and assertion verification.

The implementation follows Apple's server validation sequence and deliberately
fails closed. It never logs the attestation object, certificate receipt, public
key, assertion, or challenge bytes.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import struct
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

import cbor2
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.x509.oid import ExtensionOID, ObjectIdentifier

NONCE_OID = ObjectIdentifier("1.2.840.113635.100.8.2")
MAX_ATTESTATION_BYTES = 128 * 1024
MAX_ASSERTION_BYTES = 64 * 1024
MAX_CLIENT_DATA_BYTES = 8 * 1024
PRODUCTION_AAGUID = b"appattest" + bytes(7)
DEVELOPMENT_AAGUIDS = {b"appattestdevelop", b"appattestsandbox"}


class AppAttestVerificationError(ValueError):
    """Stable, non-sensitive verification failure."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class VerifiedAttestation:
    public_key_pem: bytes
    receipt: bytes
    environment: str
    validation_category: int | None
    bundle_version: str | None


@dataclass(frozen=True, slots=True)
class VerifiedAssertion:
    counter: int
    validation_category: int | None
    bundle_version: str | None


@dataclass(frozen=True, slots=True)
class _AuthData:
    raw: bytes
    rp_id_hash: bytes
    flags: int
    counter: int
    aaguid: bytes | None = None
    credential_id: bytes | None = None
    extensions: Mapping[str, Any] | None = None


class AppAttestVerifier:
    def __init__(
        self,
        *,
        app_id: str,
        environment: str,
        root_pem: bytes | None = None,
        allowed_bundle_versions: set[str] | None = None,
        require_extended_attributes: bool = False,
    ):
        if not app_id or environment not in {"development", "production"}:
            raise ValueError("invalid App Attest verifier configuration")
        if root_pem is None:
            root_pem = Path(__file__).with_name(
                "apple_app_attestation_root_ca.pem"
            ).read_bytes()
        self.app_id = app_id
        self.environment = environment
        self.root = x509.load_pem_x509_certificate(root_pem)
        self.allowed_bundle_versions = allowed_bundle_versions or set()
        self.require_extended_attributes = require_extended_attributes

    def verify_attestation(
        self,
        *,
        attestation_object: bytes,
        key_id: str,
        challenge: bytes,
        now: datetime | None = None,
    ) -> VerifiedAttestation:
        if len(attestation_object) > MAX_ATTESTATION_BYTES or len(challenge) != 32:
            raise AppAttestVerificationError("invalid_size")
        raw = _decode_cbor_map(attestation_object)
        if raw.get("fmt") != "apple-appattest":
            raise AppAttestVerificationError("invalid_format")
        att_stmt = raw.get("attStmt")
        auth_raw = raw.get("authData")
        if not isinstance(att_stmt, dict) or not isinstance(auth_raw, bytes):
            raise AppAttestVerificationError("invalid_cbor")
        chain_raw = att_stmt.get("x5c")
        receipt = att_stmt.get("receipt")
        if (
            not isinstance(chain_raw, list)
            or not 1 <= len(chain_raw) <= 5
            or not all(isinstance(value, bytes) for value in chain_raw)
            or not isinstance(receipt, bytes)
            or not receipt
        ):
            raise AppAttestVerificationError("invalid_attestation_statement")

        certificates = _load_der_certificates(chain_raw)
        _verify_certificate_chain(certificates, self.root, now or datetime.now(UTC))
        leaf = certificates[0]
        public_key = leaf.public_key()
        if not isinstance(public_key, ec.EllipticCurvePublicKey) or not isinstance(
            public_key.curve, ec.SECP256R1
        ):
            raise AppAttestVerificationError("invalid_public_key")

        auth = _parse_authenticator_data(auth_raw, require_attested_credential=True)
        client_data_hash = hashlib.sha256(challenge).digest()
        nonce = hashlib.sha256(auth_raw + client_data_hash).digest()
        try:
            extension = leaf.extensions.get_extension_for_oid(NONCE_OID).value
        except x509.ExtensionNotFound as exc:
            raise AppAttestVerificationError("nonce_extension_missing") from exc
        extension_bytes = getattr(extension, "value", None)
        if not isinstance(extension_bytes, bytes) or _extract_nonce(extension_bytes) != nonce:
            raise AppAttestVerificationError("nonce_mismatch")

        key_id_bytes = _decode_key_id(key_id)
        public_x962 = public_key.public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
        if hashlib.sha256(public_x962).digest() != key_id_bytes:
            raise AppAttestVerificationError("key_id_mismatch")
        self._verify_common_auth_data(auth)
        if auth.counter != 0:
            raise AppAttestVerificationError("invalid_counter")
        expected_aaguids = (
            {PRODUCTION_AAGUID}
            if self.environment == "production"
            else DEVELOPMENT_AAGUIDS
        )
        if auth.aaguid not in expected_aaguids:
            raise AppAttestVerificationError("environment_mismatch")
        if auth.credential_id != key_id_bytes:
            raise AppAttestVerificationError("credential_id_mismatch")
        category, bundle_version = self._verify_extended_attributes(auth.extensions)
        public_key_pem = public_key.public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return VerifiedAttestation(
            public_key_pem=public_key_pem,
            receipt=receipt,
            environment=self.environment,
            validation_category=category,
            bundle_version=bundle_version,
        )

    def verify_assertion(
        self,
        *,
        assertion_object: bytes,
        client_data: bytes,
        public_key_pem: bytes,
        previous_counter: int,
    ) -> VerifiedAssertion:
        if (
            len(assertion_object) > MAX_ASSERTION_BYTES
            or len(client_data) > MAX_CLIENT_DATA_BYTES
        ):
            raise AppAttestVerificationError("invalid_size")
        raw = _decode_cbor_map(assertion_object)
        signature = raw.get("signature")
        auth_raw = raw.get("authenticatorData")
        if not isinstance(signature, bytes) or not isinstance(auth_raw, bytes):
            raise AppAttestVerificationError("invalid_cbor")
        auth = _parse_authenticator_data(auth_raw, require_attested_credential=False)
        self._verify_common_auth_data(auth)
        if auth.counter <= previous_counter or auth.counter <= 0:
            raise AppAttestVerificationError("counter_replay")

        try:
            public_key = serialization.load_pem_public_key(public_key_pem)
        except (TypeError, ValueError) as exc:
            raise AppAttestVerificationError("invalid_public_key") from exc
        if not isinstance(public_key, ec.EllipticCurvePublicKey) or not isinstance(
            public_key.curve, ec.SECP256R1
        ):
            raise AppAttestVerificationError("invalid_public_key")
        client_data_hash = hashlib.sha256(client_data).digest()
        nonce = hashlib.sha256(auth_raw + client_data_hash).digest()
        try:
            # App Attest signs the calculated nonce using ECDSA with SHA-256.
            public_key.verify(signature, nonce, ec.ECDSA(hashes.SHA256()))
        except InvalidSignature as exc:
            raise AppAttestVerificationError("invalid_signature") from exc
        category, bundle_version = self._verify_extended_attributes(auth.extensions)
        return VerifiedAssertion(
            counter=auth.counter,
            validation_category=category,
            bundle_version=bundle_version,
        )

    def _verify_common_auth_data(self, auth: _AuthData) -> None:
        if auth.rp_id_hash != hashlib.sha256(self.app_id.encode("utf-8")).digest():
            raise AppAttestVerificationError("rp_id_mismatch")

    def _verify_extended_attributes(
        self, extensions: Mapping[str, Any] | None
    ) -> tuple[int | None, str | None]:
        category = _validation_category(extensions)
        bundle_version = _bundle_version(extensions)
        if self.require_extended_attributes and (
            category is None or bundle_version is None
        ):
            raise AppAttestVerificationError("extended_attributes_missing")
        if category is not None:
            allowed = {2, 4} if self.environment == "production" else {3}
            if category not in allowed:
                raise AppAttestVerificationError("validation_category_mismatch")
        if (
            bundle_version is not None
            and self.allowed_bundle_versions
            and bundle_version not in self.allowed_bundle_versions
        ):
            raise AppAttestVerificationError("bundle_version_mismatch")
        return category, bundle_version


def canonical_json(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AppAttestVerificationError("invalid_client_data") from exc


def build_app_client_data(
    *,
    challenge_id: str,
    challenge: bytes,
    payload: Mapping[str, Any],
    issued_at: int,
    path: str,
) -> bytes:
    if len(challenge) != 32:
        raise AppAttestVerificationError("invalid_challenge")
    if path not in {
        "/v1/app/feed",
        "/v1/app/discovery",
        "/v1/app/push/registrations",
        "/v1/app/privacy/consents",
        "/v1/app/article-views",
    }:
        raise AppAttestVerificationError("invalid_client_data")
    payload_hash = hashlib.sha256(canonical_json(payload)).hexdigest()
    return canonical_json(
        {
            "body_sha256": payload_hash,
            "challenge": base64.urlsafe_b64encode(challenge).rstrip(b"=").decode("ascii"),
            "challenge_id": challenge_id,
            "issued_at": int(issued_at),
            "method": "POST",
            "path": path,
            "version": 1,
        }
    )


def build_feed_client_data(
    *,
    challenge_id: str,
    challenge: bytes,
    payload: Mapping[str, Any],
    issued_at: int,
) -> bytes:
    """Backward-compatible feed wrapper used by existing tests and callers."""
    return build_app_client_data(
        challenge_id=challenge_id,
        challenge=challenge,
        payload=payload,
        issued_at=issued_at,
        path="/v1/app/feed",
    )


def _decode_cbor_map(raw: bytes) -> dict:
    try:
        value = cbor2.loads(raw)
    except (ValueError, TypeError, cbor2.CBORDecodeError) as exc:
        raise AppAttestVerificationError("invalid_cbor") from exc
    if not isinstance(value, dict):
        raise AppAttestVerificationError("invalid_cbor")
    return value


def _load_der_certificates(values: list[bytes]) -> list[x509.Certificate]:
    try:
        return [x509.load_der_x509_certificate(value) for value in values]
    except ValueError as exc:
        raise AppAttestVerificationError("invalid_chain") from exc


def _verify_certificate_chain(
    certificates: list[x509.Certificate], root: x509.Certificate, now: datetime
) -> None:
    now = now.astimezone(UTC)
    chain = [*certificates, root]
    try:
        for cert in chain:
            if now < cert.not_valid_before_utc or now > cert.not_valid_after_utc:
                raise AppAttestVerificationError("invalid_chain")
        leaf = certificates[0]
        try:
            if leaf.extensions.get_extension_for_oid(
                ExtensionOID.BASIC_CONSTRAINTS
            ).value.ca:
                raise AppAttestVerificationError("invalid_chain")
        except x509.ExtensionNotFound:
            pass
        try:
            key_usage = leaf.extensions.get_extension_for_oid(
                ExtensionOID.KEY_USAGE
            ).value
            if not key_usage.digital_signature:
                raise AppAttestVerificationError("invalid_chain")
        except x509.ExtensionNotFound:
            pass
        for child, issuer in zip(chain, chain[1:]):
            if child.issuer != issuer.subject:
                raise AppAttestVerificationError("invalid_chain")
            constraints = issuer.extensions.get_extension_for_oid(
                ExtensionOID.BASIC_CONSTRAINTS
            ).value
            if not constraints.ca:
                raise AppAttestVerificationError("invalid_chain")
            _verify_certificate_signature(child, issuer)
    except (x509.ExtensionNotFound, InvalidSignature) as exc:
        raise AppAttestVerificationError("invalid_chain") from exc


def _verify_certificate_signature(
    certificate: x509.Certificate, issuer: x509.Certificate
) -> None:
    public_key = issuer.public_key()
    if isinstance(public_key, ec.EllipticCurvePublicKey):
        public_key.verify(
            certificate.signature,
            certificate.tbs_certificate_bytes,
            ec.ECDSA(certificate.signature_hash_algorithm),
        )
    elif isinstance(public_key, rsa.RSAPublicKey):
        public_key.verify(
            certificate.signature,
            certificate.tbs_certificate_bytes,
            padding.PKCS1v15(),
            certificate.signature_hash_algorithm,
        )
    else:
        raise AppAttestVerificationError("invalid_chain")


def _parse_authenticator_data(
    raw: bytes, *, require_attested_credential: bool
) -> _AuthData:
    if len(raw) < 37:
        raise AppAttestVerificationError("invalid_authenticator_data")
    rp_id_hash = raw[:32]
    flags = raw[32]
    counter = struct.unpack("!I", raw[33:37])[0]
    offset = 37
    aaguid: bytes | None = None
    credential_id: bytes | None = None
    # Attested credential data is only present in the *attestation* authData.
    # Apple's App Attest *assertions* set the AT flag (0x40) yet carry no
    # attested credential data (the authData is exactly 37 bytes), so we must
    # gate this parse on the caller's intent, not on the flag. Assertion
    # integrity is guaranteed by the signature over authData||clientDataHash,
    # the counter check, and the RP ID hash check — none of which need this.
    if require_attested_credential:
        if not (flags & 0x40):
            raise AppAttestVerificationError("invalid_authenticator_data")
        if len(raw) < offset + 18:
            raise AppAttestVerificationError("invalid_authenticator_data")
        aaguid = raw[offset : offset + 16]
        credential_length = struct.unpack("!H", raw[offset + 16 : offset + 18])[0]
        offset += 18
        if len(raw) < offset + credential_length:
            raise AppAttestVerificationError("invalid_authenticator_data")
        credential_id = raw[offset : offset + credential_length]
        offset += credential_length
        offset = _consume_one_cbor(raw, offset)

    extensions: Mapping[str, Any] | None = None
    if flags & 0x80:
        try:
            decoded, offset = _decode_one_cbor(raw, offset)
        except (ValueError, cbor2.CBORDecodeError) as exc:
            raise AppAttestVerificationError("invalid_authenticator_data") from exc
        if not isinstance(decoded, dict):
            raise AppAttestVerificationError("invalid_authenticator_data")
        extensions = decoded
    if offset != len(raw):
        raise AppAttestVerificationError("invalid_authenticator_data")
    return _AuthData(
        raw=raw,
        rp_id_hash=rp_id_hash,
        flags=flags,
        counter=counter,
        aaguid=aaguid,
        credential_id=credential_id,
        extensions=extensions,
    )


def _consume_one_cbor(raw: bytes, offset: int) -> int:
    _, new_offset = _decode_one_cbor(raw, offset)
    return new_offset


def _decode_one_cbor(raw: bytes, offset: int) -> tuple[Any, int]:
    stream = io.BytesIO(raw[offset:])
    decoded = cbor2.CBORDecoder(stream).decode()
    return decoded, offset + stream.tell()


def _decode_key_id(key_id: str) -> bytes:
    try:
        padded = key_id + "=" * (-len(key_id) % 4)
        decoded = base64.b64decode(padded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise AppAttestVerificationError("invalid_key_id") from exc
    if len(decoded) != 32:
        raise AppAttestVerificationError("invalid_key_id")
    return decoded


def _extract_nonce(raw_der: bytes) -> bytes:
    values = _collect_der_octet_strings(raw_der)
    matches = [value for value in values if len(value) == 32]
    if len(matches) != 1:
        raise AppAttestVerificationError("invalid_nonce_extension")
    return matches[0]


def _collect_der_octet_strings(data: bytes) -> list[bytes]:
    out: list[bytes] = []
    offset = 0
    while offset < len(data):
        tag = data[offset]
        offset += 1
        length, offset = _der_length(data, offset)
        end = offset + length
        if end > len(data):
            raise AppAttestVerificationError("invalid_nonce_extension")
        value = data[offset:end]
        if tag == 0x04:
            out.append(value)
        elif tag == 0x30 or tag & 0x20:
            out.extend(_collect_der_octet_strings(value))
        offset = end
    return out


def _der_length(data: bytes, offset: int) -> tuple[int, int]:
    if offset >= len(data):
        raise AppAttestVerificationError("invalid_nonce_extension")
    first = data[offset]
    offset += 1
    if first < 0x80:
        return first, offset
    count = first & 0x7F
    if count == 0 or count > 4 or offset + count > len(data):
        raise AppAttestVerificationError("invalid_nonce_extension")
    return int.from_bytes(data[offset : offset + count], "big"), offset + count


def _validation_category(extensions: Mapping[str, Any] | None) -> int | None:
    if not extensions or "apple_validation_category_01" not in extensions:
        return None
    value = extensions["apple_validation_category_01"]
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, bytes) and len(value) == 4:
        return int.from_bytes(value, "little")
    raise AppAttestVerificationError("invalid_extended_attributes")


def _bundle_version(extensions: Mapping[str, Any] | None) -> str | None:
    if not extensions or "apple_bundle_version_01" not in extensions:
        return None
    value = extensions["apple_bundle_version_01"]
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AppAttestVerificationError("invalid_extended_attributes") from exc
    if not isinstance(value, str) or not value or len(value) > 64:
        raise AppAttestVerificationError("invalid_extended_attributes")
    return value
