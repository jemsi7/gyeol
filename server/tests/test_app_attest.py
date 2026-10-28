"""Offline cryptographic tests for Apple App Attest verification."""
from __future__ import annotations

import base64
import hashlib
import struct
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import cbor2
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID, ObjectIdentifier

from app.app_attest import (
    AppAttestVerificationError,
    AppAttestVerifier,
    _parse_authenticator_data,
    build_app_client_data,
    build_feed_client_data,
    canonical_json,
)

APP_ID = "TEAM123456.com.example.news"
NONCE_OID = ObjectIdentifier("1.2.840.113635.100.8.2")


@dataclass
class Vector:
    verifier: AppAttestVerifier
    attestation: bytes
    key_id: str
    challenge: bytes
    leaf_key: ec.EllipticCurvePrivateKey
    public_key_pem: bytes


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _key_usage(is_ca: bool) -> x509.KeyUsage:
    return x509.KeyUsage(
        digital_signature=True,
        content_commitment=False,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=is_ca,
        crl_sign=is_ca,
        encipher_only=None,
        decipher_only=None,
    )


def _ca_certificate(
    subject: x509.Name,
    key: ec.EllipticCurvePrivateKey,
    issuer: x509.Name,
    issuer_key: ec.EllipticCurvePrivateKey,
) -> x509.Certificate:
    now = datetime.now(UTC)
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(_key_usage(True), critical=True)
        .sign(issuer_key, hashes.SHA256())
    )


def _auth_data(
    *,
    key_id: bytes,
    rp_id: str = APP_ID,
    counter: int = 0,
    aaguid: bytes = b"appattestdevelop",
    credential_id: bytes | None = None,
    include_extensions: bool = True,
) -> bytes:
    public_cose_key = {1: 2, 3: -7, -1: 1, -2: bytes(32), -3: bytes(32)}
    flags = 0x40 | (0x80 if include_extensions else 0)
    credential = credential_id if credential_id is not None else key_id
    raw = (
        hashlib.sha256(rp_id.encode()).digest()
        + bytes([flags])
        + struct.pack("!I", counter)
        + aaguid
        + struct.pack("!H", len(credential))
        + credential
        + cbor2.dumps(public_cose_key)
    )
    if include_extensions:
        raw += cbor2.dumps(
            {
                "apple_validation_category_01": (3).to_bytes(4, "little"),
                "apple_bundle_version_01": "1",
            }
        )
    return raw


def _vector(
    *,
    rp_id: str = APP_ID,
    counter: int = 0,
    aaguid: bytes = b"appattestdevelop",
    credential_id: bytes | None = None,
) -> Vector:
    root_key = ec.generate_private_key(ec.SECP256R1())
    root_name = _name("Test App Attest Root")
    root = _ca_certificate(root_name, root_key, root_name, root_key)
    intermediate_key = ec.generate_private_key(ec.SECP256R1())
    intermediate_name = _name("Test App Attest Intermediate")
    intermediate = _ca_certificate(
        intermediate_name, intermediate_key, root.subject, root_key
    )
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    public_x962 = leaf_key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    key_id_bytes = hashlib.sha256(public_x962).digest()
    key_id = base64.b64encode(key_id_bytes).decode()
    challenge = bytes(range(32))
    auth_data = _auth_data(
        key_id=key_id_bytes,
        rp_id=rp_id,
        counter=counter,
        aaguid=aaguid,
        credential_id=credential_id,
    )
    nonce = hashlib.sha256(auth_data + hashlib.sha256(challenge).digest()).digest()
    # Apple's unrecognized extension is a SEQUENCE/context wrapper around one
    # 32-byte OCTET STRING: 30 24 A1 22 04 20 <nonce>.
    nonce_der = b"\x30\x24\xa1\x22\x04\x20" + nonce
    now = datetime.now(UTC)
    leaf = (
        x509.CertificateBuilder()
        .subject_name(_name("Test Credential Certificate"))
        .issuer_name(intermediate.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(_key_usage(False), critical=True)
        .add_extension(x509.UnrecognizedExtension(NONCE_OID, nonce_der), critical=False)
        .sign(intermediate_key, hashes.SHA256())
    )
    attestation = cbor2.dumps(
        {
            "fmt": "apple-appattest",
            "attStmt": {
                "x5c": [
                    leaf.public_bytes(serialization.Encoding.DER),
                    intermediate.public_bytes(serialization.Encoding.DER),
                ],
                "receipt": b"test-receipt",
            },
            "authData": auth_data,
        }
    )
    root_pem = root.public_bytes(serialization.Encoding.PEM)
    return Vector(
        verifier=AppAttestVerifier(
            app_id=APP_ID,
            environment="development",
            root_pem=root_pem,
            allowed_bundle_versions={"1"},
            require_extended_attributes=True,
        ),
        attestation=attestation,
        key_id=key_id,
        challenge=challenge,
        leaf_key=leaf_key,
        public_key_pem=leaf_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ),
    )


