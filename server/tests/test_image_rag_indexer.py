from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest

from app.config import Settings
from app.image_rag import ImageRagService
from app.openrouter import OpenRouterTransientError
from app.vector_store import ImageCursor, VectorWorkItem


def _settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "VECTOR_DATABASE_URL": "postgresql://vector.internal/rag",
        "OPENROUTER_API_KEY": "test-key",
        "IMAGE_RAG_SYNC_LIMIT": 50,
        "IMAGE_RAG_EMBED_BATCH_SIZE": 6,
    }
    values.update(overrides)
    return Settings(**values)


class FakeVector:
    def __init__(self, work: list[VectorWorkItem], *, fail_cursor: bool = False):
        self.work = list(work)
        self.fail_cursor = fail_cursor
        self.cursor = ImageCursor()
        self.enqueued: list[str] = []
        self.indexed: list[str] = []
        self.retried: list[tuple[list[str], str]] = []
        self.dead: list[tuple[list[str], str]] = []
        self.start_calls = 0
        self.closed = 0

    async def start(self):
        self.start_calls += 1
        return True

    async def close(self):
        self.closed += 1

    async def get_cursor(self):
        if self.fail_cursor:
            raise RuntimeError("raw vector credential must not surface")
        return self.cursor

    async def enqueue_discovered(self, rows, cursor):
        self.enqueued.extend(row["id"] for row in rows)
        self.cursor = cursor
        return len(rows)

    async def claim_due(self, limit):
        claimed, self.work = self.work[:limit], self.work[limit:]
        return claimed

    async def mark_indexed(self, image_id, embedding):
        assert len(embedding) == 768
        self.indexed.append(image_id)

    async def mark_retry(self, image_ids, code):
        self.retried.append((list(image_ids), code))

    async def mark_dead(self, image_ids, code):
        self.dead.append((list(image_ids), code))


class FakeEmbeddings:
    def __init__(self, *, error: Exception | None = None):
        self.error = error
        self.calls: list[list[object]] = []
        self.opened = 0
        self.closed = 0

    async def __aenter__(self):
        self.opened += 1
        return self

    async def __aexit__(self, *_exc):
        self.closed += 1

    async def embed_images(self, images):
        self.calls.append(list(images))
        if self.error:
            raise self.error
        return [[float(index)] * 768 for index in range(len(images))]


class FakeRepo:
    def __init__(self, discovered: list[dict], records: dict[str, dict | None]):
        self.discovered = list(discovered)
        self.records = records
        self.discovery_calls: list[tuple] = []

    async def list_images_after(self, *, downloaded_at, image_id, limit):
        self.discovery_calls.append((downloaded_at, image_id, limit))
        return [] if downloaded_at is not None else self.discovered[:limit]

    async def get_image_record(self, image_id):
        return self.records.get(image_id)


def _db_image(data: bytes, content_type: str) -> tuple[str, dict]:
    image_id = hashlib.sha256(data).hexdigest()
    return image_id, {
        "id": image_id,
        "source_url": "https://images.example/item",
        "content_type": content_type,
        "byte_size": len(data),
        "backend": "db",
        "storage_ref": None,
        "data": data,
    }


@pytest.mark.asyncio
async def test_sync_indexes_supported_images_marks_permanent_items_and_is_idempotent():
    jpeg_id, jpeg = _db_image(b"jpeg bytes", "image/jpeg")
    png_id, png = _db_image(b"png bytes", "image/png")
    webp_id, webp = _db_image(b"webp bytes", "image/webp")
    missing_id = "f" * 64
    now = datetime(2026, 7, 19, tzinfo=timezone.utc)
    discovered = [
        {"id": image_id, "content_type": row and row["content_type"],
         "byte_size": row and row["byte_size"], "downloaded_at": now}
        for image_id, row in [
            (jpeg_id, jpeg), (png_id, png), (webp_id, webp), (missing_id, None)
        ]
    ]
    work = [
        VectorWorkItem(item["id"], item.get("content_type"), item.get("byte_size") or 0, 1)
        for item in discovered
    ]
    vector = FakeVector(work)
    embeddings = FakeEmbeddings()
    service = ImageRagService(
        _settings(),
        FakeRepo(discovered, {jpeg_id: jpeg, png_id: png, webp_id: webp}),
        None,
        vector_store=vector,
        embeddings=embeddings,
    )

    await service.start()
    first = await service.sync()
    second = await service.sync()
    await service.close()

    assert (first.discovered, first.claimed, first.indexed, first.dead) == (4, 4, 2, 2)
    assert first.retried == 0 and first.error_code is None
    assert vector.indexed == [jpeg_id, png_id]
    assert ([webp_id], "unsupported_mime") in vector.dead
    assert ([missing_id], "source_missing") in vector.dead
    assert len(embeddings.calls) == 1 and len(embeddings.calls[0]) == 2
    assert (second.discovered, second.claimed, second.indexed) == (0, 0, 0)
    assert embeddings.opened == 1 and embeddings.closed == 1


@pytest.mark.asyncio
async def test_transient_batch_retries_below_limit_and_deads_at_limit():
    one_id, one = _db_image(b"one", "image/jpeg")
    two_id, two = _db_image(b"two", "image/png")
    vector = FakeVector(
        [
            VectorWorkItem(one_id, "image/jpeg", 3, 1),
            VectorWorkItem(two_id, "image/png", 3, 5),
        ]
    )
    service = ImageRagService(
        _settings(IMAGE_RAG_MAX_ATTEMPTS=5),
        FakeRepo([], {one_id: one, two_id: two}),
        None,
        vector_store=vector,
        embeddings=FakeEmbeddings(error=OpenRouterTransientError("provider body")),
    )

    await service.start()
    stats = await service.sync()

    assert (stats.retried, stats.dead, stats.indexed) == (1, 1, 0)
    assert vector.retried == [([one_id], "embedding_transient")]
    assert vector.dead == [([two_id], "embedding_transient")]


@pytest.mark.asyncio
async def test_vector_failure_returns_redacted_fail_open_stats():
    service = ImageRagService(
        _settings(),
        FakeRepo([], {}),
        None,
        vector_store=FakeVector([], fail_cursor=True),
        embeddings=FakeEmbeddings(),
    )

    await service.start()
    stats = await service.sync()

    assert stats.error_code == "sync_failed"
    assert stats.indexed == 0
    assert service.ready is False
