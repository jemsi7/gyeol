"""PostgreSQL performance gate for the domain-aware Map budget claim.

The measured wall interval deliberately contains more than the advisory-lock
hold: pool acquisition, waiting, the transaction, and commit are all inside
it. A sub-50ms p95 here therefore bounds the lock itself conservatively.
"""
from __future__ import annotations

import asyncio
import math
import os
from time import perf_counter
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from app.db import SCHEMA_SQL
from app.repository import MapBudget, Repository


SCHEMA = "map_budget_domain_perf_test"
CLAIM_COUNT = 60
LOCK_P95_LIMIT_SECONDS = 0.050


def _local_staging_dsn() -> str:
    value = os.environ.get("TEST_POSTGRES_DSN", "").strip()
    if not value:
        pytest.skip("TEST_POSTGRES_DSN is required for PostgreSQL performance tests")
    parsed = urlsplit(value)
    database_name = parsed.path.lstrip("/")
    hostname = (parsed.hostname or "").lower()
    if (
        hostname not in {"127.0.0.1", "localhost", "::1"}
        or not database_name.startswith("staging_test")
        or "railway" in value.lower()
    ):
        pytest.fail(
            "TEST_POSTGRES_DSN must target a local staging_test database"
        )
    return value


@pytest.mark.asyncio
async def test_budgeted_claim_wall_p95_bounds_lock_below_50ms():
    import asyncpg

    dsn = _local_staging_dsn()
    admin = await asyncpg.connect(dsn)
    pool = None
    try:
        await admin.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
        await admin.execute(f'CREATE SCHEMA "{SCHEMA}"')
        await admin.execute(f'SET search_path TO "{SCHEMA}"')
        await admin.execute(SCHEMA_SQL)

        rows = [
            (
                f"perf-{index:03d}",
                f"https://performance.invalid/{index:03d}",
                "core" if index < 40 else "crypto",
                (
                    "regulatory_filing"
                    if index >= CLAIM_COUNT - 7
                    else None
                ),
            )
            for index in range(CLAIM_COUNT)
        ]
        await admin.executemany(
            """
            INSERT INTO articles (
                id, url, coverage_domain, document_type, first_seen_at
            )
            VALUES ($1, $2, $3, $4, now())
            """,
            rows,
        )
        await admin.executemany(
            "INSERT INTO map_queue (article_id) VALUES ($1)",
            [(article_id,) for article_id, _url, _domain, _type in rows],
        )
        await admin.execute(
            """
            UPDATE map_queue q
            SET triage_score = 80,
                triage_signals = '{
                    "axes": {}, "fallback": false,
                    "profile": "crypto", "relevance": "on_topic"
                }'::jsonb,
                triage_model = 'test/crypto-relevance',
                triage_scored_at = now()
            FROM articles a
            WHERE a.id = q.article_id AND a.coverage_domain = 'crypto'
            """
        )

        pool = await asyncpg.create_pool(
            dsn,
            min_size=1,
            max_size=4,
            command_timeout=60,
            server_settings={"search_path": f"{SCHEMA},pg_catalog"},
        )
        repository = Repository(SimpleNamespace(pool=pool))
        budget = MapBudget(
            daily_limit=CLAIM_COUNT,
            fresh_share_percent=75,
            daily_run_count=1,
        )
        durations: list[float] = []
        claimed_ids: set[str] = set()
        for index in range(CLAIM_COUNT):
            started = perf_counter()
            article_id = await repository._budgeted_claim(
                lease_seconds=300,
                max_active=CLAIM_COUNT + 1,
                lease_owner=f"perf-worker-{index:03d}",
                budget=budget,
                ranked=False,
                stale_after_s=0,
                neutral_score=50,
            )
            durations.append(perf_counter() - started)
            assert article_id is not None
            claimed_ids.add(article_id)

        assert len(claimed_ids) == CLAIM_COUNT
        ordered = sorted(durations)
        p95 = ordered[math.ceil(len(ordered) * 0.95) - 1]
        print(f"budgeted_claim_wall_p95_ms={p95 * 1000:.3f}")
        assert p95 < LOCK_P95_LIMIT_SECONDS
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
        await admin.close()


@pytest.mark.asyncio
async def test_concurrent_crypto_claims_stop_at_exact_article13_document7():
    import asyncpg

    dsn = _local_staging_dsn()
    schema = "map_budget_document_split_test"
    admin = await asyncpg.connect(dsn)
    pool = None
    try:
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        await admin.execute(f'SET search_path TO "{schema}"')
        await admin.execute(SCHEMA_SQL)
        rows = [
            (
                f"strict-{index:03d}",
                f"https://strict.invalid/{index:03d}",
                "regulatory_filing" if index >= 20 else None,
            )
            for index in range(40)
        ]
        await admin.executemany(
            """
            INSERT INTO articles (
                id, url, coverage_domain, document_type, first_seen_at
            )
            VALUES ($1, $2, 'crypto', $3, now())
            """,
            rows,
        )
        await admin.executemany(
            "INSERT INTO map_queue (article_id) VALUES ($1)",
            [(article_id,) for article_id, _url, _type in rows],
        )
        await admin.execute(
            """
            UPDATE map_queue q
            SET triage_score = 80,
                triage_signals = '{
                    "axes": {}, "fallback": false,
                    "profile": "crypto", "relevance": "on_topic"
                }'::jsonb,
                triage_model = 'test/crypto-relevance',
                triage_scored_at = now()
            FROM articles a
            WHERE a.id = q.article_id AND a.coverage_domain = 'crypto'
            """
        )
        pool = await asyncpg.create_pool(
            dsn,
            min_size=1,
            max_size=30,
            command_timeout=60,
            server_settings={"search_path": f"{schema},pg_catalog"},
        )
        repository = Repository(SimpleNamespace(pool=pool))
        budget = MapBudget(
            daily_limit=60,
            fresh_share_percent=75,
            daily_run_count=1,
        )

        claimed = await asyncio.gather(
            *(
                repository._budgeted_claim(
                    lease_seconds=300,
                    max_active=100,
                    lease_owner=f"strict-worker-{index:03d}",
                    budget=budget,
                    ranked=False,
                    stale_after_s=0,
                    neutral_score=50,
                )
                for index in range(30)
            )
        )
        claimed_ids = [article_id for article_id in claimed if article_id]
        assert len(claimed_ids) == 20
        counts = await admin.fetchrow(
            """
            SELECT count(*) FILTER (WHERE document_type IS NULL) AS articles,
                   count(*) FILTER (WHERE document_type IS NOT NULL) AS documents
            FROM articles
            WHERE id = ANY($1::text[])
            """,
            claimed_ids,
        )
        assert int(counts["articles"]) == 13
        assert int(counts["documents"]) == 7

        assert await repository._budgeted_claim(
            lease_seconds=300,
            max_active=100,
            lease_owner="strict-worker-final",
            budget=budget,
            ranked=False,
            stale_after_s=0,
            neutral_score=50,
        ) is None
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()
