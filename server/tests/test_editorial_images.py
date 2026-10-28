from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from app.api import app, state
from app.config import Settings
from app.editorial_models import EditorPrincipal
from app.editorial_service import EditorialError, editor_token_hash, store_editorial_image
from app.models import StoredImage
from app.object_storage import ObjectStorageError
from app.repository import Repository


NOW = datetime(2026, 7, 19, 13, 0, tzinfo=UTC)
TOKEN = "u" * 43
JPEG = b"\xff\xd8\xff\xe0editorial-jpeg"
PNG = b"\x89PNG\r\n\x1a\neditorial-png"
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8Xanimated-webp"


class MemoryUpload:
    def __init__(self, data: bytes, content_type: str, filename: str = "image.bin"):
        self.data = data
        self.content_type = content_type
        self.filename = filename
        self.offset = 0
        self.reads = 0

    async def read(self, size: int = -1) -> bytes:
        self.reads += 1
        if self.offset >= len(self.data):
            return b""
        end = len(self.data) if size < 0 else min(len(self.data), self.offset + size)
        chunk = self.data[self.offset:end]
        self.offset = end
        return chunk


class ImageRepo:
    def __init__(self, events: list[str] | None = None):
        self.events = events if events is not None else []
        self.images: list[StoredImage] = []
        self.operator_id = uuid4()

    async def authenticate_editor_token(self, token_hash):
        if token_hash != editor_token_hash(TOKEN):
            return None
        return {
            "id": self.operator_id,
            "display_name": "이미지 편집자",
            "role": "editor",
            "created_at": NOW,
            "last_used_at": NOW,
        }

    async def store_editorial_image_metadata(self, *, image, operator_id, filename):
        self.events.append("metadata")
        self.images.append(image)
        assert operator_id == self.operator_id
        assert len(filename) <= 120


class ImageStore:
    def __init__(self, events: list[str], *, fail: bool = False):
        self.events = events
        self.fail = fail
        self.calls: list[tuple[str, bytes, str]] = []

    def put_image(self, image_id: str, data: bytes, content_type: str) -> str:
        self.events.append("put")
        self.calls.append((image_id, data, content_type))
        if self.fail:
            raise ObjectStorageError("put", f"gdelt/{image_id}.img")
        return f"gdelt/{image_id}.img"


def operator(repo: ImageRepo) -> EditorPrincipal:
    return EditorPrincipal(
        id=repo.operator_id,
        display_name="이미지 편집자",
        role="editor",
        created_at=NOW,
        last_used_at=NOW,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content_type", "payload"),
    [("image/jpeg", JPEG), ("image/png", PNG), ("image/webp", WEBP)],
)
async def test_db_accepts_only_supported_magic_and_keeps_bytes(content_type, payload):
    repo = ImageRepo()
    response = await store_editorial_image(
        MemoryUpload(payload, content_type, "현장 사진.webp"),
        operator(repo),
        settings=Settings(_env_file=None, IMAGE_BACKEND="db", IMAGE_MAX_BYTES=1024),
        repo=repo,
    )

    image_id = hashlib.sha256(payload).hexdigest()
    assert response == {
        "image_id": image_id,
        "content_type": content_type,
        "byte_size": len(payload),
        "url": f"/images/{image_id}",
    }
    assert repo.images[0].data == payload
    assert repo.images[0].backend == "db"
    assert repo.images[0].source_url.startswith("editorial-upload://")
    assert " " not in repo.images[0].source_url


@pytest.mark.asyncio
async def test_local_and_s3_write_before_metadata(tmp_path):
    local_events: list[str] = []
    local_repo = ImageRepo(local_events)
    local = await store_editorial_image(
        MemoryUpload(PNG, "image/png", "local.png"),
        operator(local_repo),
        settings=Settings(
            _env_file=None,
            IMAGE_BACKEND="local",
            IMAGE_LOCAL_DIR=str(tmp_path),
            IMAGE_MAX_BYTES=1024,
        ),
        repo=local_repo,
    )
    assert local_events == ["metadata"]
    assert (tmp_path / f"{local['image_id']}.png").read_bytes() == PNG
    assert local_repo.images[0].data is None

    s3_events: list[str] = []
    s3_repo = ImageRepo(s3_events)
    store = ImageStore(s3_events)
    s3 = await store_editorial_image(
        MemoryUpload(WEBP, "image/webp", "animated.webp"),
        operator(s3_repo),
        settings=Settings(_env_file=None, IMAGE_BACKEND="s3", IMAGE_MAX_BYTES=1024),
        repo=s3_repo,
        object_store=store,
    )
    assert s3_events == ["put", "metadata"]
    assert s3_repo.images[0].data is None
    assert s3_repo.images[0].storage_ref == f"gdelt/{s3['image_id']}.img"


