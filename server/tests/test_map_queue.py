"""Persistent Map queue repository contract tests."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.db import SCHEMA_SQL
from app.models import Article
from app.repository import Repository


def _article(article_id: str = "article-1") -> Article:
    return Article(
        id=article_id,
        url=f"https://news.example/{article_id}",
        title="Queued article",
        seendate=None,
        domain="news.example",
        language="English",
        sourcecountry="US",
        url_mobile=None,
        socialimage_url=None,
    )


class RecordingPool:
    def __init__(self, *, row=None, value=None, mappable=("core",)):
        self.row = row
        self.value = value
        # Which domains own a positive daily budget. The default is the
        # DR-13208 quarantine: core only.
        self.mappable = [
            {"coverage_domain": name} for name in mappable
        ]
        self.calls: list[tuple[str, str, tuple]] = []

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        if "map_budget_domain_settings" in query:
            return self.mappable
        return []

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        return self.row

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", query, args))
        return self.value

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        return "INSERT 0 1"


def _repo(pool) -> Repository:
    return Repository(SimpleNamespace(pool=pool))


def _write_call(pool):
    """The gated statement, skipping the mappable-domain lookup before it."""
    return next(
        call
        for call in pool.calls
        if "map_budget_domain_settings" not in call[1]
    )


@pytest.mark.asyncio
async def test_recover_expired_map_leases_returns_rows_to_due_queue():
    pool = RecordingPool(value=7)

    recovered = await _repo(pool).recover_expired_map_leases(
        lease_seconds=1800
    )

    assert recovered == 7
    kind, query, args = pool.calls[0]
    assert kind == "fetchval"
    assert "status = 'queued'" in query
    assert "lease_expires_at = NULL" in query
    assert "next_attempt_at = LEAST(next_attempt_at, now())" in query
    assert "status = 'processing'" in query
    assert args == (1800,)


@pytest.mark.asyncio
async def test_recover_orphaned_map_leases_requeues_only_stopped_or_stale_owned_rows():
    pool = RecordingPool(value=2)

    recovered = await _repo(pool).recover_orphaned_map_leases(
        stale_after_s=20
    )

    assert recovered == 2
    kind, query, args = pool.calls[0]
    assert kind == "fetchval"
    assert "lease_owner IS NOT NULL" in query
    assert "FROM worker_instances" in query
    assert "wi.stopped_at IS NULL" in query
    assert "wi.last_seen_at >= now()" in query
    assert "lease_owner = NULL" in query
    assert "lease_expires_at = NULL" in query
    assert args == (20,)


@pytest.mark.asyncio
async def test_article_upsert_atomically_enqueues_without_reset():
    pool = RecordingPool(row={"inserted": True})

    outcome = await _repo(pool).upsert_article(_article())

    assert outcome == "inserted"
    # One lookup for the mappable-domain set, then the single gated statement.
    assert len(pool.calls) == 2
    _kind, query, _args = _write_call(pool)
    assert "WITH persisted AS" in query
    assert "INSERT INTO map_queue (article_id)" in query
    assert "SELECT id FROM persisted" in query
    assert "ON CONFLICT (article_id) DO NOTHING" in query


@pytest.mark.asyncio
async def test_save_map_and_done_share_one_statement():
    pool = RecordingPool()
    repo = _repo(pool)

    await repo.save_map_and_mark_done(
        "article-1",
        format_version=3,
        model="test/model",
        status="ok",
        data={"format_version": 3},
        error=None,
    )

    map_write_calls = [call for call in pool.calls if call[0] == "execute"]
    assert len(map_write_calls) == 1
    kind, query, args = map_write_calls[0]
    assert kind == "execute"
    assert "saved AS" in query
    assert "INSERT INTO article_maps" in query
    assert "INSERT INTO map_queue" in query
    assert "status = 'done'" in query
    assert "lease_expires_at = NULL" in query
    assert "lease_owner = NULL" in query
    assert args[:4] == ("article-1", 3, "test/model", "ok")

    with pytest.raises(ValueError, match="successful map status"):
        await repo.save_map_and_mark_done(
            "article-1",
            format_version=3,
            model="test/model",
            status="failed",
            data={},
            error="no",
        )


@pytest.mark.asyncio
async def test_rag_links_map_and_done_share_the_same_statement():
    pool = RecordingPool()
    repo = _repo(pool)

    await repo.save_map_and_mark_done(
        "article-1",
        format_version=3,
        model="test/model",
        status="ok",
        data={"format_version": 3},
        error=None,
        rag_links=[
            {
                "ref_index": 2,
                "image_id": "image-rag",
                "role": "rag",
                "source_url": "https://images.example/rag.jpg",
            }
        ],
    )

    map_write_calls = [call for call in pool.calls if call[0] == "execute"]
    assert len(map_write_calls) == 1
    _kind, query, args = map_write_calls[0]
    assert "removed_rag AS" in query
    assert "jsonb_to_recordset($7::jsonb)" in query
    assert "linked AS" in query
    assert "INSERT INTO article_maps" in query
    assert "INSERT INTO map_queue" in query
    assert '"role": "rag"' in args[6]


@pytest.mark.asyncio
async def test_reconcile_covers_legacy_status_matrix_without_resetting_retries():
    pool = RecordingPool(value=4)

    changed = await _repo(pool).reconcile_map_queue(max_attempts=3)

    assert changed == 4
    kind, query, args = _write_call(pool)
    assert kind == "fetchval"
    assert "WHEN m.status IN ('ok', 'degraded') THEN 'done'" in query
    # A finished Map withheld from readers is still finished work.
    assert "WHEN m.status IN ('held_ok', 'held_degraded') THEN 'done'" in query
    assert "WHEN m.status = 'failed' THEN 'dead'" in query
    assert "ELSE 'queued'" in query
    assert "WHERE EXCLUDED.status = 'done'" in query
    assert "map_queue.status <> 'done'" in query
    assert args == (3, ["core"])


@pytest.mark.asyncio
async def test_requeue_clears_processing_lease():
    pool = RecordingPool(value=0)

    status, attempts = await _repo(pool).queue_requeue(
        "article-1",
        error="temporary",
        max_attempts=3,
        base_delay_s=10,
    )

    assert (status, attempts) == ("queued", 1)
    _kind, query, _args = pool.calls[-1]
    assert "lease_expires_at" in query
    assert "lease_expires_at = NULL" in query
    assert "lease_owner = NULL" in query


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None


class ClaimConnection:
    def __init__(self, row):
        self.row = row
        self.calls: list[tuple[str, str, tuple]] = []

    def transaction(self):
        return _Transaction()

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        return self.row


class _Acquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_exc):
        return None


class ClaimPool:
    def __init__(self, row):
        self.connection = ClaimConnection(row)

    def acquire(self):
        return _Acquire(self.connection)


@pytest.mark.asyncio
async def test_claim_is_oldest_due_and_respects_global_active_limit():
    pool = ClaimPool({"article_id": "oldest"})

    claimed = await _repo(pool).queue_claim_next(
        lease_seconds=1800,
        max_active=3,
    )

    assert claimed == "oldest"
    assert len(pool.connection.calls) == 2
    assert "pg_advisory_xact_lock" in pool.connection.calls[0][1]
    _kind, query, args = pool.connection.calls[1]
    assert "q.status = 'queued'" in query
    assert "q.status = 'processing'" in query
    assert "lease_expires_at" in query
    assert "SELECT count(*)" in query
    assert ") < $2" in query
    assert "ORDER BY a.first_seen_at ASC" in query
    assert "q.created_at ASC" in query
    assert "q.article_id ASC" in query
    assert "LIMIT 1" in query
    assert "FOR UPDATE OF q SKIP LOCKED" in query
    assert args == (1800, 3, None)


@pytest.mark.asyncio
async def test_claim_records_lease_owner_and_rejects_blank_owner():
    pool = ClaimPool({"article_id": "oldest"})

    claimed = await _repo(pool).queue_claim_next(
        lease_seconds=1800,
        max_active=3,
        lease_owner="worker-1",
    )

    assert claimed == "oldest"
    _kind, query, args = pool.connection.calls[1]
    assert "lease_owner = $3" in query
    assert args == (1800, 3, "worker-1")

    with pytest.raises(ValueError, match="lease_owner"):
        await _repo(ClaimPool(None)).queue_claim_next(
            lease_seconds=1800,
            max_active=3,
            lease_owner="   ",
        )


@pytest.mark.asyncio
async def test_claim_rejects_non_positive_active_limit_before_db_access():
    with pytest.raises(ValueError, match="max_active"):
        await _repo(ClaimPool(None)).queue_claim_next(
            lease_seconds=1800,
            max_active=0,
        )


def test_map_queue_schema_has_fifo_and_lease_contract():
    assert "lease_expires_at TIMESTAMPTZ" in SCHEMA_SQL
    assert "lease_owner TEXT" in SCHEMA_SQL
    assert "idx_queue_fifo" in SCHEMA_SQL
    assert "idx_queue_lease" in SCHEMA_SQL
    assert "idx_queue_live_owner" in SCHEMA_SQL


# ---- Coverage domain quarantine (DR-13208) ------------------------------


def _normalized(query: str) -> str:
    """Collapse whitespace after dropping ``--`` comments.

    Comments are stripped so that prose explaining a column cannot satisfy (or
    break) an assertion about the SQL that actually executes.
    """
    lines = [line.split("--", 1)[0] for line in query.splitlines()]
    return " ".join(" ".join(lines).split())


@pytest.mark.asyncio
async def test_upsert_article_enqueues_core_domain():
    pool = RecordingPool(row={"inserted": True})
    article = _article()

    assert article.coverage_domain == "core"
    await _repo(pool).upsert_article(article)

    _kind, query, args = _write_call(pool)
    assert "INSERT INTO map_queue (article_id)" in _normalized(query)
    assert (
        "SELECT id FROM persisted WHERE coverage_domain = ANY($14::text[])"
        in _normalized(query)
    )
    # The quarantine is now a set rather than a literal, and core alone is
    # what a deployment without a positive crypto budget resolves to.
    assert args[13] == ["core"]
    assert args[12] == "core"
    assert args[14:] == (None, None)


@pytest.mark.asyncio
async def test_upsert_article_quarantines_crypto_domain():
    """The gate is in SQL, so the crypto article is still written to articles
    but its map_queue insert selects nothing."""
    pool = RecordingPool(row={"inserted": True})
    article = _article()
    article.coverage_domain = "crypto"

    await _repo(pool).upsert_article(article)

    _kind, query, args = _write_call(pool)
    assert args[12] == "crypto"
    assert "INSERT INTO articles" in _normalized(query)
    # crypto is absent from the mappable set, so the enqueue selects nothing.
    assert args[13] == ["core"]


@pytest.mark.asyncio
async def test_upsert_article_never_overwrites_existing_coverage_domain():
    """First attribution wins: rediscovery must not move an article across the
    quarantine boundary in either direction."""
    pool = RecordingPool(row={"inserted": False})

    await _repo(pool).upsert_article(_article())

    _kind, query, _args = _write_call(pool)
    conflict_clause = _normalized(query).split("ON CONFLICT (id) DO UPDATE SET")[1]
    conflict_clause = conflict_clause.split("RETURNING")[0]
    assert "coverage_domain" not in conflict_clause
    assert "document_type" not in conflict_clause
    assert "document_source_name" not in conflict_clause


@pytest.mark.asyncio
async def test_upsert_article_reads_domain_back_from_persisted_row():
    """The enqueue decision must use the stored value, not EXCLUDED."""
    pool = RecordingPool(row={"inserted": True})

    await _repo(pool).upsert_article(_article())

    _kind, query, _args = _write_call(pool)
    assert "RETURNING id, (xmax = 0) AS inserted, coverage_domain" in _normalized(query)
    assert "EXCLUDED.coverage_domain" not in query


@pytest.mark.asyncio
async def test_reconcile_map_queue_skips_non_core_domains():
    pool = RecordingPool(value=0)

    await _repo(pool).reconcile_map_queue(max_attempts=5)

    _kind, query, args = _write_call(pool)
    assert "WHERE a.coverage_domain = ANY($2::text[])" in _normalized(query)
    assert "WHERE true" not in _normalized(query)
    assert args[-1] == ["core"]
