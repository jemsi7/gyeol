"""Reader exposure gate for non-core coverage domains (I-13200-g, DR-13209).

A withheld domain still gets Maps -- the tagging-accuracy measurement needs a
crypto corpus before F-1 is built -- but no reader surface may show them. The
gate is a stored status (``held_ok`` / ``held_degraded``) rather than a filter
added to each reader query: every reader path already matches ``ok`` or
``ok``/``degraded`` exactly, so a held Map is excluded by construction and no
future reader query can forget to exclude it.
"""
from __future__ import annotations

import os
import re

import pytest

from app.db import SCHEMA_SQL
from app.repository import Repository


READER_QUERY_FUNCTIONS = (
    "get_map_row",
    "recommendation_candidates",
    "recommendation_feed_rows",
    "account_saved_article_maps",
    "curation_candidate_rows",
    "curation_pool_state",
    "curation_search_rows",
    "curation_detail_rows",
    "timeline_cluster_candidates",
    "search_article_maps",
    "published_article_maps_by_ids",
    "recent_article_maps",
    "latest_article_maps",
    "random_article_maps",
    "available_article_count",
)


def _postgres_dsn() -> str:
    value = os.environ.get("TEST_POSTGRES_DSN", "").strip()
    if not value:
        pytest.skip("TEST_POSTGRES_DSN is required for PostgreSQL integration tests")
    return value


def _function_body(name: str) -> str:
    import inspect

    return inspect.getsource(getattr(Repository, name))


def test_gate_column_is_declared_and_seeded_closed_for_crypto():
    assert (
        "ADD COLUMN IF NOT EXISTS reader_exposed BOOLEAN NOT NULL DEFAULT FALSE"
        in SCHEMA_SQL
    )
    # crypto earns its Map budget but not reader exposure: the two axes are
    # independent, and only the second one is what a reader can observe.
    assert (
        "VALUES ('core', 40, TRUE, 0), ('crypto', 20, FALSE, 7)"
        in SCHEMA_SQL
    )


def test_saved_map_is_held_when_the_domain_is_not_exposed():
    body = _function_body("save_map_and_mark_done")
    assert "COALESCE(d.reader_exposed, TRUE)" in body
    assert "ELSE 'held_' || $4" in body
    # The decision is made inside the statement, so no caller can bypass it by
    # passing a status of its own.
    assert "reader_exposed" not in _function_body("upsert_map")


@pytest.mark.parametrize("name", READER_QUERY_FUNCTIONS)
def test_every_reader_query_matches_status_exactly(name):
    body = _function_body(name)
    matches = re.findall(r"status\s*(?:=|IN)\s*\(?[^\n]*", body)
    assert matches, f"{name} must filter article_maps.status"
    # Exact equality or an IN list -- never a prefix/LIKE match that a held_*
    # status could satisfy.
    assert not re.search(r"status\s+LIKE", body)
    for fragment in matches:
        assert "held" not in fragment


def test_queue_reconcile_treats_a_held_map_as_finished_work():
    body = _function_body("reconcile_map_queue")
    assert "WHEN m.status IN ('held_ok', 'held_degraded') THEN 'done'" in body
    # Otherwise a held Map would fall through to 'queued' and be re-mapped
    # forever, burning the domain's budget on work already done.
    assert body.index("held_ok") < body.index("ELSE 'queued'")


def test_mapped_checks_ignore_the_hold():
    # Both use "<> 'failed'", so a held Map already counts as mapped.
    assert "status <> 'failed'" in _function_body("has_map")
    assert "m.status <> 'failed'" in _function_body("unmapped_article_ids")


def test_exposure_toggle_preserves_the_quality_distinction():
    body = _function_body("set_domain_reader_exposure")
    # Opening unwraps held_ok -> ok and held_degraded -> degraded rather than
    # flattening both to ok.
    assert "substring(m.status FROM 6)" in body
    assert "ARRAY['held_ok', 'held_degraded']" in body
    assert "ARRAY['ok', 'degraded']" in body


def test_exposure_toggle_rejects_a_blank_domain():
    repo = Repository.__new__(Repository)
    with pytest.raises(ValueError):
        import asyncio

        asyncio.run(repo.set_domain_reader_exposure("  ", exposed=True))


@pytest.mark.asyncio
async def test_held_map_is_invisible_until_the_gate_opens():
    dsn = _postgres_dsn()
    import asyncpg

    schema = "reader_exposure_gate_test"
    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.execute(f'CREATE SCHEMA "{schema}"')
        await connection.execute(f'SET search_path TO "{schema}"')
        await connection.execute(SCHEMA_SQL)
        await connection.execute(
            """
            INSERT INTO articles (id, url, coverage_domain)
            VALUES ('crypto-1', 'https://example.com/1', 'crypto'),
                   ('core-1', 'https://example.com/2', 'core');
            INSERT INTO article_maps (article_id, status)
            VALUES ('crypto-1', 'held_degraded'), ('core-1', 'ok');
            """
        )
        visible = await connection.fetch(
            "SELECT article_id FROM article_maps"
            " WHERE status IN ('ok','degraded') ORDER BY article_id"
        )
        assert [row["article_id"] for row in visible] == ["core-1"]

        database = type("ConnectedDatabase", (), {"pool": connection})()
        repo = Repository(database)
        assert await repo.get_map_row("crypto-1") is None
        assert (await repo.get_map_row("core-1"))["status"] == "ok"

        await connection.execute(
            """
            UPDATE article_maps m
            SET status = substring(m.status FROM 6)
            FROM articles a
            WHERE a.id = m.article_id AND a.coverage_domain = 'crypto'
              AND m.status IN ('held_ok', 'held_degraded')
            """
        )
        assert (
            await connection.fetchval(
                "SELECT status FROM article_maps WHERE article_id='crypto-1'"
            )
            == "degraded"
        )
        assert (await repo.get_map_row("crypto-1"))["status"] == "degraded"
    finally:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.close()
