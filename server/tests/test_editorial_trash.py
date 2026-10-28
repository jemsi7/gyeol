from __future__ import annotations

import inspect
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest

from app.api import app, state
from app.config import Settings
from app.db import SCHEMA_SQL
from app.db_bootstrap import REQUIRED_INDEXES
from app.editorial_service import editor_token_hash
from app.repository import Repository


TOKEN = "t" * 43
NOW = datetime(2026, 7, 20, 5, 0, tzinfo=UTC)


def document(headline: str = "휴지통 검증 초안") -> dict:
    return {
        "format_version": 3,
        "source_name": "결 편집부",
        "language": "ko",
        "headline": headline,
        "subheadline": "",
        "summary": "",
        "keywords": [],
        "images": [],
        "cover_image_ref": None,
        "visualizations": [],
        "scenes": [],
    }


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


class TrashRepo:
    def __init__(self) -> None:
        self.operator_id = uuid4()
        self.draft_id = uuid4()
        self.row = {
            "id": self.draft_id,
            "status": "draft",
            "document": document(),
            "version": 1,
            "created_by": self.operator_id,
            "updated_by": self.operator_id,
            "deleted_at": None,
            "deleted_by": None,
            "created_at": NOW,
            "updated_at": NOW,
        }
        self.schedule: dict | None = {
            "id": uuid4(),
            "draft_id": self.draft_id,
            "version": 1,
            "state": "scheduled",
            "scheduled_for": NOW + timedelta(hours=1),
            "snapshot_draft_version": 1,
            "created_by": self.operator_id,
            "updated_by": self.operator_id,
            "attempt_count": 0,
            "last_error": None,
            "created_at": NOW,
            "updated_at": NOW,
            "cancelled_at": None,
            "published_at": None,
        }
        self.has_history = False
        self.transition_calls = 0

    async def authenticate_editor_token(self, token_hash):
        if token_hash != editor_token_hash(TOKEN):
            return None
        return {
            "id": self.operator_id,
            "display_name": "휴지통 편집자",
            "role": "editor",
            "created_at": NOW,
            "last_used_at": NOW,
        }

    async def editorial_drafts(self, limit: int = 100):
        if self.row["deleted_at"] is not None:
            return []
        return [deepcopy(self.row)][:limit]

    async def editorial_trashed_drafts(self, limit: int = 100):
        if self.row["deleted_at"] is None:
            return []
        return [deepcopy(self.row)][:limit]

    async def get_editorial_draft(self, draft_id: UUID):
        if draft_id != self.draft_id or self.row["deleted_at"] is not None:
            return None
        return deepcopy(self.row)

    async def update_editorial_draft(
        self, *, draft_id, expected_version, document, operator_id
    ):
        if (
            draft_id != self.draft_id
            or self.row["deleted_at"] is not None
            or self.row["version"] != expected_version
        ):
            return None
        self.row.update(
            document=deepcopy(document),
            version=expected_version + 1,
            updated_by=operator_id,
            updated_at=NOW,
        )
        return deepcopy(self.row)

    async def editorial_image_records(self, image_ids: list[str]):
        return []

    async def editorial_schedules(self, limit: int = 100):
        if self.row["deleted_at"] is not None or self.schedule is None:
            return []
        return [deepcopy(self.schedule)][:limit]

    async def editorial_schedule(self, draft_id: UUID):
        if (
            draft_id != self.draft_id
            or self.row["deleted_at"] is not None
            or self.schedule is None
        ):
            return None
        return deepcopy(self.schedule)

    async def upsert_editorial_schedule(self, **kwargs):
        if self.row["deleted_at"] is not None:
            return {"kind": "not_found"}
        raise AssertionError("active schedule write is outside this test")

    async def cancel_editorial_schedule(
        self, *, draft_id, expected_schedule_version, operator_id
    ):
        if draft_id != self.draft_id or self.row["deleted_at"] is not None:
            return {"kind": "not_found"}
        raise AssertionError("active schedule cancel is outside this test")

    async def publish_editorial_draft(self, **kwargs):
        if self.row["deleted_at"] is not None:
            return {"kind": "not_found"}
        raise AssertionError("active publish is outside this test")

    async def editorial_revisions(self, draft_id: UUID):
        return []

    async def trash_editorial_draft(
        self, *, draft_id, expected_version, operator_id
    ):
        self.transition_calls += 1
        if draft_id != self.draft_id:
            return {"kind": "not_found"}
        current = deepcopy(self.row)
        if self.row["version"] != expected_version:
            return {"kind": "conflict", "current": current}
        if (
            self.row["deleted_at"] is not None
            or self.row["status"] != "draft"
            or self.has_history
        ):
            return {"kind": "not_eligible", "current": current}
        if self.schedule is not None and self.schedule["state"] == "processing":
            return {"kind": "schedule_processing", "current": current}
        if self.schedule is not None and self.schedule["state"] == "scheduled":
            self.schedule.update(
                state="cancelled",
                version=self.schedule["version"] + 1,
                updated_by=operator_id,
                updated_at=NOW,
                cancelled_at=NOW,
            )
        self.row.update(
            version=self.row["version"] + 1,
            updated_by=operator_id,
            updated_at=NOW,
            deleted_at=NOW,
            deleted_by=operator_id,
        )
        return {"kind": "trashed", "draft": deepcopy(self.row)}

    async def restore_editorial_draft(
        self, *, draft_id, expected_version, operator_id
    ):
        self.transition_calls += 1
        if draft_id != self.draft_id:
            return {"kind": "not_found"}
        current = deepcopy(self.row)
        if self.row["version"] != expected_version:
            return {"kind": "conflict", "current": current}
        if self.row["deleted_at"] is None:
            return {"kind": "not_trashed", "current": current}
        self.row.update(
            version=self.row["version"] + 1,
            updated_by=operator_id,
            updated_at=NOW,
            deleted_at=None,
            deleted_by=None,
        )
        return {"kind": "restored", "draft": deepcopy(self.row)}


