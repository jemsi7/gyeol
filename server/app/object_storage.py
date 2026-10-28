"""Private S3-compatible object storage for article images.

Permanent credentials stay inside the SDK client. Callers persist only the
deterministic object key and hand clients short-lived presigned GET URLs.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import boto3
from botocore.config import Config

from .config import Settings

_IMAGE_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_OBJECT_KEY_RE = re.compile(
    r"^gdelt/[0-9a-f]{64}\.(?:jpg|png|gif|webp|avif|img)$"
)
_VERIFY_CHUNK_BYTES = 1024 * 1024
_CONTENT_TYPE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/avif": ".avif",
}


class ObjectStorageError(RuntimeError):
    """A redacted SDK/storage failure safe to expose to application logs."""

    def __init__(self, operation: str, key: str):
        self.operation = operation
        self.key = key
        super().__init__(f"object storage {operation} failed for {key}")


@dataclass(frozen=True, slots=True)
class ObjectVerification:
    key: str
    sha256: str
    byte_size: int
    matches: bool


class S3ObjectStore:
    def __init__(self, settings: Settings, client: Any | None = None):
        settings.validate_s3()
        self.s = settings
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = boto3.client(
                "s3",
                endpoint_url=self.s.s3_endpoint_url,
                region_name=self.s.s3_region,
                aws_access_key_id=self.s.s3_access_key_id.get_secret_value(),
                aws_secret_access_key=self.s.s3_secret_access_key.get_secret_value(),
                config=Config(
                    signature_version="s3v4",
                    s3={"addressing_style": self.s.s3_url_style},
                    retries={"max_attempts": 3, "mode": "standard"},
                ),
            )
        return self._client

    def key_for(self, image_id: str, content_type: str | None) -> str:
        _validate_image_id(image_id)
        normalized_type = _normalize_content_type(content_type)
        extension = _CONTENT_TYPE_EXTENSIONS.get(normalized_type, ".img")
        return f"gdelt/{image_id}{extension}"

    def put_image(
        self, image_id: str, data: bytes, content_type: str | None
    ) -> str:
        _validate_image_id(image_id)
        if not isinstance(data, bytes):
            raise TypeError("image data must be bytes")
        if len(data) > self.s.image_max_bytes:
            raise ValueError("image exceeds IMAGE_MAX_BYTES")
        actual_sha256 = hashlib.sha256(data).hexdigest()
        if actual_sha256 != image_id:
            raise ValueError("image sha256 does not match image_id")

        key = self.key_for(image_id, content_type)
        normalized_type = _normalize_content_type(content_type)
        try:
            self.client.put_object(
                Bucket=self.s.s3_bucket,
                Key=key,
                Body=data,
                ContentType=normalized_type or "application/octet-stream",
                CacheControl="public, max-age=31536000, immutable",
                Metadata={
                    "sha256": image_id,
                    "byte-size": str(len(data)),
                },
            )
        except Exception:
            raise ObjectStorageError("put", key) from None
        return key

    def verify_image(
        self,
        key: str,
        *,
        expected_sha256: str,
        expected_size: int,
    ) -> ObjectVerification:
        _validate_object_key(key)
        _validate_image_id(expected_sha256)
        if expected_size < 0:
            raise ValueError("expected_size must be non-negative")

        body: Any | None = None
        digest = hashlib.sha256()
        byte_size = 0
        try:
            response = self.client.get_object(Bucket=self.s.s3_bucket, Key=key)
            body = response.get("Body")
            if body is None or not callable(getattr(body, "read", None)):
                raise RuntimeError("object body is unavailable")
            while True:
                chunk = body.read(_VERIFY_CHUNK_BYTES)
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise RuntimeError("object body returned non-bytes data")
                digest.update(chunk)
                byte_size += len(chunk)
        except Exception:
            raise ObjectStorageError("verify", key) from None
        finally:
            if body is not None:
                try:
                    body.close()
                except Exception:
                    pass

        actual_sha256 = digest.hexdigest()
        return ObjectVerification(
            key=key,
            sha256=actual_sha256,
            byte_size=byte_size,
            matches=(
                actual_sha256 == expected_sha256 and byte_size == expected_size
            ),
        )

    def read_verified_image(
        self,
        key: str,
        *,
        expected_sha256: str,
        expected_size: int,
    ) -> bytes:
        """Read one bounded object and return bytes only after hash/size match."""
        _validate_object_key(key)
        _validate_image_id(expected_sha256)
        if expected_size < 0 or expected_size > self.s.image_max_bytes:
            raise ValueError("expected_size is outside IMAGE_MAX_BYTES")

        body: Any | None = None
        payload = bytearray()
        try:
            response = self.client.get_object(Bucket=self.s.s3_bucket, Key=key)
            body = response.get("Body")
            if body is None or not callable(getattr(body, "read", None)):
                raise RuntimeError("object body is unavailable")
            while True:
                chunk = body.read(_VERIFY_CHUNK_BYTES)
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise RuntimeError("object body returned non-bytes data")
                payload.extend(chunk)
                if len(payload) > self.s.image_max_bytes:
                    raise RuntimeError("object exceeds IMAGE_MAX_BYTES")
        except Exception:
            raise ObjectStorageError("read", key) from None
        finally:
            if body is not None:
                try:
                    body.close()
                except Exception:
                    pass

        data = bytes(payload)
        if len(data) != expected_size or hashlib.sha256(data).hexdigest() != expected_sha256:
            raise ObjectStorageError("verify", key)
        return data

    def presign_get(self, key: str) -> str:
        _validate_object_key(key)
        try:
            url = self.client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.s.s3_bucket, "Key": key},
                ExpiresIn=self.s.s3_presign_ttl_s,
            )
        except Exception:
            raise ObjectStorageError("presign", key) from None
        parsed = urlparse(url) if isinstance(url, str) else None
        if parsed is None or parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ObjectStorageError("presign", key)
        return url


def _validate_image_id(image_id: str) -> None:
    if not isinstance(image_id, str) or not _IMAGE_ID_RE.fullmatch(image_id):
        raise ValueError("image_id must be a lowercase sha256 hex string")


def _validate_object_key(key: str) -> None:
    if not isinstance(key, str) or not _OBJECT_KEY_RE.fullmatch(key):
        raise ValueError("invalid image object key")


def _normalize_content_type(content_type: str | None) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()
