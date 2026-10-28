from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

from app.api import app, state
from app.config import Settings
from app.editorial_service import editor_token_hash
from app.repository import Repository


NOW = datetime(2026, 7, 19, 13, 0, tzinfo=UTC)
TOKEN = "p" * 43
FIXTURE = Path(__file__).with_name("fixtures") / "editorial_document.json"


def publishable_document() -> dict:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["images"] = []
    document["cover_image_ref"] = None
    for scene in document["scenes"]:
        scene["image_ref"] = None
    return document


class PublicationRepo:
    def __init__(self, *, fail_publish: bool = False):
        self.operator_id = uuid4()
        self.draft_id = uuid4()
        self.row = {
            "id": self.draft_id,
            "status": "draft",
            "document": publishable_document(),
            "version": 1,
            "created_by": self.operator_id,
            "updated_by": self.operator_id,
            "created_at": NOW,
            "updated_at": NOW,
        }
        self.publication: dict | None = None
        self.revision_rows: list[dict] = []
        self.map_data: dict | None = None
        self.publish_calls = 0
        self.fail_publish = fail_publish

    async def authenticate_editor_token(self, token_hash):
        if token_hash != editor_token_hash(TOKEN):
            return None
        return {
            "id": self.operator_id,
            "display_name": "발행 편집자",
            "role": "editor",
            "created_at": NOW,
            "last_used_at": NOW,
        }

    async def get_editorial_draft(self, draft_id: UUID):
        return self.row if draft_id == self.draft_id else None

    async def editorial_image_records(self, image_ids: list[str]):
        return []

    async def publish_editorial_draft(
        self, *, draft_id, expected_version, operator_id, document, map_data, image_links
    ):
        self.publish_calls += 1
        if self.fail_publish:
            raise RuntimeError("injected failure")
        if draft_id != self.draft_id:
            return {"kind": "not_found"}
        if self.row["version"] != expected_version:
            return {"kind": "conflict", "current": self.row}
        article_id = hashlib.sha256(f"editorial:{draft_id}".encode()).hexdigest()
        revision_id = uuid4()
        revision = {
            "id": revision_id,
            "draft_id": draft_id,
            "revision_number": len(self.revision_rows) + 1,
            "document": deepcopy(document),
            "map_data": deepcopy(map_data),
            "created_by": operator_id,
            "created_at": NOW,
        }
        self.revision_rows.append(revision)
        self.row = {
            **self.row,
            "status": "published",
            "version": self.row["version"] + 1,
            "document": deepcopy(document),
            "updated_by": operator_id,
            "updated_at": NOW,
        }
        self.publication = {
            "draft_id": draft_id,
            "article_id": article_id,
            "revision_id": revision_id,
            "state": "published",
            "published_at": NOW,
            "published_by": operator_id,
            "withdrawn_at": None,
            "withdrawn_by": None,
            "display_order": None,
            "is_featured": False,
            "updated_at": NOW,
            "draft_version": self.row["version"],
        }
        self.map_data = deepcopy(map_data)
        return {
            "kind": "published",
            "draft": self.row,
            "revision": revision,
            "publication": self.publication,
        }

    async def editorial_revisions(self, draft_id):
        return list(reversed(self.revision_rows)) if draft_id == self.draft_id else []

    async def withdraw_editorial_publication(
        self, *, draft_id, expected_version, operator_id
    ):
        if draft_id != self.draft_id:
            return {"kind": "not_found"}
        if self.publication is None:
            return {"kind": "not_published", "current": self.row}
        if self.publication["state"] == "withdrawn":
            return {"kind": "withdrawn", "publication": self.publication}
        if expected_version is not None and self.row["version"] != expected_version:
            return {"kind": "conflict", "current": self.row}
        self.row = {**self.row, "status": "withdrawn", "version": self.row["version"] + 1}
        self.publication = {
            **self.publication,
            "state": "withdrawn",
            "withdrawn_at": NOW,
            "withdrawn_by": operator_id,
            "display_order": None,
            "is_featured": False,
            "draft_version": self.row["version"],
        }
        return {"kind": "withdrawn", "publication": self.publication}


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


@pytest.mark.asyncio
async def test_first_publish_republish_revision_and_double_click_conflict():
    repo = PublicationRepo()
    state.settings = Settings(_env_file=None, API_TOKEN="master")
    state.repo = repo
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            f"/v1/editor/drafts/{repo.draft_id}/publish",
            headers=auth(),
            json={"expected_version": 1},
        )
        duplicate = await client.post(
            f"/v1/editor/drafts/{repo.draft_id}/publish",
            headers=auth(),
            json={"expected_version": 1},
        )
        repo.row["document"]["headline"] = "수정된 심층 보도"
        repo.row["version"] = 3
        second = await client.post(
            f"/v1/editor/drafts/{repo.draft_id}/publish",
            headers=auth(),
            json={"expected_version": 3},
        )
        revisions = await client.get(
            f"/v1/editor/drafts/{repo.draft_id}/revisions", headers=auth()
        )

    assert first.status_code == 200
    assert first.json()["publication"]["draft_version"] == 2
    assert first.json()["revision"]["revision_number"] == 1
    assert first.json()["map"]["editorial"]["pipeline_version"] == "manual-editor-v1"
    assert duplicate.status_code == 409
    assert second.status_code == 200
    assert second.json()["publication"]["article_id"] == first.json()["publication"]["article_id"]
    assert second.json()["revision"]["revision_number"] == 2
    assert [item["revision_number"] for item in revisions.json()] == [2, 1]