@pytest.fixture
def trash_state():
    repo = TrashRepo()
    state.settings = Settings(_env_file=None, API_TOKEN="master")
    state.repo = repo
    return repo


@pytest.mark.asyncio
async def test_trash_hides_active_draft_cancels_schedule_and_restore_preserves_document(
    trash_state,
):
    original_document = deepcopy(trash_state.row["document"])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        before = await client.get("/v1/editor/drafts", headers=auth())
        trashed = await client.post(
            f"/v1/editor/drafts/{trash_state.draft_id}/trash",
            headers=auth(),
            json={"expected_version": 1},
        )
        active = await client.get("/v1/editor/drafts", headers=auth())
        trash = await client.get("/v1/editor/trash", headers=auth())
        hidden = await client.get(
            f"/v1/editor/drafts/{trash_state.draft_id}", headers=auth()
        )
        restored = await client.post(
            f"/v1/editor/drafts/{trash_state.draft_id}/restore",
            headers=auth(),
            json={"expected_version": 2},
        )
        after = await client.get("/v1/editor/drafts", headers=auth())

    assert before.status_code == 200 and len(before.json()) == 1
    assert trashed.status_code == 200
    assert trashed.json()["version"] == 2
    assert trashed.json()["deleted_at"] is not None
    assert trashed.json()["deleted_by"] == str(trash_state.operator_id)
    assert active.json() == []
    assert [item["id"] for item in trash.json()] == [str(trash_state.draft_id)]
    assert hidden.status_code == 404
    assert trash_state.schedule is not None
    assert trash_state.schedule["state"] == "cancelled"
    assert trash_state.schedule["version"] == 2
    assert restored.status_code == 200
    assert restored.json()["version"] == 3
    assert restored.json()["deleted_at"] is None
    assert restored.json()["document"] == {
        **original_document,
        "publication_kind": "editorial",
        # DR-13254 F-3 fields: empty means "ordinary article", so a draft that
        # never set them round-trips with the defaults.
        "document_type": "",
        "document_source_name": "",
    }
    assert len(after.json()) == 1
    assert trash_state.schedule["state"] == "cancelled"


