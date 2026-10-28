from __future__ import annotations

import pytest

from app.db import SCHEMA_SQL
from app.db_bootstrap import (
    REQUIRED_INDEXES,
    REQUIRED_TABLES,
    bootstrap_database,
    verify_empty_database,
)


class FakePool:
    def __init__(
        self,
        *,
        tables=REQUIRED_TABLES,
        indexes=REQUIRED_INDEXES,
        counts=None,
        non_null_image_data=0,
        dangling_article_images=0,
        dangling_image_links=0,
    ):
        self.tables = set(tables)
        self.indexes = set(indexes)
        self.counts = counts or {name: 0 for name in REQUIRED_TABLES}
        self.invariants = {
            "non_null_image_data": non_null_image_data,
            "dangling_article_images": dangling_article_images,
            "dangling_image_links": dangling_image_links,
        }
        self.queries: list[str] = []
        self.fetchrow_calls = 0

    async def fetch(self, query, _required):
        self.queries.append(query)
        if "pg_catalog.pg_tables" in query:
            return [{"tablename": name} for name in sorted(self.tables)]
        return [{"indexname": name} for name in sorted(self.indexes)]

    async def fetchrow(self, query):
        self.queries.append(query)
        self.fetchrow_calls += 1
        if "non_null_image_data" in query:
            return self.invariants
        return self.counts


class FakeDB:
    def __init__(self, pool: FakePool):
        self.pool = pool
        self.ensure_calls = 0

    async def ensure_schema(self):
        self.ensure_calls += 1


@pytest.mark.asyncio
async def test_ensure_calls_schema_then_verify():
    db = FakeDB(FakePool())

    report = await bootstrap_database(db, ensure=True)

    assert db.ensure_calls == 1
    assert report.ok is True
    assert report.legacy_data_absent is True


@pytest.mark.asyncio
async def test_verify_accepts_all_zero_and_required_schema_matches_sql():
    db = FakeDB(FakePool())

    report = await verify_empty_database(db)

    assert report.ok is True
    assert report.counts == {name: 0 for name in REQUIRED_TABLES}
    assert all(report.tables.values())
    assert all(report.indexes.values())
    for name in REQUIRED_TABLES + REQUIRED_INDEXES:
        assert name in SCHEMA_SQL


@pytest.mark.asyncio
async def test_verify_reports_missing_table_without_querying_it():
    missing = REQUIRED_TABLES[-1]
    pool = FakePool(tables=REQUIRED_TABLES[:-1])

    report = await verify_empty_database(FakeDB(pool))

    assert report.ok is False
    assert report.tables[missing] is False
    assert report.counts == {}
    assert pool.fetchrow_calls == 0


@pytest.mark.asyncio
async def test_verify_rejects_non_null_bytea():
    report = await verify_empty_database(
        FakeDB(FakePool(non_null_image_data=1))
    )

    assert report.ok is False
    assert report.non_null_image_data == 1
    assert report.legacy_data_absent is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("article_dangling", "link_dangling"),
    [(1, 0), (0, 1)],
)
async def test_verify_rejects_dangling_refs(article_dangling, link_dangling):
    report = await verify_empty_database(
        FakeDB(
            FakePool(
                dangling_article_images=article_dangling,
                dangling_image_links=link_dangling,
            )
        )
    )

    assert report.ok is False
    assert report.dangling_article_images == article_dangling
    assert report.dangling_image_links == link_dangling


@pytest.mark.asyncio
async def test_verify_does_not_migrate_or_delete_data():
    pool = FakePool()

    await verify_empty_database(FakeDB(pool))

    combined = "\n".join(pool.queries).lower()
    for forbidden in ("insert ", "update ", "delete ", "vacuum", "truncate"):
        assert forbidden not in combined