def _assertion(
    vector: Vector, client_data: bytes, *, counter: int = 1, bad_signature: bool = False
) -> bytes:
    auth_data = (
        hashlib.sha256(APP_ID.encode()).digest()
        + b"\x80"
        + struct.pack("!I", counter)
        + cbor2.dumps(
            {
                "apple_validation_category_01": (3).to_bytes(4, "little"),
                "apple_bundle_version_01": "1",
            }
        )
    )
    nonce = hashlib.sha256(auth_data + hashlib.sha256(client_data).digest()).digest()
    key = ec.generate_private_key(ec.SECP256R1()) if bad_signature else vector.leaf_key
    signature = key.sign(nonce, ec.ECDSA(hashes.SHA256()))
    return cbor2.dumps({"signature": signature, "authenticatorData": auth_data})


def test_verify_attestation_accepts_valid_generated_chain():
    vector = _vector()
    result = vector.verifier.verify_attestation(
        attestation_object=vector.attestation,
        key_id=vector.key_id,
        challenge=vector.challenge,
    )
    assert result.public_key_pem == vector.public_key_pem
    assert result.receipt == b"test-receipt"
    assert result.validation_category == 3
    assert result.bundle_version == "1"


@pytest.mark.parametrize(
    ("field", "expected_code"),
    [
        ("challenge", "nonce_mismatch"),
        ("key_id", "key_id_mismatch"),
    ],
)
def test_verify_attestation_rejects_tampered_binding(field: str, expected_code: str):
    vector = _vector()
    kwargs = {
        "attestation_object": vector.attestation,
        "key_id": vector.key_id,
        "challenge": vector.challenge,
    }
    if field == "challenge":
        kwargs["challenge"] = bytes(reversed(vector.challenge))
    else:
        kwargs["key_id"] = base64.b64encode(bytes(32)).decode()
    with pytest.raises(AppAttestVerificationError) as error:
        vector.verifier.verify_attestation(**kwargs)
    assert error.value.code == expected_code


@pytest.mark.parametrize(
    ("vector", "code"),
    [
        (lambda: _vector(rp_id="TEAM.bad.bundle"), "rp_id_mismatch"),
        (lambda: _vector(counter=1), "invalid_counter"),
        (lambda: _vector(aaguid=b"appattest" + bytes(7)), "environment_mismatch"),
        (lambda: _vector(credential_id=bytes(32)), "credential_id_mismatch"),
    ],
)
def test_verify_attestation_rejects_invalid_auth_data(vector, code: str):
    item = vector()
    with pytest.raises(AppAttestVerificationError) as error:
        item.verifier.verify_attestation(
            attestation_object=item.attestation,
            key_id=item.key_id,
            challenge=item.challenge,
        )
    assert error.value.code == code


def test_verify_attestation_rejects_wrong_root():
    vector = _vector()
    other = _vector()
    verifier = AppAttestVerifier(
        app_id=APP_ID,
        environment="development",
        root_pem=other.verifier.root.public_bytes(serialization.Encoding.PEM),
    )
    with pytest.raises(AppAttestVerificationError) as error:
        verifier.verify_attestation(
            attestation_object=vector.attestation,
            key_id=vector.key_id,
            challenge=vector.challenge,
        )
    assert error.value.code == "invalid_chain"


def test_verify_assertion_accepts_increasing_counter():
    vector = _vector()
    client_data = b'{"challenge":"one-time"}'
    result = vector.verifier.verify_assertion(
        assertion_object=_assertion(vector, client_data, counter=2),
        client_data=client_data,
        public_key_pem=vector.public_key_pem,
        previous_counter=1,
    )
    assert result.counter == 2


