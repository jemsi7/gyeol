"""Authentication and domain services for first-party editorial tools."""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .config import Settings
from .editorial_models import EditorPrincipal
from .models import StoredImage
from .object_storage import ObjectStorageError, S3ObjectStore


class EditorialError(RuntimeError):
    def __init__(self, status_code: int, code: str, *, detail: Any = None):
        self.status_code = status_code
        self.code = code
        self.detail = detail
        super().__init__(code)


_BEARER_RE = re.compile(r"^Bearer[ \t]+([^ \t\r\n]+)$", re.IGNORECASE)


def extract_editor_bearer(authorization: str | None) -> str:
    if authorization is None:
        raise EditorialError(401, "invalid_or_missing_editor_token")
    match = _BEARER_RE.fullmatch(authorization.strip())
    if match is None:
        raise EditorialError(401, "invalid_or_missing_editor_token")
    token = match.group(1)
    if not 32 <= len(token) <= 128:
        raise EditorialError(401, "invalid_or_missing_editor_token")
    return token


def editor_token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


async def authenticate_editor(repo: Any, authorization: str | None) -> EditorPrincipal:
    token = extract_editor_bearer(authorization)
    row = await repo.authenticate_editor_token(editor_token_hash(token))
    if row is None:
        raise EditorialError(401, "invalid_or_missing_editor_token")
    return EditorPrincipal(
        id=row["id"],
        display_name=row["display_name"],
        role=row["role"],
        created_at=row["created_at"],
        last_used_at=row.get("last_used_at"),
    )


_EDITORIAL_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
_EDITORIAL_IMAGE_CHUNK_BYTES = 64 * 1024


def _detected_editorial_image_type(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _editorial_upload_source(filename: str | None) -> str:
    leaf = os.path.basename((filename or "upload").replace("\x00", "")).strip()
    leaf = re.sub(r"[\r\n\t]+", "-", leaf)[:120] or "upload"
    return f"editorial-upload://{quote(leaf, safe='._-')}"


def _write_editorial_local(
    settings: Settings,
    image_id: str,
    data: bytes,
    content_type: str,
) -> str:
    extension = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
    }[content_type]
    directory = Path(settings.image_local_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{image_id}{extension}"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return str(path)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
    except Exception:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return str(path)


async def store_editorial_image(
    upload: Any,
    operator: EditorPrincipal,
    *,
    settings: Settings,
    repo: Any,
    object_store: S3ObjectStore | Any | None = None,
) -> dict:
    """Validate one multipart image and persist it content-addressably."""
    content_type = (getattr(upload, "content_type", None) or "").split(";", 1)[0]
    content_type = content_type.strip().lower()
    if content_type not in _EDITORIAL_IMAGE_TYPES:
        raise EditorialError(415, "unsupported_editorial_image_type")
    if settings.image_backend == "none":
        raise EditorialError(503, "editorial_image_storage_unavailable")

    payload = bytearray()
    while True:
        chunk = await upload.read(_EDITORIAL_IMAGE_CHUNK_BYTES)
        if not chunk:
            break
        payload.extend(chunk)
        if len(payload) > settings.image_max_bytes:
            raise EditorialError(413, "editorial_image_too_large")
    data = bytes(payload)
    if not data:
        raise EditorialError(422, "empty_editorial_image")
    if _detected_editorial_image_type(data) != content_type:
        raise EditorialError(422, "editorial_image_signature_mismatch")

    image_id = hashlib.sha256(data).hexdigest()
    source_url = _editorial_upload_source(getattr(upload, "filename", None))
    storage_ref: str | None = None
    db_bytes: bytes | None = None
    try:
        if settings.image_backend == "db":
            db_bytes = data
        elif settings.image_backend == "local":
            storage_ref = await asyncio.to_thread(
                _write_editorial_local,
                settings,
                image_id,
                data,
                content_type,
            )
        elif settings.image_backend == "s3":
            if object_store is None:
                raise ObjectStorageError("put", f"gdelt/{image_id}")
            storage_ref = await asyncio.to_thread(
                object_store.put_image,
                image_id,
                data,
                content_type,
            )
        else:
            raise EditorialError(503, "editorial_image_storage_unavailable")
    except EditorialError:
        raise
    except (ObjectStorageError, OSError, ValueError):
        raise EditorialError(503, "editorial_image_storage_unavailable") from None

    image = StoredImage(
        id=image_id,
        source_url=source_url,
        content_type=content_type,
        byte_size=len(data),
        backend=settings.image_backend,
        storage_ref=storage_ref,
        data=db_bytes,
    )
    try:
        await repo.store_editorial_image_metadata(
            image=image,
            operator_id=operator.id,
            filename=os.path.basename(getattr(upload, "filename", None) or "upload")[:120],
        )
    except Exception:
        raise EditorialError(503, "editorial_image_metadata_unavailable") from None
    return {
        "image_id": image_id,
        "content_type": content_type,
        "byte_size": len(data),
        "url": f"/images/{image_id}",
    }
