from __future__ import annotations

import hashlib

import pytest

from app.config import Settings
from app.images import ImageFetcher
from app.models import StoredImage
from app.object_storage import ObjectStorageError
from app.repository import Repository


def _s3_settings() -> Settings:
    return Settings(
        _env_file=None,
        IMAGE_BACKEND="s3",
        FETCH_IMAGES=True,
        S3_BUCKET="article-images",
        S3_ENDPOINT_URL="https://storage.example",
        S3_REGION="sin",
        S3_ACCESS_KEY_ID="access-key",
        S3_SECRET_ACCESS_KEY="secret-key",
    )


class RecordingRepo:
    def __init__(self, events: list[str]):
        self.events = events
        self.images: list[StoredImage] = []

    async def upsert_image(self, image: StoredImage) -> None:
        self.events.append("upsert")
        self.images.append(image)


class RecordingStore:
    def __init__(self, events: list[str], *, fail: bool = False):
        self.events = events
        self.fail = fail
        self.calls: list[tuple[str, bytes, str | None]] = []

    def put_image(self, image_id: str, data: bytes, content_type: str | None) -> str:
        self.events.append("put")
        self.calls.append((image_id, data, content_type))
        if self.fail:
            raise ObjectStorageError("put", f"gdelt/{image_id}.png")
        return f"gdelt/{image_id}.png"


@pytest.mark.asyncio
async def test_s3_upload_precedes_metadata_only_upsert(monkeypatch):
    data = b"downloaded image"
    image_id = hashlib.sha256(data).hexdigest()
    events: list[str] = []
    repo = RecordingRepo(events)
    store = RecordingStore(events)
    fetcher = ImageFetcher(_s3_settings(), repo, object_store=store)

    async def download(_url):
        return data, "image/png"

    monkeypatch.setattr(fetcher, "_download", download)

    ref = await fetcher.store_url("https://origin.example/image.png")

    assert events == ["put", "upsert"]
    assert ref is not None
    assert ref.image_id == image_id
    assert ref.data == data
    assert repo.images == [
        StoredImage(
            id=image_id,
            source_url="https://origin.example/image.png",
            content_type="image/png",
            byte_size=len(data),
            backend="s3",
            storage_ref=f"gdelt/{image_id}.png",
            data=None,
        )
    ]


@pytest.mark.asyncio
async def test_s3_failure_does_not_write_database(monkeypatch):
    events: list[str] = []
    repo = RecordingRepo(events)
    store = RecordingStore(events, fail=True)
    fetcher = ImageFetcher(_s3_settings(), repo, object_store=store)

    async def download(_url):
        return b"image", "image/png"

    monkeypatch.setattr(fetcher, "_download", download)

    with pytest.raises(ObjectStorageError):
        await fetcher.store_url("https://origin.example/image.png")

    assert events == ["put"]
    assert repo.images == []


@pytest.mark.asyncio
async def test_s3_same_image_is_idempotent_and_mapper_keeps_bytes(monkeypatch):
    data = b"same image"
    events: list[str] = []
    repo = RecordingRepo(events)
    store = RecordingStore(events)
    fetcher = ImageFetcher(_s3_settings(), repo, object_store=store)

    async def download(_url):
        return data, "image/png"

    monkeypatch.setattr(fetcher, "_download", download)

    first = await fetcher.store_url("https://origin.example/one.png")
    second = await fetcher.store_url("https://origin.example/two.png")

    assert first is not None and second is not None
    assert first.image_id == second.image_id
    assert first.data == second.data == data
    assert store.calls[0][0] == store.calls[1][0]
    assert all(image.data is None for image in repo.images)


class RecordingPool:
    def __init__(self):
        self.query = ""
        self.args = ()

    async def execute(self, query, *args):
        self.query = query
        self.args = args


class FakeDB:
    def __init__(self):
        self.pool = RecordingPool()


@pytest.mark.asyncio
async def test_repository_s3_conflict_promotes_without_bytea():
    db = FakeDB()
    repo = Repository(db)
    image = StoredImage(
        id="a" * 64,
        source_url="https://origin.example/image.jpg",
        content_type="image/jpeg",
        byte_size=123,
        backend="s3",
        storage_ref=f"gdelt/{'a' * 64}.jpg",
        data=None,
    )

    await repo.upsert_image(image)

    normalized_sql = " ".join(db.pool.query.split())
    assert "ON CONFLICT (id) DO UPDATE SET" in normalized_sql
    assert "backend = 's3'" in normalized_sql
    assert "storage_ref = EXCLUDED.storage_ref" in normalized_sql
    assert "data = NULL" in normalized_sql
    assert "WHERE EXCLUDED.backend = 's3'" in normalized_sql
    assert db.pool.args[-1] is None
