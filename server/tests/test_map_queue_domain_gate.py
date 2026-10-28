"""map_queue admission by coverage domain (I-13200-i, DR-13208).

DR-13208 quarantined crypto with a SQL literal because no per-domain budget
existed yet. Now that one does, the gate asks the budget instead: a domain is
admitted exactly when it owns a positive daily limit. The gate is widened, not
deleted -- a future third domain must be quarantined by default rather than
silently spending another domain's share.
"""
from __future__ import annotations

import pytest

from tests.test_map_queue import RecordingPool, _article, _repo, _write_call


@pytest.mark.asyncio
async def test_admission_follows_the_positive_budget():
    pool = RecordingPool(row={"inserted": True}, mappable=("core", "crypto"))
    article = _article()
    article.coverage_domain = "crypto"

    await _repo(pool).upsert_article(article)

    _kind, _query, args = _write_call(pool)
    assert args[13] == ["core", "crypto"]


@pytest.mark.asyncio
async def test_zero_budget_keeps_a_domain_quarantined():
    pool = RecordingPool(row={"inserted": True}, mappable=("core",))
    article = _article()
    article.coverage_domain = "crypto"

    await _repo(pool).upsert_article(article)

    _kind, query, args = _write_call(pool)
    assert args[13] == ["core"]
    # The article is still stored; only its queue insert selects nothing.
    assert "INSERT INTO articles" in query


@pytest.mark.asyncio
async def test_empty_budget_axis_falls_back_to_quarantine():
    # An empty settings table must not read as "everything is allowed".
    pool = RecordingPool(row={"inserted": True}, mappable=())

    assert await _repo(pool).mappable_domains() == ("core",)


@pytest.mark.asyncio
async def test_missing_budget_table_falls_back_to_quarantine():
    import asyncpg

    class MissingTablePool(RecordingPool):
        async def fetch(self, query, *args):
            if "map_budget_domain_settings" in query:
                raise asyncpg.UndefinedTableError("no such table")
            return []

    assert await _repo(MissingTablePool()).mappable_domains() == ("core",)


@pytest.mark.asyncio
async def test_reconcile_uses_the_same_domain_set():
    pool = RecordingPool(value=0, mappable=("core", "crypto"))

    await _repo(pool).reconcile_map_queue(max_attempts=5)

    _kind, query, args = _write_call(pool)
    assert "WHERE a.coverage_domain = ANY($2::text[])" in query
    assert args == (5, ["core", "crypto"])


@pytest.mark.asyncio
async def test_reconcile_never_sweeps_a_quarantined_domain():
    # Gating only upsert_article would leak: this sweep would pull every
    # quarantined article into the queue at once.
    pool = RecordingPool(value=0, mappable=("core",))

    await _repo(pool).reconcile_map_queue(max_attempts=5)

    _kind, _query, args = _write_call(pool)
    assert args[-1] == ["core"]