@pytest.mark.asyncio
async def test_trash_rejects_stale_history_processing_and_invalid_body_without_mutation(
    trash_state,
):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        stale = await client.post(
            f"/v1/editor/drafts/{trash_state.draft_id}/trash",
            headers=auth(),
            json={"expected_version": 9},
        )
        trash_state.has_history = True
        history = await client.post(
            f"/v1/editor/drafts/{trash_state.draft_id}/trash",
            headers=auth(),
            json={"expected_version": 1},
        )
        trash_state.has_history = False
        assert trash_state.schedule is not None
        trash_state.schedule["state"] = "processing"
        processing = await client.post(
            f"/v1/editor/drafts/{trash_state.draft_id}/trash",
            headers=auth(),
            json={"expected_version": 1},
        )
        before_invalid = trash_state.transition_calls
        invalid = await client.post(
            f"/v1/editor/drafts/{trash_state.draft_id}/trash",
            headers=auth(),
            json={},
        )

    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "draft_version_conflict"
    assert stale.json()["error"]["detail"]["current"]["version"] == 1
    assert history.status_code == 409
    assert history.json()["error"]["code"] == "editorial_draft_not_trashable"
    assert processing.status_code == 409
    assert processing.json()["error"]["code"] == "editorial_schedule_not_mutable"
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "invalid_request"
    assert trash_state.transition_calls == before_invalid
    assert trash_state.row["deleted_at"] is None
    assert trash_state.row["version"] == 1


@pytest.mark.asyncio
async def test_trashed_draft_is_404_on_every_active_read_and_mutation_route(
    trash_state,
):
    trash_state.row.update(
        version=2,
        deleted_at=NOW,
        deleted_by=trash_state.operator_id,
    )
    assert trash_state.schedule is not None
    trash_state.schedule["state"] = "cancelled"
    draft_id = trash_state.draft_id
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        responses = [
            await client.get(f"/v1/editor/drafts/{draft_id}", headers=auth()),
            await client.put(
                f"/v1/editor/drafts/{draft_id}",
                headers=auth(),
                json={"expected_version": 2, "document": document("수정")},
            ),
            await client.post(f"/v1/editor/drafts/{draft_id}/validate", headers=auth()),
            await client.get(f"/v1/editor/drafts/{draft_id}/schedule", headers=auth()),
            await client.put(
                f"/v1/editor/drafts/{draft_id}/schedule",
                headers=auth(),
                json={
                    "expected_draft_version": 2,
                    "scheduled_for": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                },
            ),
            await client.post(
                f"/v1/editor/drafts/{draft_id}/schedule/cancel",
                headers=auth(),
                json={"expected_schedule_version": 2},
            ),
            await client.post(
                f"/v1/editor/drafts/{draft_id}/publish",
                headers=auth(),
                json={"expected_version": 2},
            ),
            await client.get(f"/v1/editor/drafts/{draft_id}/revisions", headers=auth()),
        ]

    assert all(response.status_code == 404 for response in responses)
    assert all(
        response.json()["error"]["code"]
        in {"editorial_draft_not_found", "editorial_schedule_not_found"}
        for response in responses
    )


@pytest.mark.asyncio
async def test_trash_routes_require_editor_auth_and_have_no_delete_or_purge_route(
    trash_state,
):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        unauthorized = await client.post(
            f"/v1/editor/drafts/{trash_state.draft_id}/trash",
            json={"expected_version": 1},
        )
        delete = await client.delete(
            f"/v1/editor/drafts/{trash_state.draft_id}", headers=auth()
        )
        purge = await client.post("/v1/editor/trash/purge", headers=auth(), json={})

    assert unauthorized.status_code == 401
    assert trash_state.transition_calls == 0
    assert delete.status_code == 405
    assert purge.status_code in {404, 405, 422}
    openapi = app.openapi()["paths"]
    assert "delete" not in openapi["/v1/editor/drafts/{draft_id}"]
    assert not any("purge" in path for path in openapi)


class FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class TrashConnection:
    def __init__(self, *, schedule_state: str = "scheduled", has_history: bool = False):
        self.operator_id = uuid4()
        self.draft_id = uuid4()
        self.draft = {
            "id": self.draft_id,
            "status": "draft",
            "document": document(),
            "version": 1,
            "created_by": self.operator_id,
            "updated_by": self.operator_id,
            "deleted_at": None,
            "deleted_by": None,
            "created_at": NOW,
            "updated_at": NOW,
        }
        self.schedule = {
            "id": uuid4(),
            "draft_id": self.draft_id,
            "version": 1,
            "state": schedule_state,
            "scheduled_for": NOW + timedelta(hours=1),
            "snapshot_draft_version": 1,
            "document": document(),
            "map_data": {},
            "image_links": [],
            "created_by": self.operator_id,
            "updated_by": self.operator_id,
            "attempt_count": 0,
            "next_attempt_at": NOW + timedelta(hours=1),
            "lease_expires_at": None,
            "last_error": None,
            "publication_revision_id": None,
            "created_at": NOW,
            "updated_at": NOW,
            "cancelled_at": None,
            "published_at": None,
        }
        self.has_history = has_history
        self.queries: list[str] = []
        self.audit_actions: list[str] = []

    def transaction(self):
        return FakeTransaction()

    async def fetchrow(self, query, *args):
        normalized = " ".join(query.split())
        self.queries.append(normalized)
        if normalized.startswith("SELECT * FROM editorial_drafts"):
            return deepcopy(self.draft) if args[0] == self.draft_id else None
        if normalized.startswith("SELECT * FROM editorial_schedules"):
            if self.schedule["state"] in {"scheduled", "processing"}:
                return deepcopy(self.schedule)
            return None
        if normalized.startswith("UPDATE editorial_schedules"):
            assert self.schedule["state"] == "scheduled"
            self.schedule.update(
                state="cancelled",
                version=self.schedule["version"] + 1,
                updated_by=args[1],
                updated_at=NOW,
                cancelled_at=NOW,
            )
            return deepcopy(self.schedule)
        if "SET deleted_at=now()" in normalized:
            self.draft.update(
                deleted_at=NOW,
                deleted_by=args[2],
                version=self.draft["version"] + 1,
                updated_by=args[2],
                updated_at=NOW,
            )
            return deepcopy(self.draft)
        if "SET deleted_at=NULL" in normalized:
            self.draft.update(
                deleted_at=None,
                deleted_by=None,
                version=self.draft["version"] + 1,
                updated_by=args[2],
                updated_at=NOW,
            )
            return deepcopy(self.draft)
        raise AssertionError(f"unexpected fetchrow: {normalized}")

    async def fetchval(self, query, *args):
        self.queries.append(" ".join(query.split()))
        return self.has_history

    async def execute(self, query, *args):
        normalized = " ".join(query.split())
        self.queries.append(normalized)
        if "'draft.trashed'" in normalized:
            self.audit_actions.append("draft.trashed")
        elif "'draft.restored'" in normalized:
            self.audit_actions.append("draft.restored")
        else:
            raise AssertionError(f"unexpected execute: {normalized}")
        return "INSERT 0 1"