def test_verify_assertion_rejects_changed_payload_replay_and_bad_signature():
    vector = _vector()
    client_data = b'{"challenge":"one-time"}'
    good = _assertion(vector, client_data, counter=2)
    with pytest.raises(AppAttestVerificationError) as changed:
        vector.verifier.verify_assertion(
            assertion_object=good,
            client_data=b'{"challenge":"changed"}',
            public_key_pem=vector.public_key_pem,
            previous_counter=1,
        )
    assert changed.value.code == "invalid_signature"

    with pytest.raises(AppAttestVerificationError) as replay:
        vector.verifier.verify_assertion(
            assertion_object=good,
            client_data=client_data,
            public_key_pem=vector.public_key_pem,
            previous_counter=2,
        )
    assert replay.value.code == "counter_replay"

    with pytest.raises(AppAttestVerificationError) as bad:
        vector.verifier.verify_assertion(
            assertion_object=_assertion(
                vector, client_data, counter=3, bad_signature=True
            ),
            client_data=client_data,
            public_key_pem=vector.public_key_pem,
            previous_counter=2,
        )
    assert bad.value.code == "invalid_signature"


def test_canonical_feed_client_data_is_stable_and_bound_to_payload():
    payload = {"limit": 5, "cursor": None, "device": {"platform": "ipados"}}
    first = build_feed_client_data(
        challenge_id="00000000-0000-0000-0000-000000000001",
        challenge=bytes(32),
        payload=payload,
        issued_at=1_700_000_000,
    )
    reordered = build_feed_client_data(
        challenge_id="00000000-0000-0000-0000-000000000001",
        challenge=bytes(32),
        payload={"device": {"platform": "ipados"}, "cursor": None, "limit": 5},
        issued_at=1_700_000_000,
    )
    assert first == reordered
    assert canonical_json({"b": 1, "a": "한글"}) == '{"a":"한글","b":1}'.encode()


def test_discovery_client_data_is_bound_to_its_exact_path():
    payload = {"device": {"platform": "ios"}, "query": None, "limit": 12}
    feed = build_app_client_data(
        challenge_id="00000000-0000-0000-0000-000000000001",
        challenge=bytes(32),
        payload=payload,
        issued_at=1_700_000_000,
        path="/v1/app/feed",
    )
    discovery = build_app_client_data(
        challenge_id="00000000-0000-0000-0000-000000000001",
        challenge=bytes(32),
        payload=payload,
        issued_at=1_700_000_000,
        path="/v1/app/discovery",
    )
    assert feed != discovery
    assert b'"path":"/v1/app/discovery"' in discovery
    with pytest.raises(AppAttestVerificationError, match="invalid_client_data"):
        build_app_client_data(
            challenge_id="00000000-0000-0000-0000-000000000001",
            challenge=bytes(32),
            payload=payload,
            issued_at=1_700_000_000,
            path="/v1/app/unknown",
        )


def test_build_app_client_data_accepts_article_view_path_and_rejects_unknown_paths():
    payload = {
        "event_id": "00000000-0000-0000-0000-000000000002",
        "article_id": "a" * 64,
        "device": {"platform": "ios"},
    }
    article_view = build_app_client_data(
        challenge_id="00000000-0000-0000-0000-000000000001",
        challenge=bytes(32),
        payload=payload,
        issued_at=1_700_000_000,
        path="/v1/app/article-views",
    )

    assert b'"method":"POST"' in article_view
    assert b'"path":"/v1/app/article-views"' in article_view
    with pytest.raises(AppAttestVerificationError, match="invalid_client_data"):
        build_app_client_data(
            challenge_id="00000000-0000-0000-0000-000000000001",
            challenge=bytes(32),
            payload=payload,
            issued_at=1_700_000_000,
            path="/v1/app/article-views/other",
        )


def test_parse_real_device_assertion_authdata_with_at_flag():
    """Regression: real iOS App Attest *assertions* set the AT flag (0x40) but
    carry no attested credential data (authData is exactly 37 bytes). Captured
    from a physical device (iOS 26.5). The parser must not try to read
    attested credential data on the assertion path, or it raises
    invalid_authenticator_data and every feed load fails.
    """
    # <32B rpIdHash> <flags=0x40> <counter=0x00000001>
    auth_data = bytes.fromhex(
        "d653c4d5d07c8debc5c9de364d66cb5f806df776b7e4eda3450b4d2abb181cac"
        "40"
        "00000001"
    )
    assert len(auth_data) == 37
    assert auth_data[32] & 0x40  # AT flag is set by Apple even for assertions

    parsed = _parse_authenticator_data(auth_data, require_attested_credential=False)
    assert parsed.counter == 1
    assert parsed.aaguid is None
    assert parsed.credential_id is None
    assert parsed.extensions is None

    # The attestation path still requires real attested credential data.
    with pytest.raises(AppAttestVerificationError) as exc:
        _parse_authenticator_data(auth_data, require_attested_credential=True)
    assert exc.value.code == "invalid_authenticator_data"
