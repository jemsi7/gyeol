from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest

from app.api import app, state
from app.config import Settings
from app.editorial_service import EditorialError, editor_token_hash, extract_editor_bearer
from app.repository import Repository


NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)


def operator_row(operator_id: UUID | None = None, *, status: str = "active") -> dict:
    return {
        "id": operator_id or uuid4(),
        "display_name": "편집자",
        "role": "editor",
        "status": status,
        "created_at": NOW,
        "last_used_at": NOW,
        "revoked_at": NOW if status == "revoked" else None,
    }


class APIFakeRepo:
    def __init__(self):
        self.operator = operator_row()
        self.valid_hash: bytes | None = None
        self.created = []

    async def create_editor_operator(self, *, display_name, role):
        self.created.append((display_name, role))
        row = {**self.operator, "display_name": display_name, "role": role}
        token = "t" * 43
        self.valid_hash = editor_token_hash(token)
        return row, token

    async def authenticate_editor_token(self, token_hash):
        if token_hash == self.valid_hash and self.operator["status"] == "active":
            return self.operator
        return None

    async def revoke_editor_operator(self, operator_id):
        if operator_id != self.operator["id"]:
            return None
        self.operator = {**self.operator, "status": "revoked", "revoked_at": NOW}
        return self.operator


@pytest.fixture
def editor_state():
    repo = APIFakeRepo()
    state.settings = Settings(_env_file=None, API_TOKEN="master-secret")
    state.repo = repo
    return repo


@pytest.mark.asyncio
async def test_master_routes_fail_closed_without_api_token(editor_state):
    state.settings = Settings(_env_file=None, API_TOKEN="")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/editor/operators",
            headers={"X-API-Token": "anything"},
            json={"display_name": "편집자", "role": "editor"},
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "editor_master_token_not_configured"


@pytest.mark.asyncio
async def test_operator_create_authenticate_and_revoke(editor_state):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/v1/editor/operators",
            headers={"X-API-Token": "master-secret"},
            json={"display_name": "데스크 A", "role": "editor"},
        )
        token = created.json()["token"]
        me = await client.get(
            "/v1/editor/me", headers={"Authorization": f"Bearer {token}"}
        )
        revoked = await client.delete(
            f"/v1/editor/operators/{editor_state.operator['id']}",
            headers={"X-API-Token": "master-secret"},
        )
        rejected = await client.get(
            "/v1/editor/me", headers={"Authorization": f"Bearer {token}"}
        )

    assert created.status_code == 201
    assert len(token) == 43
    assert editor_state.created == [("데스크 A", "editor")]
    assert me.status_code == 200
    assert me.json()["display_name"] == "편집자"
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"
    assert rejected.status_code == 401
    assert token not in rejected.text


@pytest.mark.asyncio
async def test_editor_rejects_missing_malformed_and_unknown_tokens(editor_state):
    editor_state.valid_hash = editor_token_hash("t" * 43)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        responses = [
            await client.get("/v1/editor/me"),
            await client.get("/v1/editor/me", headers={"Authorization": "Basic abc"}),
            await client.get(
                "/v1/editor/me", headers={"Authorization": f"Bearer {'x' * 43}"}
            ),
        ]
    assert [response.status_code for response in responses] == [401, 401, 401]
    assert all(
        response.json()["error"]["code"] == "invalid_or_missing_editor_token"
        for response in responses
    )


def test_editor_bearer_parser_and_hash_are_deterministic():
    token = "z" * 43
    assert extract_editor_bearer(f"bearer\t{token}") == token
    assert editor_token_hash(token) == hashlib.sha256(token.encode()).digest()
    for invalid in (None, "", "Bearer short", "Basic " + token, "Bearer a b"):
        with pytest.raises(EditorialError):
            extract_editor_bearer(invalid)


class Pool:
    def __init__(self):
        self.args = None

    async def fetchrow(self, _query, *args):
        self.args = args
        return operator_row(args[0])


class DB:
    def __init__(self):
        self.pool = Pool()


@pytest.mark.asyncio
async def test_repository_persists_hash_not_returned_plaintext_token():
    db = DB()
    row, token = await Repository(db).create_editor_operator(
        display_name="편집자", role="editor"
    )
    assert row["display_name"] == "편집자"
    assert len(token) >= 32
    assert db.pool.args[3] == hashlib.sha256(token.encode()).digest()
    assert db.pool.args[4] == token[:8]
    assert token not in db.pool.args
