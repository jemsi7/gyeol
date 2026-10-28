"""Download article images and persist them via a pluggable backend.

Backends
--------
db    : bytes stored in Postgres (article_images.data). Durable on Railway,
        zero extra infra. Default.
s3    : upload to a private S3-compatible bucket; Postgres keeps only the
        deterministic object key and metadata.
local : write to a directory. Convenient locally, but Railway's filesystem is
        ephemeral, so don't rely on it in production.
none  : skip byte download entirely (socialimage_url is still stored on the
        article, so you keep the reference).

Dedup: the image id is sha256(bytes). Identical stock photos shared across
many articles collapse to a single stored object; every article simply points
at the same image_id — that is the article↔photo pairing.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from typing import Optional

import httpx

from dataclasses import dataclass

from .config import Settings
from .models import Article, StoredImage
from .object_storage import S3ObjectStore
from .repository import Repository
from .telemetry import NullWorkerTelemetry, WorkerTelemetryProtocol

log = logging.getLogger("gdelt.images")

_ALLOWED_PREFIXES = ("image/",)


@dataclass(slots=True)
class StoredRef:
    """Returned by store_url so callers (e.g. the mapper) can both persist a
    link AND build a data URL for the vision model without re-downloading."""

    image_id: str
    content_type: str | None
    byte_size: int
    data: bytes


class ImageFetcher:
    def __init__(
        self,
        settings: Settings,
        repo: Repository,
        client: Optional[httpx.AsyncClient] = None,
        telemetry: WorkerTelemetryProtocol | None = None,
        object_store: S3ObjectStore | None = None,
    ):
        self.s = settings
        self.repo = repo
        self._client = client
        self._owns_client = client is None
        self.telemetry = telemetry or NullWorkerTelemetry()
        self._sem = asyncio.Semaphore(max(1, settings.image_concurrency))
        self._object_store = object_store
        if settings.image_backend == "s3" and self._object_store is None:
            self._object_store = S3ObjectStore(settings)

    async def __aenter__(self) -> "ImageFetcher":
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.s.image_timeout_s,
                follow_redirects=True,
                headers={"User-Agent": self.s.gdelt_user_agent},
            )
        if self.s.image_backend == "local":
            os.makedirs(self.s.image_local_dir, exist_ok=True)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---- public --------------------------------------------------------
    async def attach_images(self, articles: list[Article]) -> int:
        """Fetch+store images for all articles that have one. Sets image_id
        on each Article in place. Returns the number of images stored."""
        if self.s.image_backend == "none" or not self.s.fetch_images:
            return 0
        targets = [a for a in articles if a.has_image]
        if not targets:
            return 0
        results = await asyncio.gather(
            *(self._process_one(a) for a in targets), return_exceptions=True
        )
        stored = 0
        for a, res in zip(targets, results):
            if isinstance(res, Exception):
                log.warning("image failed for %s: %s", a.url, res)
                continue
            if res:
                stored += 1
        return stored

    async def store_url(self, url: str) -> Optional[StoredRef]:
        """Download one image URL, persist it via the configured backend, and
        return a StoredRef (or None on any failure). Reusable by the mapper for
        arbitrary in-body images, not just the GDELT social image."""
        if self.s.image_backend == "none" or not self.s.fetch_images:
            return None
        async with self._sem:
            async with self.telemetry.activity("image", url):
                blob = await self._download(url)
        if blob is None:
            return None
        data, content_type = blob
        image_id = hashlib.sha256(data).hexdigest()

        backend = self.s.image_backend
        storage_ref: Optional[str] = None
        db_bytes: Optional[bytes] = None
        if backend == "db":
            db_bytes = data
        elif backend == "local":
            storage_ref = self._write_local(image_id, data, content_type)
        elif backend == "s3":
            assert self._object_store is not None
            storage_ref = await asyncio.to_thread(
                self._object_store.put_image, image_id, data, content_type
            )

        await self.repo.upsert_image(
            StoredImage(
                id=image_id,
                source_url=url,
                content_type=content_type,
                byte_size=len(data),
                backend=backend,
                storage_ref=storage_ref,
                data=db_bytes,
            )
        )
        return StoredRef(image_id, content_type, len(data), data)

    # ---- internals -----------------------------------------------------
    async def _process_one(self, article: Article) -> bool:
        ref = await self.store_url(article.socialimage_url)  # type: ignore[arg-type]
        if ref is None:
            return False
        article.image_id = ref.image_id  # pair it before the article upsert
        return True

    async def _download(self, url: str) -> Optional[tuple[bytes, Optional[str]]]:
        assert self._client is not None
        try:
            async with self._client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    return None
                ctype = (resp.headers.get("content-type") or "").split(";")[0].strip()
                if ctype and not ctype.startswith(_ALLOWED_PREFIXES):
                    log.debug("skip non-image content-type %s (%s)", ctype, url)
                    return None
                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > self.s.image_max_bytes:
                        log.debug("image too large, skipping %s", url)
                        return None
                if not buf:
                    return None
                return bytes(buf), (ctype or None)
        except httpx.HTTPError as e:
            log.debug("download error %s: %s", url, e)
            return None

    def _write_local(self, image_id: str, data: bytes, ctype: Optional[str]) -> str:
        ext = _ext_for(ctype)
        path = os.path.join(self.s.image_local_dir, f"{image_id}{ext}")
        if not os.path.exists(path):
            with open(path, "wb") as f:
                f.write(data)
        return path

def _ext_for(content_type: Optional[str]) -> str:
    mapping = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/avif": ".avif",
    }
    return mapping.get((content_type or "").lower(), ".img")