@pytest.mark.asyncio
async def test_spoof_empty_unsupported_and_limit_fail_without_metadata():
    repo = ImageRepo()
    cases = [
        (MemoryUpload(JPEG, "image/png"), 422, "editorial_image_signature_mismatch"),
        (MemoryUpload(b"", "image/png"), 422, "empty_editorial_image"),
        (MemoryUpload(PNG, "image/gif"), 415, "unsupported_editorial_image_type"),
        (MemoryUpload(PNG + b"x", "image/png"), 413, "editorial_image_too_large"),
    ]
    for upload, status, code in cases:
        with pytest.raises(EditorialError) as caught:
            await store_editorial_image(
                upload,
                operator(repo),
                settings=Settings(
                    _env_file=None,
                    IMAGE_BACKEND="db",
                    IMAGE_MAX_BYTES=len(PNG),
                ),
                repo=repo,
            )
        assert (caught.value.status_code, caught.value.code) == (status, code)
    assert repo.images == []


@pytest.mark.asyncio
async def test_exact_limit_duplicate_and_s3_failure_contract():
    repo = ImageRepo()
    settings = Settings(
        _env_file=None,
        IMAGE_BACKEND="db",
        IMAGE_MAX_BYTES=len(PNG),
    )
    first = await store_editorial_image(
        MemoryUpload(PNG, "image/png"), operator(repo), settings=settings, repo=repo
    )
    second = await store_editorial_image(
        MemoryUpload(PNG, "image/png"), operator(repo), settings=settings, repo=repo
    )
    assert first["image_id"] == second["image_id"]

    events: list[str] = []
    failing_repo = ImageRepo(events)
    with pytest.raises(EditorialError) as caught:
        await store_editorial_image(
            MemoryUpload(PNG, "image/png"),
            operator(failing_repo),
            settings=Settings(_env_file=None, IMAGE_BACKEND="s3", IMAGE_MAX_BYTES=1024),
            repo=failing_repo,
            object_store=ImageStore(events, fail=True),
        )
    assert caught.value.code == "editorial_image_storage_unavailable"
    assert events == ["put"]
    assert failing_repo.images == []


@pytest.mark.asyncio
async def test_authenticated_multipart_endpoint_and_error_shape():
    repo = ImageRepo()
    state.settings = Settings(_env_file=None, API_TOKEN="master", IMAGE_BACKEND="db")
    state.repo = repo
    state.object_store = None
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        accepted = await client.post(
            "/v1/editor/assets",
            headers={"Authorization": f"Bearer {TOKEN}"},
            files={"file": ("현장.png", PNG, "image/png")},
        )
        spoofed = await client.post(
            "/v1/editor/assets",
            headers={"Authorization": f"Bearer {TOKEN}"},
            files={"file": ("현장.png", JPEG, "image/png")},
        )
    assert accepted.status_code == 201
    assert accepted.json()["image_id"] == hashlib.sha256(PNG).hexdigest()
    assert spoofed.status_code == 422
    assert spoofed.json()["error"]["code"] == "editorial_image_signature_mismatch"


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
async def test_repository_persists_dedup_metadata_and_audit_together():
    repo = Repository(FakeDB())
    image = StoredImage(
        id="a" * 64,
        source_url="editorial-upload://photo.png",
        content_type="image/png",
        byte_size=len(PNG),
        backend="db",
        storage_ref=None,
        data=PNG,
    )
    await repo.store_editorial_image_metadata(
        image=image,
        operator_id=uuid4(),
        filename="photo.png",
    )
    sql = " ".join(repo.pool.query.split())
    assert "INSERT INTO article_images" in sql
    assert "ON CONFLICT (id) DO UPDATE" in sql
    assert "INSERT INTO editorial_audit_log" in sql
    assert "asset.uploaded" in sql
