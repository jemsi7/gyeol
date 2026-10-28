"""Per-domain Map budget claim (I-13200-d, DR-13200/DR-13206).

The claim now walks one domain at a time. Each domain carries its own limit,
its own bucket preference and its own ledger rows, and unused allowance never
crosses the boundary -- that non-transfer is the whole point of DR-13200, so
most of these tests are about what must *not* happen.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from app.repository import (
    DomainBudget,
    MapBudget,
    Repository,
    order_domains_by_fill,
)

from tests.test_map_budget import BudgetConnection, BudgetPool


TODAY = date(2026, 7, 25)
NOW = datetime(2026, 7, 25, 3, 0, tzinfo=UTC)
CORE_40_CRYPTO_20 = (
    {
        "coverage_domain": "core",
        "daily_map_limit": 40,
        "document_map_limit": 0,
    },
    {
        "coverage_domain": "crypto",
        "daily_map_limit": 20,
        "document_map_limit": 7,
    },
)


def _repo(pool) -> Repository:
    return Repository(SimpleNamespace(pool=pool))


def _claim_calls(connection):
    return [call for call in connection.calls if "WITH candidate AS" in call[1]]


def _domains_attempted(connection) -> list[str | None]:
    seen: list[str | None] = []
    for call in _claim_calls(connection):
        domain = call[2][8]
        if domain not in seen:
            seen.append(domain)
    return seen


def _ledger_writes(connection):
    return [
        call
        for call in connection.calls
        if call[0] == "execute" and "INSERT INTO map_budget_ledger" in call[1]
    ]


async def _claim(connection, *, limit=60, share=75):
    return await _repo(BudgetPool(connection)).queue_claim_next(
        lease_seconds=1800,
        max_active=3,
        budget=MapBudget(daily_limit=limit, fresh_share_percent=share),
    )


@pytest.mark.asyncio
async def test_explicit_core_filter_never_attempts_crypto_domain():
    connection = BudgetConnection(
        domain_rows=CORE_40_CRYPTO_20,
        # BudgetConnection is a query-order fake rather than a SQL engine, so
        # the returned row models the candidate left after PostgreSQL applies
        # the explicit ``coverage_domain = core`` predicate.
        claims=[{"article_id": "a-1", "coverage_domain": "core", "bucket": "fresh"}],
    )

    claimed = await _repo(BudgetPool(connection)).queue_claim_next(
        lease_seconds=1800,
        max_active=3,
        budget=MapBudget(daily_limit=60, fresh_share_percent=75),
        coverage_domain="core",
    )

    assert claimed == "a-1"
    assert _domains_attempted(connection) == ["core"]


@pytest.mark.asyncio
async def test_unknown_explicit_domain_returns_no_claim():
    connection = BudgetConnection(
        domain_rows=CORE_40_CRYPTO_20,
        claims=[{"article_id": "a-1", "coverage_domain": "core", "bucket": "fresh"}],
    )

    claimed = await _repo(BudgetPool(connection)).queue_claim_next(
        lease_seconds=1800,
        max_active=3,
        budget=MapBudget(daily_limit=60, fresh_share_percent=75),
        coverage_domain="missing",
    )

    assert claimed is None
    assert _domains_attempted(connection) == []


def test_order_domains_by_fill_interleaves_and_drops_zero_limits():
    domains = [
        DomainBudget("core", 40),
        DomainBudget("crypto", 20),
        DomainBudget("paused", 0),
    ]
    # core 20/40 = 0.50 is fuller than crypto 4/20 = 0.20, so crypto goes first.
    ordered = order_domains_by_fill(domains, {"core": 20, "crypto": 4})
    assert [item.coverage_domain for item in ordered] == ["crypto", "core"]
    # A zero limit is a hold, not a tie: it must never be offered a claim.
    assert all(item.daily_limit > 0 for item in ordered)


def test_order_domains_by_fill_breaks_ties_by_name():
    domains = [DomainBudget("crypto", 20), DomainBudget("core", 20)]
    ordered = order_domains_by_fill(domains, {})
    assert [item.coverage_domain for item in ordered] == ["core", "crypto"]


@pytest.mark.asyncio
async def test_claim_charges_the_domain_the_article_came_from():
    connection = BudgetConnection(
        domain_rows=CORE_40_CRYPTO_20,
        claims=[{"article_id": "c-1", "coverage_domain": "crypto", "bucket": "fresh"}],
    )

    assert await _claim(connection) == "c-1"
    writes = _ledger_writes(connection)
    assert len(writes) == 1
    assert writes[0][2] == (TODAY, "crypto", "fresh")
    reservations = [
        call
        for call in connection.calls
        if call[0] == "execute" and "INSERT INTO map_budget_claims" in call[1]
    ]
    assert reservations[0][2] == ("c-1", TODAY, "crypto", "fresh")


@pytest.mark.asyncio
async def test_exhausted_domain_does_not_block_the_other():
    connection = BudgetConnection(
        domain_rows=CORE_40_CRYPTO_20,
        consumed=[
            {"coverage_domain": "crypto", "bucket": "fresh", "consumed": 15},
            {"coverage_domain": "crypto", "bucket": "backlog", "consumed": 5},
        ],
        claims=[{"article_id": "a-1", "coverage_domain": "core", "bucket": "fresh"}],
    )

    assert await _claim(connection) == "a-1"
    # crypto is spent, so it is never even offered a candidate query.
    assert _domains_attempted(connection) == ["core"]


@pytest.mark.asyncio
async def test_unused_allowance_does_not_carry_over():
    connection = BudgetConnection(
        domain_rows=CORE_40_CRYPTO_20,
        consumed=[
            {"coverage_domain": "crypto", "bucket": "fresh", "consumed": 15},
            {"coverage_domain": "crypto", "bucket": "backlog", "consumed": 5},
        ],
        # core has 40 unspent, but there is no core candidate to spend it on.
        claims=[None, None, None],
    )

    assert await _claim(connection) is None
    assert _domains_attempted(connection) == ["core"]
    assert _ledger_writes(connection) == []


@pytest.mark.asyncio
async def test_domains_are_offered_least_filled_first():
    connection = BudgetConnection(
        domain_rows=CORE_40_CRYPTO_20,
        consumed=[{"coverage_domain": "core", "bucket": "fresh", "consumed": 20}],
        claims=[{"article_id": "c-1", "coverage_domain": "crypto", "bucket": "fresh"}],
    )

    assert await _claim(connection) == "c-1"
    assert _domains_attempted(connection)[0] == "crypto"


@pytest.mark.asyncio
async def test_zero_limit_domain_never_claims():
    connection = BudgetConnection(
        domain_rows=(
            {
                "coverage_domain": "core",
                "daily_map_limit": 0,
                "document_map_limit": 0,
            },
            {
                "coverage_domain": "crypto",
                "daily_map_limit": 0,
                "document_map_limit": 0,
            },
        ),
        claims=[{"article_id": "c-1", "coverage_domain": "crypto", "bucket": "fresh"}],
    )

    assert await _claim(connection) is None
    assert _claim_calls(connection) == []
    assert _ledger_writes(connection) == []


@pytest.mark.asyncio
async def test_missing_domain_settings_falls_back_to_the_shared_allowance():
    connection = BudgetConnection(
        domain_rows=(),
        consumed=[{"coverage_domain": "core", "bucket": "fresh", "consumed": 4}],
        claims=[{"article_id": "a-1", "coverage_domain": "core", "bucket": "fresh"}],
    )

    assert await _claim(connection) == "a-1"
    # A NULL filter is the legacy path: one shared allowance, no domain axis.
    assert _domains_attempted(connection) == [None]


@pytest.mark.asyncio
async def test_bucket_preference_is_computed_per_domain():
    connection = BudgetConnection(
        domain_rows=CORE_40_CRYPTO_20,
        consumed=[
            # crypto has spent its whole fresh share (15 of 20) but is still
            # less filled overall (0.75) than core (36/40 = 0.90), so crypto is
            # offered first and must fall to its own backlog bucket.
            {"coverage_domain": "crypto", "bucket": "fresh", "consumed": 15},
            {"coverage_domain": "core", "bucket": "fresh", "consumed": 30},
            {"coverage_domain": "core", "bucket": "backlog", "consumed": 6},
        ],
        claims=[
            {"article_id": "c-1", "coverage_domain": "crypto", "bucket": "backlog"}
        ],
    )

    assert await _claim(connection) == "c-1"
    first_call = _claim_calls(connection)[0]
    assert first_call[2][8] == "crypto"
    # crypto's own fresh quota is full, so backlog is its preferred bucket.
    assert first_call[2][2] == "backlog"


@pytest.mark.asyncio
async def test_spill_stays_inside_one_domain():
    connection = BudgetConnection(
        domain_rows=CORE_40_CRYPTO_20,
        # core has spent some of its share, so crypto (0.00) is offered first.
        consumed=[{"coverage_domain": "core", "bucket": "fresh", "consumed": 10}],
        # crypto: fresh miss, backlog miss, unfiltered-bucket miss, then core.
        claims=[None, None, None, {"article_id": "a-1", "coverage_domain": "core",
                                   "bucket": "fresh"}],
    )

    assert await _claim(connection) == "a-1"
    calls = _claim_calls(connection)
    crypto_calls = [call for call in calls if call[2][8] == "crypto"]
    assert [call[2][2] for call in crypto_calls] == ["fresh", "backlog", None]
    assert _ledger_writes(connection)[0][2] == (TODAY, "core", "fresh")


CRYPTO_13_7 = (
    {
        "coverage_domain": "crypto",
        "daily_map_limit": 20,
        "document_map_limit": 7,
    },
)


@pytest.mark.asyncio
async def test_article_cap_forces_document_candidate():
    connection = BudgetConnection(
        domain_rows=CRYPTO_13_7,
        subtype_rows=[
            {
                "coverage_domain": "crypto",
                "article_consumed": 13,
                "document_consumed": 2,
            }
        ],
        claims=[
            {"article_id": "doc-1", "coverage_domain": "crypto", "bucket": "fresh"}
        ],
    )

    assert await _claim(connection) == "doc-1"
    assert _claim_calls(connection)[0][2][9] is True


@pytest.mark.asyncio
async def test_document_cap_forces_article_candidate():
    connection = BudgetConnection(
        domain_rows=CRYPTO_13_7,
        subtype_rows=[
            {
                "coverage_domain": "crypto",
                "article_consumed": 4,
                "document_consumed": 7,
            }
        ],
        claims=[
            {"article_id": "article-1", "coverage_domain": "crypto", "bucket": "fresh"}
        ],
    )

    assert await _claim(connection) == "article-1"
    assert _claim_calls(connection)[0][2][9] is False


@pytest.mark.asyncio
async def test_both_subtype_caps_skip_crypto_without_spill():
    connection = BudgetConnection(
        domain_rows=CRYPTO_13_7,
        subtype_rows=[
            {
                "coverage_domain": "crypto",
                "article_consumed": 13,
                "document_consumed": 7,
            }
        ],
    )

    assert await _claim(connection) is None
    assert _claim_calls(connection) == []
    assert _ledger_writes(connection) == []


@pytest.mark.asyncio
async def test_open_subtypes_keep_fifo_and_active_claim_query_excludes_released():
    connection = BudgetConnection(
        domain_rows=CRYPTO_13_7,
        subtype_rows=[
            {
                "coverage_domain": "crypto",
                "article_consumed": 1,
                "document_consumed": 1,
            }
        ],
        claims=[
            {"article_id": "either-1", "coverage_domain": "crypto", "bucket": "fresh"}
        ],
    )

    assert await _claim(connection) == "either-1"
    assert _claim_calls(connection)[0][2][9] is None
    subtype_query = next(
        call[1]
        for call in connection.calls
        if call[0] == "fetch" and "FROM map_budget_claims c" in call[1]
    )
    assert "status IN ('reserved', 'committed')" in subtype_query
    assert "released" not in subtype_query


def test_document_limit_is_clamped_inside_domain_limit():
    budget = DomainBudget("crypto", 5, document_map_limit=7)

    assert budget.effective_document_map_limit == 5
    assert budget.article_map_limit == 0