class FakeAcquire:
    def __init__(self, connection: TrashConnection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class TrashPool:
    def __init__(self, connection: TrashConnection):
        self.connection = connection

    def acquire(self):
        return FakeAcquire(self.connection)


class TrashDB:
    def __init__(self, connection: TrashConnection):
        self.pool = TrashPool(connection)


@pytest.mark.asyncio
async def test_repository_trash_cancels_scheduled_in_same_draft_then_schedule_lock_transaction():
    connection = TrashConnection()
    repo = Repository(TrashDB(connection))

    trashed = await repo.trash_editorial_draft(
        draft_id=connection.draft_id,
        expected_version=1,
        operator_id=connection.operator_id,
    )
    restored = await repo.restore_editorial_draft(
        draft_id=connection.draft_id,
        expected_version=2,
        operator_id=connection.operator_id,
    )

    assert trashed["kind"] == "trashed"
    assert trashed["draft"]["version"] == 2
    assert trashed["draft"]["deleted_at"] == NOW
    assert trashed["schedule"]["state"] == "cancelled"
    assert trashed["schedule"]["version"] == 2
    assert restored["kind"] == "restored"
    assert restored["draft"]["version"] == 3
    assert restored["draft"]["document"] == document()
    assert connection.schedule["state"] == "cancelled"
    assert connection.audit_actions == ["draft.trashed", "draft.restored"]
    draft_lock = next(
        index
        for index, query in enumerate(connection.queries)
        if query.startswith("SELECT * FROM editorial_drafts")
    )
    schedule_lock = next(
        index
        for index, query in enumerate(connection.queries)
        if query.startswith("SELECT * FROM editorial_schedules")
    )
    assert draft_lock < schedule_lock


@pytest.mark.asyncio
async def test_repository_restore_clears_only_delete_metadata_and_keeps_cancelled_schedule():
    connection = TrashConnection(schedule_state="cancelled")
    connection.draft.update(
        version=2,
        deleted_at=NOW,
        deleted_by=connection.operator_id,
    )
    original_document = deepcopy(connection.draft["document"])
    original_created_at = connection.draft["created_at"]
    repo = Repository(TrashDB(connection))

    result = await repo.restore_editorial_draft(
        draft_id=connection.draft_id,
        expected_version=2,
        operator_id=connection.operator_id,
    )

    assert result["kind"] == "restored"
    assert result["draft"]["version"] == 3
    assert result["draft"]["deleted_at"] is None
    assert result["draft"]["deleted_by"] is None
    assert result["draft"]["document"] == original_document
    assert result["draft"]["created_at"] == original_created_at
    assert connection.schedule["state"] == "cancelled"
    assert not any(
        query.startswith("UPDATE editorial_schedules") for query in connection.queries
    )
    assert connection.audit_actions == ["draft.restored"]


@pytest.mark.asyncio
async def test_repository_trash_processing_or_history_mutates_nothing():
    processing = TrashConnection(schedule_state="processing")
    processing_repo = Repository(TrashDB(processing))
    processing_result = await processing_repo.trash_editorial_draft(
        draft_id=processing.draft_id,
        expected_version=1,
        operator_id=processing.operator_id,
    )

    history = TrashConnection(has_history=True)
    history_repo = Repository(TrashDB(history))
    history_result = await history_repo.trash_editorial_draft(
        draft_id=history.draft_id,
        expected_version=1,
        operator_id=history.operator_id,
    )

    assert processing_result["kind"] == "schedule_processing"
    assert history_result["kind"] == "not_eligible"
    for connection in (processing, history):
        assert connection.draft["deleted_at"] is None
        assert connection.draft["version"] == 1
        assert connection.audit_actions == []
    assert processing.schedule["state"] == "processing"
    assert history.schedule["state"] == "scheduled"


def test_trash_schema_is_additive_idempotent_and_bootstrap_requires_partial_index():
    for expected in (
        "deleted_at  TIMESTAMPTZ",
        "deleted_by  UUID REFERENCES editor_operators(id)",
        "ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ",
        "ADD COLUMN IF NOT EXISTS deleted_by UUID REFERENCES editor_operators(id)",
        "idx_editorial_drafts_deleted",
        "WHERE deleted_at IS NOT NULL",
    ):
        assert expected in SCHEMA_SQL
    assert "idx_editorial_drafts_deleted" in REQUIRED_INDEXES

    source = inspect.getsource(Repository)
    assert "DELETE FROM editorial_drafts" not in source
    assert "WHERE deleted_at IS NULL" in source
    assert "WHERE deleted_at IS NOT NULL" in source