@pytest.mark.asyncio
async def test_invalid_document_and_storage_failure_mutate_nothing():
    invalid_repo = PublicationRepo()
    invalid_repo.row["document"]["headline"] = ""
    state.settings = Settings(_env_file=None, API_TOKEN="master")
    state.repo = invalid_repo
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        invalid = await client.post(
            f"/v1/editor/drafts/{invalid_repo.draft_id}/publish",
            headers=auth(),
            json={"expected_version": 1},
        )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "editorial_document_invalid"
    assert invalid_repo.publish_calls == 0
    assert invalid_repo.revision_rows == []

    failing_repo = PublicationRepo(fail_publish=True)
    state.repo = failing_repo
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        failed = await client.post(
            f"/v1/editor/drafts/{failing_repo.draft_id}/publish",
            headers=auth(),
            json={"expected_version": 1},
        )
    assert failed.status_code == 503
    assert failed.json()["error"]["code"] == "editorial_publish_unavailable"
    assert failing_repo.row["version"] == 1
    assert failing_repo.revision_rows == []


@pytest.mark.asyncio
async def test_soft_withdraw_is_idempotent_and_retains_revision_and_map():
    repo = PublicationRepo()
    state.settings = Settings(_env_file=None, API_TOKEN="master")
    state.repo = repo
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            f"/v1/editor/drafts/{repo.draft_id}/publish",
            headers=auth(),
            json={"expected_version": 1},
        )
        withdrawn = await client.post(
            f"/v1/editor/drafts/{repo.draft_id}/withdraw",
            headers=auth(),
            json={"expected_version": 2},
        )
        repeated = await client.post(
            f"/v1/editor/drafts/{repo.draft_id}/withdraw",
            headers=auth(),
            json={"expected_version": 2},
        )

    assert withdrawn.status_code == repeated.status_code == 200
    assert withdrawn.json()["state"] == repeated.json()["state"] == "withdrawn"
    assert repo.row["version"] == 3
    assert len(repo.revision_rows) == 1
    assert repo.map_data is not None


class FakeTransaction:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        if exc_type is None:
            self.connection.committed.extend(self.connection.pending)
        else:
            self.connection.rolled_back = True
        self.connection.pending.clear()
        return False


class FakeConnection:
    def __init__(
        self,
        draft_id: UUID,
        operator_id: UUID,
        *,
        fail_on_map: bool = False,
        fail_on_outbox: bool = False,
        schedule_id: UUID | None = None,
    ):
        self.draft_id = draft_id
        self.operator_id = operator_id
        self.fail_on_map = fail_on_map
        self.fail_on_outbox = fail_on_outbox
        self.schedule_id = schedule_id
        self.pending: list[str] = []
        self.committed: list[str] = []
        self.rolled_back = False

    def transaction(self):
        return FakeTransaction(self)

    async def fetchval(self, query, *args):
        normalized = " ".join(query.split())
        if "INSERT INTO editorial_push_deliveries" in normalized:
            self.pending.append("INSERT INTO editorial_push_deliveries")
            if self.fail_on_outbox:
                raise RuntimeError("outbox write failure")
            return 2
        self.pending.append("revision_number")
        return 1

    async def execute(self, query, *args):
        normalized = " ".join(query.split())
        if "INSERT INTO article_maps" in normalized and self.fail_on_map:
            raise RuntimeError("map write failure")
        for marker in (
            "INSERT INTO articles", "DELETE FROM article_image_links",
            "INSERT INTO article_maps", "INSERT INTO map_queue",
            "INSERT INTO editorial_audit_log",
        ):
            if marker in normalized:
                self.pending.append(marker)
                break
        return "OK"

    async def executemany(self, query, args):
        self.pending.append("INSERT INTO article_image_links")

    async def fetchrow(self, query, *args):
        normalized = " ".join(query.split())
        if normalized.startswith("SELECT * FROM editorial_drafts"):
            return {
                "id": self.draft_id, "status": "draft", "document": publishable_document(),
                "version": 1, "created_by": self.operator_id, "updated_by": self.operator_id,
                "created_at": NOW, "updated_at": NOW,
            }
        if normalized.startswith("SELECT * FROM editorial_schedules"):
            return {
                "id": self.schedule_id,
                "draft_id": self.draft_id,
                "state": "processing",
                "version": 1,
                "updated_by": self.operator_id,
                "document": publishable_document(),
                "map_data": {},
                "image_links": [],
            }
        if "INSERT INTO editorial_revisions" in normalized:
            self.pending.append("INSERT INTO editorial_revisions")
            return {
                "id": args[0], "draft_id": self.draft_id, "revision_number": 1,
                "document": args[3], "map_data": args[4],
                "created_by": self.operator_id, "created_at": NOW,
            }
        if "INSERT INTO editorial_publications" in normalized:
            self.pending.append("INSERT INTO editorial_publications")
            return {
                "draft_id": self.draft_id, "article_id": args[1], "revision_id": args[2],
                "state": "published", "published_at": NOW, "published_by": self.operator_id,
                "withdrawn_at": None, "withdrawn_by": None, "display_order": None,
                "is_featured": False, "updated_at": NOW,
            }
        if "UPDATE editorial_drafts" in normalized:
            self.pending.append("UPDATE editorial_drafts")
            return {
                "id": self.draft_id, "status": "published", "document": publishable_document(),
                "version": 2, "created_by": self.operator_id, "updated_by": self.operator_id,
                "created_at": NOW, "updated_at": NOW,
            }
        if "UPDATE editorial_schedules" in normalized:
            self.pending.append("UPDATE editorial_schedules")
            return {
                "id": self.schedule_id,
                "draft_id": self.draft_id,
                "state": "published",
                "version": 2,
                "updated_by": self.operator_id,
                "document": publishable_document(),
                "map_data": {},
                "image_links": [],
                "publication_revision_id": args[1],
            }
        if "INSERT INTO editorial_push_notifications" in normalized:
            self.pending.append("INSERT INTO editorial_push_notifications")
            return {"id": args[0]}
        raise AssertionError(normalized)


class FakeAcquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakePool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return FakeAcquire(self.connection)


class FakeDB:
    def __init__(self, pool):
        self.pool = pool


@pytest.mark.asyncio
async def test_repository_publish_is_one_transaction_and_rolls_back_injected_failure():
    draft_id, operator_id = uuid4(), uuid4()
    document = publishable_document()
    from app.editorial_compiler import compile_editorial_document

    map_data = compile_editorial_document(document, [])
    connection = FakeConnection(draft_id, operator_id)
    repo = Repository(FakeDB(FakePool(connection)))
    result = await repo.publish_editorial_draft(
        draft_id=draft_id,
        expected_version=1,
        operator_id=operator_id,
        document=document,
        map_data=map_data,
        image_links=[],
    )
    assert result["kind"] == "published"
    assert connection.rolled_back is False
    assert {
        "INSERT INTO articles", "INSERT INTO article_maps", "INSERT INTO map_queue",
        "INSERT INTO editorial_revisions", "INSERT INTO editorial_publications",
        "UPDATE editorial_drafts", "INSERT INTO editorial_audit_log",
        "INSERT INTO editorial_push_notifications",
        "INSERT INTO editorial_push_deliveries",
    } <= set(connection.committed)

    failing_connection = FakeConnection(draft_id, operator_id, fail_on_map=True)
    failing_repo = Repository(FakeDB(FakePool(failing_connection)))
    with pytest.raises(RuntimeError, match="map write failure"):
        await failing_repo.publish_editorial_draft(
            draft_id=draft_id,
            expected_version=1,
            operator_id=operator_id,
            document=document,
            map_data=map_data,
            image_links=[],
        )
    assert failing_connection.rolled_back is True
    assert failing_connection.committed == []

    outbox_failure = FakeConnection(draft_id, operator_id, fail_on_outbox=True)
    outbox_failure_repo = Repository(FakeDB(FakePool(outbox_failure)))
    with pytest.raises(RuntimeError, match="outbox write failure"):
        await outbox_failure_repo.publish_editorial_draft(
            draft_id=draft_id,
            expected_version=1,
            operator_id=operator_id,
            document=document,
            map_data=map_data,
            image_links=[],
        )
    assert outbox_failure.rolled_back is True
    assert outbox_failure.committed == []


@pytest.mark.asyncio
async def test_scheduled_first_publish_uses_the_same_transactional_outbox_boundary():
    draft_id, operator_id, schedule_id = uuid4(), uuid4(), uuid4()
    connection = FakeConnection(
        draft_id,
        operator_id,
        schedule_id=schedule_id,
    )
    repository = Repository(FakeDB(FakePool(connection)))

    result = await repository.publish_editorial_draft(
        draft_id=draft_id,
        expected_version=None,
        operator_id=None,
        document=None,
        map_data=None,
        image_links=None,
        schedule_id=schedule_id,
    )

    assert result["kind"] == "published"
    assert result["schedule"]["state"] == "published"
    assert {
        "UPDATE editorial_schedules",
        "INSERT INTO editorial_push_notifications",
        "INSERT INTO editorial_push_deliveries",
    } <= set(connection.committed)
