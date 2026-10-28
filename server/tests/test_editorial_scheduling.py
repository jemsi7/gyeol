from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

from app.api import app, state
from app.config import Settings
from app.db import SCHEMA_SQL
from app.editorial_models import EditorialScheduleUpsertRequest
from app.editorial_service import editor_token_hash
from app.repository import Repository


TOKEN = "s" * 43
NOW = datetime(2026, 7, 19, 13, 0, tzinfo=UTC)
FIXTURE = Path(__file__).with_name("fixtures") / "editorial_document.json"


def publishable_document(headline: str = "예약 기사") -> dict:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["headline"] = headline
    document["images"] = []
    document["cover_image_ref"] = None
    for scene in document["scenes"]:
        scene["image_ref"] = None
    return document


class ScheduleRepo:
    def __init__(self):
        self.operator_id = uuid4()
        self.draft_id = uuid4()
        self.draft = {
            "id": self.draft_id,
            "status": "draft",
            "document": publishable_document(),
            "version": 1,
            "created_by": self.operator_id,
            "updated_by": self.operator_id,
            "created_at": NOW,
            "updated_at": NOW,
        }
        self.schedule: dict | None = None
        self.schedule_writes = 0

    async def authenticate_editor_token(self, token_hash):
        if token_hash != editor_token_hash(TOKEN):
            return None
        return {
            "id": self.operator_id,
            "display_name": "예약 편집자",
            "role": "editor",
            "created_at": NOW,
            "last_used_at": NOW,
        }

    async def get_editorial_draft(self, draft_id: UUID):
        return self.draft if draft_id == self.draft_id else None

    async def editorial_image_records(self, image_ids: list[str]):
        return []

    async def editorial_schedules(self, limit: int = 100):
        return [self.schedule] if self.schedule else []

    async def editorial_schedule(self, draft_id: UUID):
        return self.schedule if draft_id == self.draft_id else None

    async def upsert_editorial_schedule(
        self,
        *,
        draft_id,
        expected_draft_version,
        expected_schedule_version,
        scheduled_for,
        operator_id,
        document,
        map_data,
        image_links,
    ):
        if draft_id != self.draft_id:
            return {"kind": "not_found"}
        if expected_draft_version != self.draft["version"]:
            return {"kind": "draft_conflict", "current": self.draft}
        if self.schedule and self.schedule["state"] in {"scheduled", "processing"}:
            if self.schedule["state"] != "scheduled":
                return {"kind": "schedule_not_mutable", "current": self.schedule}
            if expected_schedule_version != self.schedule["version"]:
                return {"kind": "schedule_conflict", "current": self.schedule}
            schedule_id = self.schedule["id"]
            version = self.schedule["version"] + 1
            created_at = self.schedule["created_at"]
        else:
            if expected_schedule_version is not None:
                return {"kind": "schedule_conflict", "current": self.schedule}
            schedule_id = uuid4()
            version = 1
            created_at = NOW
        self.schedule_writes += 1
        self.schedule = {
            "id": schedule_id,
            "draft_id": draft_id,
            "version": version,
            "state": "scheduled",
            "scheduled_for": scheduled_for,
            "snapshot_draft_version": expected_draft_version,
            "document": deepcopy(document),
            "map_data": deepcopy(map_data),
            "image_links": deepcopy(image_links),
            "created_by": operator_id,
            "updated_by": operator_id,
            "attempt_count": 0,
            "last_error": None,
            "created_at": created_at,
            "updated_at": NOW,
            "cancelled_at": None,
            "published_at": None,
        }
        return {"kind": "scheduled", "schedule": self.schedule}

    async def cancel_editorial_schedule(
        self, *, draft_id, expected_schedule_version, operator_id
    ):
        if draft_id != self.draft_id:
            return {"kind": "not_found"}
        if not self.schedule or self.schedule["state"] != "scheduled":
            return {"kind": "schedule_not_active", "current": self.schedule}
        if self.schedule["version"] != expected_schedule_version:
            return {"kind": "schedule_conflict", "current": self.schedule}
        self.schedule = {
            **self.schedule,
            "version": self.schedule["version"] + 1,
            "state": "cancelled",
            "updated_by": operator_id,
            "updated_at": NOW,
            "cancelled_at": NOW,
        }
        return {"kind": "cancelled", "schedule": self.schedule}


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def schedule_state():
    repo = ScheduleRepo()
    state.settings = Settings(_env_file=None, API_TOKEN="master")
    state.repo = repo
    return repo


