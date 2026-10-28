"""Storage for web-researched publisher interests (I-13212-e, DR-13247).

One table does three jobs -- cache, daily spend ledger, registry promotion
queue -- so these tests pin the query shapes each of them depends on.
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from app.db import SCHEMA_SQL
from app.repository import Repository


DAY = date(2026, 8, 17)
NOW = datetime(2026, 8, 17, 9, tzinfo=UTC)


class RecordingPool:
    def __init__(self, *, rows=None, row=None, value=None):
        self.rows = rows or []
        self.row = row
        self.value = value
        self.calls: list[tuple[str, str, tuple]] = []

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        return self.rows

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        return self.row

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", query, args))
        return self.value

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))


def _repo(pool):
    return Repository(SimpleNamespace(pool=pool))


def test_schema_declares_the_table_and_indexes():
    for value in (
        "CREATE TABLE IF NOT EXISTS publisher_interest_findings",
        "idx_publisher_interest_day",
        "idx_publisher_interest_status",
        "PRIMARY KEY (domain, assets_key)",
        "CHECK (confidence IN ('likely','uncertain'))",
        "CHECK (registry_status IN ('proposed','approved','rejected'))",
    ):
        assert value in SCHEMA_SQL


@pytest.mark.asyncio
async def test_cache_read_applies_the_age_bound_in_sql():
    pool = RecordingPool(row=None)
    assert await _repo(pool).read_publisher_interest("x.test", "BTC") is None
    _kind, query, args = pool.calls[0]
    assert "researched_at > now() - make_interval(days => $3::int)" in query
    assert args == ("x.test", "BTC", 30)


@pytest.mark.asyncio
async def test_cache_read_returns_the_row_as_a_dict():
    row = {"domain": "x.test", "assets_key": "BTC", "relation": "exchange_listed"}
    assert await _repo(RecordingPool(row=row)).read_publisher_interest("x.test", "BTC") == row


@pytest.mark.asyncio
async def test_write_upserts_and_resets_review_state():
    pool = RecordingPool()
    await _repo(pool).write_publisher_interest(
        {
            "domain": "x.test",
            "assets_key": "BTC",
            "relation": "exchange_listed",
            "owner": "예시거래소",
            "asset": "BTC",
            "confidence": "likely",
            "sentence": "예시거래소이(가) 발행했고…",
            "evidence": [{"url": "https://x.test/a", "quote": "lists BTC"}],
            "article_id": "article-1",
            "editorial_day": DAY,
        }
    )
    _kind, query, args = pool.calls[0]
    assert "ON CONFLICT (domain, assets_key) DO UPDATE" in query
    # A re-researched verdict goes back into the queue for review.
    assert "registry_status = 'proposed'" in query
    assert json.loads(args[7]) == [{"url": "https://x.test/a", "quote": "lists BTC"}]
    assert args[9] == DAY


@pytest.mark.asyncio
async def test_write_defaults_optional_slots_to_empty():
    pool = RecordingPool()
    await _repo(pool).write_publisher_interest(
        {
            "domain": "x.test",
            "assets_key": "BTC",
            "relation": "none",
            "confidence": "uncertain",
            "editorial_day": DAY,
        }
    )
    _kind, _query, args = pool.calls[0]
    assert args[3] == "" and args[4] == "" and args[6] == ""
    assert json.loads(args[7]) == []


@pytest.mark.asyncio
async def test_daily_count_includes_none_relation():
    pool = RecordingPool(value=3)
    assert await _repo(pool).count_publisher_research_runs(DAY) == 3
    _kind, query, args = pool.calls[0]
    assert "WHERE editorial_day = $1" in query
    assert "relation" not in query.split("WHERE")[1]
    assert args == (DAY,)


@pytest.mark.asyncio
async def test_daily_count_of_none_is_zero():
    assert await _repo(RecordingPool(value=None)).count_publisher_research_runs(DAY) == 0


@pytest.mark.asyncio
async def test_proposals_are_newest_first_and_exclude_none():
    pool = RecordingPool(rows=[{"domain": "a.test"}, {"domain": "b.test"}])
    result = await _repo(pool).list_publisher_interest_proposals(limit=10)
    _kind, query, args = pool.calls[0]
    assert "relation <> 'none'" in query
    assert "ORDER BY researched_at DESC" in query
    assert args == (10,)
    assert result == [{"domain": "a.test"}, {"domain": "b.test"}]


@pytest.mark.asyncio
async def test_triage_signals_are_decoded_from_json_text():
    pool = RecordingPool(row={"triage_signals": '{"reasons": ["wire_domain"]}'})
    assert await _repo(pool).read_triage_signals("article-1") == {
        "reasons": ["wire_domain"]
    }


@pytest.mark.asyncio
async def test_triage_signals_missing_or_malformed_is_none():
    assert await _repo(RecordingPool(row=None)).read_triage_signals("a") is None
    assert (
        await _repo(RecordingPool(row={"triage_signals": "not json"})).read_triage_signals("a")
        is None
    )
    assert (
        await _repo(RecordingPool(row={"triage_signals": None})).read_triage_signals("a")
        is None
    )
