from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest

from app.api import app, state
from app.config import Settings
from app.editorial_models import EditorialDocument
from app.editorial_service import editor_token_hash


NOW = datetime(2026, 7, 19, 13, 0, tzinfo=UTC)
TOKEN = "d" * 43


def document(headline: str = "") -> dict:
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


def test_editorial_document_publication_kind_is_additive_strict_and_defaults_legacy():
    assert EditorialDocument.model_validate(document()).publication_kind == "editorial"
    weekly = EditorialDocument.model_validate(
        {**document(), "publication_kind": "weekly_insight"}
    )
    assert weekly.publication_kind == "weekly_insight"
    with pytest.raises(ValueError):
        EditorialDocument.model_validate({**document(), "publication_kind": "weekly"})


class DraftRepo:
    def __init__(self):
        self.operator_id = uuid4()
        self.rows: dict[UUID, dict] = {}

    async def authenticate_editor_token(self, token_hash):
        if token_hash != editor_token_hash(TOKEN):
            return None
        return {
            "id": self.operator_id,
            "display_name": "편집자",
            "role": "editor",
            "created_at": NOW,
            "last_used_at": NOW,
        }

    async def create_editorial_draft(self, *, document, operator_id):
        draft_id = uuid4()
        row = {
            "id": draft_id,
            "status": "draft",
            "document": document,
            "version": 1,
            "created_by": operator_id,
            "updated_by": operator_id,
            "created_at": NOW,
            "updated_at": NOW,
        }
        self.rows[draft_id] = row
        return row

    async def editorial_drafts(self, limit=100):
        return list(self.rows.values())[:limit]

    async def get_editorial_draft(self, draft_id):
        return self.rows.get(draft_id)

    async def update_editorial_draft(
        self, *, draft_id, expected_version, document, operator_id
    ):
        current = self.rows.get(draft_id)
        if current is None or current["version"] != expected_version:
            return None
        row = {
            **current,
            "document": document,
            "version": expected_version + 1,
            "updated_by": operator_id,
            "updated_at": NOW,
        }
        self.rows[draft_id] = row
        return row


@pytest.fixture
def draft_state():
    repo = DraftRepo()
    state.settings = Settings(_env_file=None, API_TOKEN="master")
    state.repo = repo
    return repo


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


@pytest.mark.asyncio
async def test_draft_roundtrip_and_update_increments_once(draft_state):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/v1/editor/drafts", headers=auth(), json={"document": document()}
        )
        draft_id = created.json()["id"]
        updated = await client.put(
            f"/v1/editor/drafts/{draft_id}",
            headers=auth(),
            json={"expected_version": 1, "document": document("심층 기사")},
        )
        fetched = await client.get(f"/v1/editor/drafts/{draft_id}", headers=auth())
        listed = await client.get("/v1/editor/drafts", headers=auth())

    assert created.status_code == 201
    assert updated.status_code == 200
    assert updated.json()["version"] == 2
    assert fetched.json()["document"]["headline"] == "심층 기사"
    assert [row["id"] for row in listed.json()] == [draft_id]


@pytest.mark.asyncio
async def test_stale_update_returns_current_without_mutation(draft_state):
    current = await draft_state.create_editorial_draft(
        document=document("현재"), operator_id=draft_state.operator_id
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            f"/v1/editor/drafts/{current['id']}",
            headers=auth(),
            json={"expected_version": 9, "document": document("덮어쓰기")},
        )

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "draft_version_conflict"
    assert body["error"]["detail"]["current"]["document"]["headline"] == "현재"
    assert draft_state.rows[current["id"]]["version"] == 1


@pytest.mark.asyncio
async def test_missing_draft_and_invalid_shape(draft_state):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        missing = await client.get(f"/v1/editor/drafts/{uuid4()}", headers=auth())
        invalid = await client.post(
            "/v1/editor/drafts",
            headers=auth(),
            json={"document": {**document(), "format_version": 2}},
        )
    assert missing.status_code == 404
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "invalid_request"


def test_document_allows_incomplete_bounded_draft_and_rejects_caps():
    assert EditorialDocument.model_validate(document()).scenes == []
    with pytest.raises(ValueError):
        EditorialDocument.model_validate({**document(), "keywords": ["x" * 121]})
    with pytest.raises(ValueError):
        EditorialDocument.model_validate(
            {**document(), "scenes": [{"role": "context"}] * 10}
        )
