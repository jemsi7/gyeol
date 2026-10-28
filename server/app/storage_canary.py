"""Secret-free live canary for the configured private image Bucket."""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from .config import Settings, get_settings
from .object_storage import ObjectStorageError, S3ObjectStore

CANARY_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
CANARY_CONTENT_TYPE = "image/png"


class StorageCanaryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class StorageCanaryReport:
    image_id: str
    object_key: str
    byte_size: int
    read_back_matches: bool
    presigned_get_matches: bool
    ok: bool


def run_storage_canary(
    settings: Settings,
    *,
    store: S3ObjectStore | None = None,
    http_client: Any | None = None,
) -> StorageCanaryReport:
    object_store = store or S3ObjectStore(settings)
    image_id = hashlib.sha256(CANARY_BYTES).hexdigest()
    key = object_store.put_image(image_id, CANARY_BYTES, CANARY_CONTENT_TYPE)
    verification = object_store.verify_image(
        key,
        expected_sha256=image_id,
        expected_size=len(CANARY_BYTES),
    )
    if not verification.matches:
        raise StorageCanaryError("object read-back verification failed")

    presigned_url = object_store.presign_get(key)
    owns_client = http_client is None
    client = http_client or httpx.Client(timeout=30.0, follow_redirects=True)
    try:
        response = client.get(presigned_url)
        if response.status_code != 200:
            raise StorageCanaryError("presigned GET returned a non-200 status")
        payload = bytes(response.content)
    except StorageCanaryError:
        raise
    except Exception:
        raise StorageCanaryError("presigned GET failed") from None
    finally:
        if owns_client:
            client.close()

    get_matches = (
        len(payload) == len(CANARY_BYTES)
        and hashlib.sha256(payload).hexdigest() == image_id
    )
    if not get_matches:
        raise StorageCanaryError("presigned GET payload verification failed")

    return StorageCanaryReport(
        image_id=image_id,
        object_key=key,
        byte_size=len(CANARY_BYTES),
        read_back_matches=True,
        presigned_get_matches=True,
        ok=True,
    )


def main() -> int:
    try:
        report = run_storage_canary(get_settings())
    except (ObjectStorageError, StorageCanaryError, ValueError):
        print(json.dumps({"ok": False, "error": "storage_canary_failed"}))
        return 1
    print(json.dumps(asdict(report), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
