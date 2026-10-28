from __future__ import annotations

import hashlib
import io

import pytest

from app.config import Settings
from app.object_storage import ObjectStorageError, S3ObjectStore


def _settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "IMAGE_BACKEND": "s3",
        "S3_BUCKET": "article-images",
        "S3_ENDPOINT_URL": "https://storage.example",
        "S3_REGION": "sin",
        "S3_ACCESS_KEY_ID": "access-secret-value",
        "S3_SECRET_ACCESS_KEY": "permanent-secret-value",
        "S3_URL_STYLE": "virtual",
        "S3_PRESIGN_TTL_S": 900,
        "S3_REDIRECT_CACHE_S": 60,
    }
    values.update(overrides)
    return Settings(**values)


class FakeBody:
    def __init__(self, data: bytes):
        self._stream = io.BytesIO(data)
        self.closed = False
        self.read_sizes: list[int] = []

    def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        return self._stream.read(size)

    def close(self) -> None:
        self.closed = True
        self._stream.close()


class FakeS3Client:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.put_calls: list[dict] = []
        self.last_body: FakeBody | None = None
        self.presign_calls: list[tuple[str, dict, int]] = []

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)
        self.objects[kwargs["Key"]] = kwargs["Body"]
        return {"ETag": "not-used-for-integrity"}

    def get_object(self, *, Bucket, Key):
        self.last_body = FakeBody(self.objects[Key])
        return {"Body": self.last_body}

    def generate_presigned_url(self, operation, *, Params, ExpiresIn):
        self.presign_calls.append((operation, Params, ExpiresIn))
        return f"https://{Params['Bucket']}.storage.example/{Params['Key']}?signed=1"


def test_key_is_deterministic_and_rejects_invalid_id():
    store = S3ObjectStore(_settings(), FakeS3Client())
    image_id = "a" * 64

    assert store.key_for(image_id, "image/jpeg; charset=binary") == (
        f"gdelt/{image_id}.jpg"
    )
    assert store.key_for(image_id, "application/octet-stream") == (
        f"gdelt/{image_id}.img"
    )
    with pytest.raises(ValueError, match="lowercase sha256"):
        store.key_for("A" * 64, "image/png")


def test_put_rejects_hash_mismatch_before_network():
    client = FakeS3Client()
    store = S3ObjectStore(_settings(), client)

    with pytest.raises(ValueError, match="does not match"):
        store.put_image("0" * 64, b"actual image", "image/png")

    assert client.put_calls == []


def test_put_sets_integrity_metadata_and_immutable_cache():
    data = b"small deterministic png payload"
    image_id = hashlib.sha256(data).hexdigest()
    client = FakeS3Client()
    store = S3ObjectStore(_settings(), client)

    key = store.put_image(image_id, data, "image/png")

    assert key == f"gdelt/{image_id}.png"
    assert client.put_calls == [
        {
            "Bucket": "article-images",
            "Key": key,
            "Body": data,
            "ContentType": "image/png",
            "CacheControl": "public, max-age=31536000, immutable",
            "Metadata": {"sha256": image_id, "byte-size": str(len(data))},
        }
    ]


def test_verify_streams_hash_and_closes_body():
    data = b"x" * (1024 * 1024 + 17)
    image_id = hashlib.sha256(data).hexdigest()
    client = FakeS3Client()
    store = S3ObjectStore(_settings(IMAGE_MAX_BYTES=len(data)), client)
    key = store.put_image(image_id, data, "image/webp")

    result = store.verify_image(
        key, expected_sha256=image_id, expected_size=len(data)
    )

    assert result.matches is True
    assert result.sha256 == image_id
    assert result.byte_size == len(data)
    assert client.last_body is not None and client.last_body.closed is True
    assert client.last_body.read_sizes == [1024 * 1024, 1024 * 1024, 1024 * 1024]


def test_verify_detects_corruption_without_claiming_success():
    expected = b"expected"
    image_id = hashlib.sha256(expected).hexdigest()
    key = f"gdelt/{image_id}.jpg"
    client = FakeS3Client()
    client.objects[key] = b"corrupt"
    store = S3ObjectStore(_settings(), client)

    result = store.verify_image(
        key, expected_sha256=image_id, expected_size=len(expected)
    )

    assert result.matches is False
    assert client.last_body is not None and client.last_body.closed is True


def test_read_verified_image_returns_bytes_only_after_integrity_match():
    data = b"verified jpeg bytes"
    image_id = hashlib.sha256(data).hexdigest()
    client = FakeS3Client()
    store = S3ObjectStore(_settings(), client)
    key = store.put_image(image_id, data, "image/jpeg")

    result = store.read_verified_image(
        key,
        expected_sha256=image_id,
        expected_size=len(data),
    )

    assert result == data
    assert client.last_body is not None and client.last_body.closed is True


def test_read_verified_image_rejects_corruption_and_oversized_expectation():
    expected = b"expected"
    image_id = hashlib.sha256(expected).hexdigest()
    key = f"gdelt/{image_id}.png"
    client = FakeS3Client()
    client.objects[key] = b"corrupt"
    store = S3ObjectStore(_settings(IMAGE_MAX_BYTES=32), client)

    with pytest.raises(ObjectStorageError) as exc_info:
        store.read_verified_image(
            key,
            expected_sha256=image_id,
            expected_size=len(expected),
        )
    assert "verify failed" in str(exc_info.value)

    with pytest.raises(ValueError, match="IMAGE_MAX_BYTES"):
        store.read_verified_image(
            key,
            expected_sha256=image_id,
            expected_size=33,
        )


def test_presign_uses_configured_ttl():
    image_id = "b" * 64
    key = f"gdelt/{image_id}.avif"
    client = FakeS3Client()
    store = S3ObjectStore(_settings(S3_PRESIGN_TTL_S=600), client)

    url = store.presign_get(key)

    assert url.startswith("https://article-images.storage.example/")
    assert client.presign_calls == [
        (
            "get_object",
            {"Bucket": "article-images", "Key": key},
            600,
        )
    ]


def test_sdk_error_is_redacted():
    class FailingClient(FakeS3Client):
        def put_object(self, **kwargs):
            raise RuntimeError("permanent-secret-value")

    data = b"image"
    image_id = hashlib.sha256(data).hexdigest()
    store = S3ObjectStore(_settings(), FailingClient())

    with pytest.raises(ObjectStorageError) as exc_info:
        store.put_image(image_id, data, "image/png")

    message = str(exc_info.value)
    assert "permanent-secret-value" not in message
    assert "access-secret-value" not in message
    assert message == f"object storage put failed for gdelt/{image_id}.png"
