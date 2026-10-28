from __future__ import annotations

from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from app.api import app, state
from app.config import Settings
from app.editorial_service import editor_token_hash
from app.repository import Repository


NOW = datetime(2026, 7, 19, 8, 0, tzinfo=UTC)
TOKEN = "o" * 43


class TodayRepo:
    def __init__(self):
        self.operator_id = uuid4()
        self.items = [
            {
                "draft_id": uuid4(),
                "article_id": character * 64,
                "headline": f"심층 기사 {character.upper()}",
                "published_at": NOW - timedelta(minutes=index),
                "display_order": None,
                "is_featured": False,
            }
            for index, character in enumerate(("a", "b", "c"))
        ]
        self.mutations = 0

    async def authenticate_editor_token(self, token_hash):
        if token_hash != editor_token_hash(TOKEN):
            return None
        return {
            "id": self.operator_id,
            "display_name": "오늘 편집자",
            "role": "editor",
            "created_at": NOW,
            "last_used_at": NOW,
        }

    @staticmethod
    def _sort(items):
        return sorted(
            items,
            key=lambda item: (
                not item["is_featured"],
                item["display_order"] is None,
                item["display_order"] if item["display_order"] is not None else 2_147_483_647,
                -item["published_at"].timestamp(),
                -int(item["article_id"], 16),
            ),
        )

    async def editorial_today(self, editorial_day):
        return self._sort(deepcopy(self.items))

    async def set_editorial_today_order(
        self, *, editorial_day, ordered_article_ids, featured_article_id, operator_id
    ):
        eligible = {item["article_id"] for item in self.items}
        requested = set(ordered_article_ids)
        if featured_article_id:
            requested.add(featured_article_id)
        invalid = sorted(requested - eligible)
        if invalid:
            return {"kind": "invalid", "invalid_article_ids": invalid}
        self.mutations += 1
        positions = {article_id: index for index, article_id in enumerate(ordered_article_ids)}
        for item in self.items:
            item["display_order"] = positions.get(item["article_id"])
            item["is_featured"] = item["article_id"] == featured_article_id
        return {"kind": "updated", "items": self._sort(deepcopy(self.items))}


def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


@pytest.mark.asyncio
async def test_today_defaults_to_newest_then_reflects_reorder_and_featured_override():
    repo = TodayRepo()
    state.settings = Settings(_env_file=None, API_TOKEN="master")
    state.repo = repo
    article_a, article_b, article_c = [item["article_id"] for item in repo.items]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        default = await client.get("/v1/editor/today", headers=auth())
        changed = await client.put(
            "/v1/editor/today-order",
            headers=auth(),
            json={
                "ordered_article_ids": [article_b, article_a],
                "featured_article_id": article_c,
            },
        )

    assert default.status_code == 200
    assert [item["article_id"] for item in default.json()["items"]] == [
        article_a, article_b, article_c
    ]
    assert changed.status_code == 200
    assert [item["article_id"] for item in changed.json()["items"]] == [
        article_c, article_b, article_a
    ]
    assert changed.json()["items"][0]["is_featured"] is True
    assert [item["display_order"] for item in changed.json()["items"][1:]] == [0, 1]


@pytest.mark.asyncio
async def test_invalid_duplicate_unknown_or_withdrawn_order_is_all_or_nothing():
    repo = TodayRepo()
    state.settings = Settings(_env_file=None, API_TOKEN="master")
    state.repo = repo
    original = deepcopy(repo.items)
    known = repo.items[0]["article_id"]
    unknown = "f" * 64
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        duplicate = await client.put(
            "/v1/editor/today-order",
            headers=auth(),
            json={"ordered_article_ids": [known, known], "featured_article_id": None},
        )
        invalid = await client.put(
            "/v1/editor/today-order",
            headers=auth(),
            json={"ordered_article_ids": [known], "featured_article_id": unknown},
        )

    assert duplicate.status_code == 422
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "invalid_editorial_today_articles"
    assert repo.items == original
    assert repo.mutations == 0


class Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class OrderConnection:
    def __init__(self):
        self.fetch_queries = []
        self.execute_query = ""

    def transaction(self):
        return Transaction()

    async def fetch(self, query, *args):
        self.fetch_queries.append((query, args))
        if "SELECT article_id" in query:
            return [{"article_id": "a" * 64}, {"article_id": "b" * 64}]
        return [
            {
                "draft_id": uuid4(),
                "article_id": "b" * 64,
                "headline": "B",
                "published_at": NOW,
                "display_order": 0,
                "is_featured": True,
            },
            {
                "draft_id": uuid4(),
                "article_id": "a" * 64,
                "headline": "A",
                "published_at": NOW,
                "display_order": 1,
                "is_featured": False,
            },
        ]

    async def execute(self, query, *args):
        self.execute_query = query
        return "INSERT 0 1"


class Acquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class Pool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return Acquire(self.connection)


class DB:
    def __init__(self, connection):
        self.pool = Pool(connection)


@pytest.mark.asyncio
async def test_repository_order_locks_today_rows_updates_all_and_audits():
    connection = OrderConnection()
    repo = Repository(DB(connection))
    result = await repo.set_editorial_today_order(
        editorial_day=date(2026, 7, 19),
        ordered_article_ids=["b" * 64, "a" * 64],
        featured_article_id="b" * 64,
        operator_id=uuid4(),
    )
    assert result["kind"] == "updated"
    lock_sql = " ".join(connection.fetch_queries[0][0].split())
    update_sql = " ".join(connection.fetch_queries[1][0].split())
    assert "FOR UPDATE" in lock_sql
    assert "AT TIME ZONE 'Asia/Seoul'" in lock_sql
    assert "array_position" in update_sql
    assert "ELSE NULL" in update_sql
    assert "is_featured" in update_sql
    assert "today.order.updated" in connection.execute_query