@pytest.mark.asyncio
async def test_reserve_freezes_valid_snapshot_and_response_never_exposes_payload(
    schedule_state,
):
    scheduled_for = datetime.now(UTC) + timedelta(minutes=5)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        reserved = await client.put(
            f"/v1/editor/drafts/{schedule_state.draft_id}/schedule",
            headers=auth(),
            json={
                "expected_draft_version": 1,
                "expected_schedule_version": None,
                "scheduled_for": scheduled_for.isoformat(),
            },
        )
        listed = await client.get("/v1/editor/schedules", headers=auth())

    assert reserved.status_code == 200
    assert reserved.json()["state"] == "scheduled"
    assert reserved.json()["snapshot_draft_version"] == 1
    for private_key in ("document", "map_data", "image_links", "lease_expires_at"):
        assert private_key not in reserved.json()
        assert private_key not in listed.json()[0]

    frozen = deepcopy(schedule_state.schedule["document"])
    schedule_state.draft["document"]["headline"] = "예약 뒤 수정본"
    schedule_state.draft["version"] = 2
    assert schedule_state.schedule["document"] == frozen
    assert frozen["headline"] == "예약 기사"


@pytest.mark.asyncio
async def test_reschedule_uses_schedule_cas_and_cancel_is_terminal(schedule_state):
    transport = httpx.ASGITransport(app=app)
    first_time = datetime.now(UTC) + timedelta(minutes=5)
    second_time = datetime.now(UTC) + timedelta(minutes=10)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.put(
            f"/v1/editor/drafts/{schedule_state.draft_id}/schedule",
            headers=auth(),
            json={
                "expected_draft_version": 1,
                "expected_schedule_version": None,
                "scheduled_for": first_time.isoformat(),
            },
        )
        stale = await client.put(
            f"/v1/editor/drafts/{schedule_state.draft_id}/schedule",
            headers=auth(),
            json={
                "expected_draft_version": 1,
                "expected_schedule_version": 9,
                "scheduled_for": second_time.isoformat(),
            },
        )
        changed = await client.put(
            f"/v1/editor/drafts/{schedule_state.draft_id}/schedule",
            headers=auth(),
            json={
                "expected_draft_version": 1,
                "expected_schedule_version": first.json()["version"],
                "scheduled_for": second_time.isoformat(),
            },
        )
        cancelled = await client.post(
            f"/v1/editor/drafts/{schedule_state.draft_id}/schedule/cancel",
            headers=auth(),
            json={"expected_schedule_version": changed.json()["version"]},
        )

    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "editorial_schedule_conflict"
    detail = stale.json()["error"]["detail"]["current_schedule"]
    assert "document" not in detail and detail["version"] == 1
    assert changed.status_code == 200 and changed.json()["version"] == 2
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"
    assert cancelled.json()["version"] == 3


@pytest.mark.asyncio
async def test_invalid_or_too_soon_schedule_mutates_nothing(schedule_state):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        too_soon = await client.put(
            f"/v1/editor/drafts/{schedule_state.draft_id}/schedule",
            headers=auth(),
            json={
                "expected_draft_version": 1,
                "scheduled_for": datetime.now(UTC).isoformat(),
            },
        )
        schedule_state.draft["document"]["headline"] = ""
        invalid = await client.put(
            f"/v1/editor/drafts/{schedule_state.draft_id}/schedule",
            headers=auth(),
            json={
                "expected_draft_version": 1,
                "scheduled_for": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            },
        )

    assert too_soon.status_code == 422
    assert too_soon.json()["error"]["code"] == "editorial_schedule_too_soon"
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "editorial_document_invalid"
    assert schedule_state.schedule_writes == 0


def test_schedule_request_requires_timezone_and_schema_is_additive():
    with pytest.raises(ValueError):
        EditorialScheduleUpsertRequest(
            expected_draft_version=1,
            scheduled_for=datetime(2026, 7, 20, 9, 0),
        )
    for expected in (
        "CREATE TABLE IF NOT EXISTS editorial_schedules",
        "idx_editorial_schedules_active_draft",
        "idx_editorial_schedules_due",
        "idx_editorial_schedules_updated",
    ):
        assert expected in SCHEMA_SQL


class CapturePool:
    def __init__(self):
        self.query = ""
        self.args = ()

    async def fetch(self, query, *args):
        self.query = query
        self.args = args
        return []


class CaptureDB:
    def __init__(self):
        self.pool = CapturePool()


@pytest.mark.asyncio
async def test_due_claim_is_fifo_leased_and_skip_locked():
    db = CaptureDB()
    repo = Repository(db)
    assert await repo.claim_due_editorial_schedules(limit=7, lease_seconds=60) == []
    normalized = " ".join(db.pool.query.split())
    assert "FOR UPDATE SKIP LOCKED" in normalized
    assert "ORDER BY scheduled_for, created_at, id" in normalized
    assert "state='processing'" in normalized
    assert db.pool.args == (7, 60)
