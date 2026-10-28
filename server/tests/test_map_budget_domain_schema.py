"""Schema tests for the per-domain Map budget axis (I-13200-c, DR-13206).

The shape tests read SCHEMA_SQL directly so they run without a database: the
DDL text is the artifact this unit ships. The behavioural tests -- in-flight
consumed counts surviving the primary-key widening, and the seed not
overwriting operator edits -- need a real PostgreSQL and skip without one.
"""
from __future__ import annotations

import os
import re

import pytest

from app.db import SCHEMA_SQL
from app.db_bootstrap import REQUIRED_INDEXES, REQUIRED_TABLES


def _postgres_dsn() -> str:
    value = os.environ.get("TEST_POSTGRES_DSN", "").strip()
    if not value:
        pytest.skip("TEST_POSTGRES_DSN is required for PostgreSQL integration tests")
    return value


def test_domain_settings_table_is_declared_and_required():
    assert "CREATE TABLE IF NOT EXISTS map_budget_domain_settings" in SCHEMA_SQL
    assert "coverage_domain TEXT PRIMARY KEY" in SCHEMA_SQL
    assert "CHECK (daily_map_limit BETWEEN 0 AND 1000)" in SCHEMA_SQL
    assert "map_budget_domain_settings" in REQUIRED_TABLES


def test_seed_matches_dr_13206_split_and_never_overwrites():
    seed = re.search(
        r"INSERT INTO map_budget_domain_settings\s.*?;",
        SCHEMA_SQL,
        re.DOTALL,
    )
    assert seed is not None
    statement = seed.group(0)
    # The DR-13206 split, with the DR-13209 reader gate closed for crypto:
    # it earns a Map budget but reaches no reader surface.
    assert "('core', 40, TRUE, 0), ('crypto', 20, FALSE, 7)" in statement
    # DO NOTHING, never DO UPDATE: a boot must not reset an operator's edit.
    assert "ON CONFLICT (coverage_domain) DO NOTHING" in statement
    assert "DO UPDATE" not in statement


def test_ledger_and_claims_gain_coverage_domain_defaulting_to_core():
    for table in ("map_budget_ledger", "map_budget_claims"):
        assert (
            f"ALTER TABLE {table}\n"
            "    ADD COLUMN IF NOT EXISTS coverage_domain TEXT NOT NULL DEFAULT 'core';"
            in SCHEMA_SQL
        )


def test_ledger_key_widening_is_guarded_and_adds_column_first():
    add_column = SCHEMA_SQL.index(
        "ALTER TABLE map_budget_ledger\n"
        "    ADD COLUMN IF NOT EXISTS coverage_domain"
    )
    widen = SCHEMA_SQL.index("ADD PRIMARY KEY (editorial_day, coverage_domain, bucket)")
    # The column must exist before the key references it, otherwise an
    # in-flight editorial day would lose its consumed counts.
    assert add_column < widen
    guard = SCHEMA_SQL[SCHEMA_SQL.index("DECLARE\n    ledger_key") : widen]
    assert "contype = 'p'" in guard
    assert "ledger_key_cols = 2" in guard


def test_domain_claim_index_exists():
    assert "CREATE INDEX IF NOT EXISTS idx_map_budget_claims_domain" in SCHEMA_SQL
    assert "ON map_budget_claims (editorial_day, coverage_domain, status)" in SCHEMA_SQL
    assert "idx_map_budget_claims_domain" in REQUIRED_INDEXES


def test_primary_document_columns_limit_and_partial_index_exist():
    assert "ADD COLUMN IF NOT EXISTS document_type TEXT" in SCHEMA_SQL
    assert "ADD COLUMN IF NOT EXISTS document_source_name TEXT" in SCHEMA_SQL
    assert "document_map_limit INTEGER NOT NULL DEFAULT 0" in SCHEMA_SQL
    assert "CHECK (document_map_limit BETWEEN 0 AND 1000)" in SCHEMA_SQL
    assert "idx_articles_document_type" in REQUIRED_INDEXES
    assert "WHERE document_type IS NOT NULL" in SCHEMA_SQL


@pytest.mark.asyncio
async def test_ddl_is_idempotent_and_preserves_consumed():
    dsn = _postgres_dsn()
    import asyncpg

    schema = "map_budget_domain_axis_test"
    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.execute(f'CREATE SCHEMA "{schema}"')
        await connection.execute(f'SET search_path TO "{schema}"')

        # Build the pre-axis shape, spend some of an in-flight editorial day,
        # then apply the real DDL twice.
        await connection.execute(
            """
            CREATE TABLE map_budget_ledger (
                editorial_day DATE NOT NULL,
                bucket        TEXT NOT NULL CHECK (bucket IN ('fresh','backlog')),
                consumed      INTEGER NOT NULL DEFAULT 0 CHECK (consumed >= 0),
                updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (editorial_day, bucket)
            );
            INSERT INTO map_budget_ledger (editorial_day, bucket, consumed)
            VALUES (DATE '2026-08-09', 'fresh', 7);
            """
        )
        for _ in range(2):
            await connection.execute(SCHEMA_SQL)

        consumed = await connection.fetchval(
            """
            SELECT consumed FROM map_budget_ledger
            WHERE editorial_day = DATE '2026-08-09'
              AND coverage_domain = 'core'
              AND bucket = 'fresh'
            """
        )
        assert consumed == 7

        key_columns = await connection.fetchval(
            """
            SELECT array_length(conkey, 1) FROM pg_constraint
            WHERE conrelid = 'map_budget_ledger'::regclass AND contype = 'p'
            """
        )
        assert key_columns == 3

        rows = await connection.fetch(
            "SELECT coverage_domain, daily_map_limit, document_map_limit "
            "FROM map_budget_domain_settings"
        )
        limits = {
            row["coverage_domain"]: (
                row["daily_map_limit"],
                row["document_map_limit"],
            )
            for row in rows
        }
        assert limits == {"core": (40, 0), "crypto": (20, 7)}
    finally:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.close()


@pytest.mark.asyncio
async def test_reseed_does_not_overwrite_operator_values():
    dsn = _postgres_dsn()
    import asyncpg

    schema = "map_budget_domain_reseed_test"
    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.execute(f'CREATE SCHEMA "{schema}"')
        await connection.execute(f'SET search_path TO "{schema}"')
        await connection.execute(SCHEMA_SQL)
        await connection.execute(
            "UPDATE map_budget_domain_settings "
            "SET daily_map_limit = 5, document_map_limit = 3"
            " WHERE coverage_domain = 'crypto'"
        )
        await connection.execute(SCHEMA_SQL)
        row = await connection.fetchrow(
            "SELECT daily_map_limit, document_map_limit "
            "FROM map_budget_domain_settings WHERE coverage_domain = 'crypto'"
        )
        assert tuple(row) == (5, 3)
    finally:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.close()
