"""Daily Map budget: quota math, bucket claim, ledger and settings contract."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.db import SCHEMA_SQL
from app.repository import (
    MapBudget,
    Repository,
    bucket_quotas,
    map_budget_window,
    preferred_bucket,
)


TODAY = date(2026, 7, 25)
NOW = datetime(2026, 7, 25, 3, 0, tzinfo=UTC)


class _Transaction:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *_exc):
        return None


class BudgetConnection:
    """Fake connection that answers the budgeted claim's fixed query order."""

    def __init__(
        self,
        *,
        consumed=(),
        claims=(),
        settings_row=None,
        domain_rows=(),
        subtype_rows=(),
        now=NOW,
    ):
        # Ledger rows default to the core domain: that is what every row
        # written before the DR-13206 axis existed actually is.
        self.consumed = [
            {"coverage_domain": "core", **dict(row)} for row in consumed
        ]
        # ``None`` entries model a bucket attempt that finds no candidate.
        self.claims = [
            None if row is None else {"coverage_domain": "core", **dict(row)}
            for row in claims
        ]
        self.settings_row = settings_row
        self.domain_rows = list(domain_rows)
        self.subtype_rows = list(subtype_rows)
        self.now = now
        self.calls: list[tuple[str, str, tuple]] = []

    def transaction(self):
        return _Transaction()

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", query, args))
        return self.now

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        if "FROM map_budget_claims c" in query:
            return self.subtype_rows
        if "map_budget_domain_settings" in query:
            return self.domain_rows
        return self.consumed

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        if "map_budget_settings" in query:
            return self.settings_row
        return self.claims.pop(0) if self.claims else None


class _Acquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_exc):
        return None


class BudgetPool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return _Acquire(self.connection)


def _repo(pool) -> Repository:
    return Repository(SimpleNamespace(pool=pool))


def _claim_calls(connection) -> list[tuple[str, str, tuple]]:
    return [call for call in connection.calls if "WITH candidate AS" in call[1]]


def test_bucket_quotas_splits_limit_by_share():
    assert bucket_quotas(60, 75) == (45, 15)
    assert bucket_quotas(0, 75) == (0, 0)
    assert bucket_quotas(7, 75) == (5, 2)
    assert bucket_quotas(10, 0) == (0, 10)
    assert bucket_quotas(10, 100) == (10, 0)


def test_bucket_quotas_rejects_invalid_bounds():
    with pytest.raises(ValueError):
        bucket_quotas(-1, 75)
    with pytest.raises(ValueError):
        bucket_quotas(10, 101)


def test_preferred_bucket_balances_fill_ratio():
    assert preferred_bucket(45, 0, 15, 0) == "fresh"
    assert preferred_bucket(45, 30, 15, 3) == "backlog"
    assert preferred_bucket(45, 45, 15, 3) == "backlog"
    assert preferred_bucket(45, 10, 15, 15) == "fresh"
    assert preferred_bucket(45, 45, 15, 15) is None
    # A zero quota never wins, even against an untouched partner.
    assert preferred_bucket(0, 0, 15, 4) == "backlog"


def test_map_budget_accepts_new_maximum_and_rejects_out_of_range_values():
    assert MapBudget(daily_limit=1000, fresh_share_percent=75).daily_limit == 1000
    with pytest.raises(ValueError, match="daily_limit"):
        MapBudget(daily_limit=1001, fresh_share_percent=75)
    with pytest.raises(ValueError, match="fresh_share_percent"):
        MapBudget(daily_limit=60, fresh_share_percent=101)
    with pytest.raises(ValueError, match="daily_run_count"):
        MapBudget(daily_limit=60, fresh_share_percent=75, daily_run_count=0)
    with pytest.raises(ValueError, match="daily_run_count"):
        MapBudget(daily_limit=60, fresh_share_percent=75, daily_run_count=25)


def test_map_budget_window_single_run_opens_full_budget_at_midnight():
    window = map_budget_window(
        MapBudget(1000, 75, 1),
        datetime(2026, 7, 31, 15, 0, tzinfo=UTC),
    )

    assert window.editorial_day == date(2026, 8, 1)
    assert window.current_run == 1
    assert window.current_run_allowance == 1000
    assert window.current_allowance == 1000
    assert window.next_run_at is None


def test_map_budget_window_five_equal_runs_and_exact_boundaries():
    budget = MapBudget(1000, 75, 5)
    midnight_utc = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)

    expected = [
        (0, 1, 200),
        (17_280, 2, 400),
        (34_560, 3, 600),
        (51_840, 4, 800),
        (69_120, 5, 1000),
    ]
    for seconds, current_run, allowance in expected:
        before = map_budget_window(
            budget,
            midnight_utc + timedelta(seconds=seconds, microseconds=-1)
            if seconds
            else midnight_utc,
        )
        at_boundary = map_budget_window(
            budget, midnight_utc + timedelta(seconds=seconds)
        )
        if seconds:
            assert before.current_run == current_run - 1
        assert at_boundary.current_run == current_run
        assert at_boundary.current_run_allowance == 200
        assert at_boundary.current_allowance == allowance


def test_map_budget_window_non_divisible_remainder_lands_in_last_run():
    budget = MapBudget(1000, 75, 3)
    midnight_utc = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)
    windows = [
        map_budget_window(budget, midnight_utc),
        map_budget_window(budget, midnight_utc + timedelta(hours=8)),
        map_budget_window(budget, midnight_utc + timedelta(hours=16)),
    ]

    assert [item.current_run_allowance for item in windows] == [333, 333, 334]
    assert [item.current_allowance for item in windows] == [333, 666, 1000]
    assert windows[0].next_run_at.isoformat() == "2026-08-01T08:00:00+09:00"
    assert windows[2].next_run_at is None


def test_map_budget_window_zero_limit_and_naive_clock():
    window = map_budget_window(
        MapBudget(0, 75, 24),
        datetime(2026, 8, 1, 3, 0, tzinfo=UTC),
    )
    assert window.current_allowance == 0
    assert window.current_run_allowance == 0

    with pytest.raises(ValueError, match="timezone-aware"):
        map_budget_window(MapBudget(60, 75, 1), datetime(2026, 8, 1))


def test_map_budget_window_seven_run_fractional_boundary_rounds_up_to_microsecond():
    budget = MapBudget(700, 75, 7)
    midnight_utc = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)
    first_boundary_us = (86_400_000_000 + 6) // 7

    before = map_budget_window(
        budget,
        midnight_utc + timedelta(microseconds=first_boundary_us - 1),
    )
    at_boundary = map_budget_window(
        budget,
        midnight_utc + timedelta(microseconds=first_boundary_us),
    )

    assert before.current_run == 1
    assert at_boundary.current_run == 2
    assert at_boundary.current_allowance == 200


def test_schema_migrates_existing_budget_limit_constraint():
    assert "daily_map_limit BETWEEN 0 AND 1000" in SCHEMA_SQL
    assert "map_budget_settings_daily_map_limit_check" in SCHEMA_SQL
    assert "DROP CONSTRAINT map_budget_settings_daily_map_limit_check" in SCHEMA_SQL
    assert "ADD CONSTRAINT map_budget_settings_daily_map_limit_check" in SCHEMA_SQL
    assert "CREATE TABLE IF NOT EXISTS map_budget_claims" in SCHEMA_SQL
    assert "idx_map_budget_claims_reserved_article" in SCHEMA_SQL
    assert "daily_run_count INTEGER NOT NULL DEFAULT 1" in SCHEMA_SQL
    assert "map_budget_settings_daily_run_count_check" in SCHEMA_SQL
    assert "previous_run_count INTEGER" in SCHEMA_SQL


@pytest.mark.asyncio
async def test_claim_without_budget_keeps_the_existing_fifo_statement():
    connection = BudgetConnection(claims=[{"article_id": "oldest"}])
    pool = BudgetPool(connection)

    claimed = await _repo(pool).queue_claim_next(lease_seconds=1800, max_active=3)

    assert claimed == "oldest"
    assert len(connection.calls) == 2
    _kind, query, args = connection.calls[1]
    assert "ORDER BY a.first_seen_at ASC" in query
    assert "map_budget_ledger" not in query
    assert args == (1800, 3, None)


@pytest.mark.asyncio
async def test_budgeted_claim_records_the_optional_lease_owner():
    connection = BudgetConnection(
        claims=[{"article_id": "article-1", "bucket": "fresh"}],
    )

    claimed = await _repo(BudgetPool(connection)).queue_claim_next(
        lease_seconds=1800,
        max_active=3,
        lease_owner="worker-1",
        budget=MapBudget(daily_limit=60, fresh_share_percent=75),
    )

    assert claimed == "article-1"
    _kind, query, args = _claim_calls(connection)[0]
    assert "lease_owner = $8" in query
    # $9 is the coverage-domain filter added by DR-13206; the owner stays $8.
    assert args[7] == "worker-1"
    assert args[8] is None


@pytest.mark.asyncio
async def test_budgeted_claim_charges_the_bucket_it_came_from():
    connection = BudgetConnection(
        # fresh 4/45 is a lower fill ratio than backlog 2/15, so fresh is next.
        consumed=[
            {"bucket": "fresh", "consumed": 4},
            {"bucket": "backlog", "consumed": 2},
        ],
        claims=[{"article_id": "article-1", "bucket": "fresh"}],
    )
    pool = BudgetPool(connection)

    claimed = await _repo(pool).queue_claim_next(
        lease_seconds=1800,
        max_active=3,
        budget=MapBudget(daily_limit=60, fresh_share_percent=75),
    )

    assert claimed == "article-1"
    claims = _claim_calls(connection)
    assert len(claims) == 1
    assert claims[0][2][2] == "fresh"
    assert claims[0][2][3] == TODAY
    ledger = [call for call in connection.calls if "map_budget_ledger" in call[1]]
    increment = [call for call in ledger if call[0] == "execute"]
    assert len(increment) == 1
    assert increment[0][2] == (TODAY, "core", "fresh")
    assert "consumed = map_budget_ledger.consumed + 1" in increment[0][1]
    reservations = [
        call
        for call in connection.calls
        if call[0] == "execute"
        and "INSERT INTO map_budget_claims" in call[1]
    ]
    assert len(reservations) == 1
    assert reservations[0][2] == ("article-1", TODAY, "core", "fresh")


@pytest.mark.asyncio
async def test_budgeted_claim_returns_none_when_the_day_is_spent():
    connection = BudgetConnection(
        consumed=[
            {"bucket": "fresh", "consumed": 45},
            {"bucket": "backlog", "consumed": 15},
        ],
        claims=[{"article_id": "never", "bucket": "fresh"}],
    )
    pool = BudgetPool(connection)

    claimed = await _repo(pool).queue_claim_next(
        lease_seconds=1800,
        max_active=3,
        budget=MapBudget(daily_limit=60, fresh_share_percent=75),
    )

    assert claimed is None
    assert _claim_calls(connection) == []
    assert not [
        call
        for call in connection.calls
        if call[0] == "execute" and "map_budget_ledger" in call[1]
    ]


@pytest.mark.asyncio
async def test_budgeted_claim_waits_for_next_run_without_ledger_write():
    seoul_midnight = datetime(2026, 7, 24, 15, 0, tzinfo=UTC)
    connection = BudgetConnection(
        consumed=[{"bucket": "fresh", "consumed": 150}, {"bucket": "backlog", "consumed": 50}],
        claims=[{"article_id": "future", "bucket": "fresh"}],
        now=seoul_midnight,
    )

    claimed = await _repo(BudgetPool(connection)).queue_claim_next(
        lease_seconds=1800,
        max_active=3,
        budget=MapBudget(1000, 75, 5),
    )

    assert claimed is None
    assert _claim_calls(connection) == []
    assert any(
        call[0] == "fetchval" and "clock_timestamp()" in call[1]
        for call in connection.calls
    )
    assert not any(
        call[0] == "execute" and "map_budget_ledger" in call[1]
        for call in connection.calls
    )


@pytest.mark.asyncio
async def test_budgeted_claim_reopens_at_next_run_and_carries_unused_allowance():
    second_run = datetime(2026, 7, 24, 19, 48, tzinfo=UTC)
    connection = BudgetConnection(
        consumed=[{"bucket": "fresh", "consumed": 100}, {"bucket": "backlog", "consumed": 50}],
        claims=[{"article_id": "carried", "bucket": "fresh"}],
        now=second_run,
    )

    claimed = await _repo(BudgetPool(connection)).queue_claim_next(
        lease_seconds=1800,
        max_active=3,
        budget=MapBudget(1000, 75, 5),
    )

    assert claimed == "carried"
    assert len(_claim_calls(connection)) == 1
    ledger_insert = next(
        call
        for call in connection.calls
        if call[0] == "execute" and "INSERT INTO map_budget_ledger" in call[1]
    )
    assert ledger_insert[2][0] == TODAY


@pytest.mark.asyncio
async def test_budgeted_claim_live_run_count_change_pauses_without_refund():
    connection = BudgetConnection(
        consumed=[{"bucket": "fresh", "consumed": 200}],
        claims=[{"article_id": "paused", "bucket": "fresh"}],
        settings_row={
            "daily_map_limit": 1000,
            "fresh_share_percent": 75,
            "daily_run_count": 5,
        },
        now=datetime(2026, 7, 24, 15, 0, tzinfo=UTC),
    )

    claimed = await _repo(BudgetPool(connection)).queue_claim_next(
        lease_seconds=1800,
        max_active=3,
        budget=MapBudget(1000, 75, 1),
    )

    assert claimed is None
    assert _claim_calls(connection) == []
    assert not any(
        call[0] == "execute" and "UPDATE map_budget_ledger" in call[1]
        for call in connection.calls
    )


@pytest.mark.asyncio
async def test_budgeted_claim_spills_to_the_other_bucket_when_preferred_is_empty():
    connection = BudgetConnection(
        consumed=[],
        claims=[None, {"article_id": "old-one", "bucket": "backlog"}],
    )
    pool = BudgetPool(connection)

    claimed = await _repo(pool).queue_claim_next(
        lease_seconds=1800,
        max_active=3,
        budget=MapBudget(daily_limit=60, fresh_share_percent=75),
    )

    assert claimed == "old-one"
    buckets = [call[2][2] for call in _claim_calls(connection)]
    assert buckets == ["fresh", "backlog"]
    increment = [
        call
        for call in connection.calls
        if call[0] == "execute" and "map_budget_ledger" in call[1]
    ]
    assert increment[0][2] == (TODAY, "core", "backlog")


@pytest.mark.asyncio
async def test_budgeted_claim_prefers_the_live_settings_row_over_env_defaults():
    connection = BudgetConnection(
        consumed=[{"bucket": "fresh", "consumed": 4}],
        claims=[{"article_id": "article-1", "bucket": "fresh"}],
        settings_row={
            "daily_map_limit": 4,
            "fresh_share_percent": 100,
            "daily_run_count": 1,
        },
    )
    pool = BudgetPool(connection)

    claimed = await _repo(pool).queue_claim_next(
        lease_seconds=1800,
        max_active=3,
        budget=MapBudget(daily_limit=60, fresh_share_percent=75),
    )

    assert claimed is None
    assert _claim_calls(connection) == []


@pytest.mark.asyncio
async def test_ranked_claim_orders_by_score_and_admits_stale_unscored_rows():
    connection = BudgetConnection(
        consumed=[],
        claims=[{"article_id": "best", "bucket": "fresh"}],
    )
    pool = BudgetPool(connection)

    claimed = await _repo(pool).queue_claim_next(
        lease_seconds=1800,
        max_active=3,
        budget=MapBudget(daily_limit=60, fresh_share_percent=75),
        ranked=True,
        stale_after_s=21600.0,
        neutral_score=50,
    )

    assert claimed == "best"
    _kind, query, args = _claim_calls(connection)[0]
    assert "COALESCE(q.triage_score, $7::int)" in query
    assert "q.triage_scored_at IS NOT NULL" in query
    assert args[4] is True
    assert args[5] == 21600.0
    assert args[6] == 50


@pytest.mark.asyncio
async def test_budgeted_claim_rejects_invalid_ranking_bounds():
    repo = _repo(BudgetPool(BudgetConnection()))
    budget = MapBudget(daily_limit=60, fresh_share_percent=75)

    with pytest.raises(ValueError, match="stale_after_s"):
        await repo.queue_claim_next(
            lease_seconds=1800, max_active=3, budget=budget, stale_after_s=0
        )
    with pytest.raises(ValueError, match="neutral_score"):
        await repo.queue_claim_next(
            lease_seconds=1800, max_active=3, budget=budget, neutral_score=101
        )


class ClaimLifecycleConnection:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls: list[tuple[str, str, tuple]] = []

    def transaction(self):
        return _Transaction()

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        return self.rows.pop(0) if self.rows else None

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        return "UPDATE 1"


@pytest.mark.asyncio
async def test_commit_budget_claim_is_idempotent_without_ledger_change():
    connection = ClaimLifecycleConnection([{"id": 17}])
    repo = _repo(BudgetPool(connection))

    assert await repo.commit_map_budget_claim("article-1") is True
    assert any(
        call[0] == "execute"
        and "status='committed'" in call[1]
        and call[2] == (17,)
        for call in connection.calls
    )
    assert not any("map_budget_ledger" in call[1] for call in connection.calls)

    empty = ClaimLifecycleConnection([None])
    assert await _repo(BudgetPool(empty)).commit_map_budget_claim("article-1") is False


@pytest.mark.asyncio
async def test_release_budget_claim_refunds_exact_bucket_once():
    connection = ClaimLifecycleConnection(
        [
            {"id": 23, "editorial_day": TODAY, "coverage_domain": "core",
             "bucket": "backlog"},
            {"consumed": 4},
        ]
    )
    repo = _repo(BudgetPool(connection))

    assert await repo.release_map_budget_claim(
        "article-1",
        reason="source_too_short",
    ) is True
    ledger_update = next(
        call for call in connection.calls if "UPDATE map_budget_ledger" in call[1]
    )
    assert ledger_update[2] == (TODAY, "core", "backlog")
    assert "consumed=consumed - 1" in ledger_update[1]
    release_update = next(
        call
        for call in connection.calls
        if "status='released'" in call[1]
    )
    assert release_update[2] == (23, "source_too_short")

    empty = ClaimLifecycleConnection([None])
    assert await _repo(BudgetPool(empty)).release_map_budget_claim(
        "article-1",
        reason="duplicate",
    ) is False


class SettingsPool:
    def __init__(self, *, row=None, rows=(), value=None, domain_rows=()):
        self.row = row
        # Ledger rows default to the core domain, which is what every row
        # written before the DR-13206 axis existed actually is.
        self.rows = [{"coverage_domain": "core", **dict(item)} for item in rows]
        self.domain_rows = list(domain_rows)
        self.value = value
        self.calls: list[tuple[str, str, tuple]] = []
        self.connection = None

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        return self.row

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        if "map_budget_domain_settings" in query:
            return self.domain_rows
        return self.rows

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", query, args))
        return self.value


@pytest.mark.asyncio
async def test_settings_fall_back_to_env_defaults_when_no_row_exists():
    pool = SettingsPool(row=None)

    settings = await _repo(pool).map_budget_settings(
        default_limit=60, default_share=75, default_run_count=1
    )

    assert settings == {
        "daily_map_limit": 60,
        "fresh_share_percent": 75,
        "daily_run_count": 1,
        "updated_at": None,
        "source": "default",
    }


@pytest.mark.asyncio
async def test_settings_reject_invalid_env_defaults():
    with pytest.raises(ValueError):
        await _repo(SettingsPool(row=None)).map_budget_settings(
            default_limit=1001, default_share=75, default_run_count=1
        )
    with pytest.raises(ValueError):
        await _repo(SettingsPool(row=None)).map_budget_settings(
            default_limit=60, default_share=75, default_run_count=25
        )


class StatusPool(SettingsPool):
    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        if "map_queue" in query:
            return {"scored": 12, "pending": 3}
        return self.row


@pytest.mark.asyncio
async def test_status_reports_quotas_consumption_and_coverage():
    pool = StatusPool(
        row={
            "daily_map_limit": 60,
            "fresh_share_percent": 75,
            "daily_run_count": 4,
            "updated_at": NOW,
        },
        rows=[{"bucket": "fresh", "consumed": 10}],
        domain_rows=[
            {
                "coverage_domain": "core",
                "daily_map_limit": 40,
                "document_map_limit": 0,
                "reader_exposed": True,
                "quarantined": 0,
                "held_maps": 0,
                "article_consumed": 10,
                "document_consumed": 0,
            },
            {
                "coverage_domain": "crypto",
                "daily_map_limit": 20,
                "document_map_limit": 7,
                "reader_exposed": False,
                "quarantined": 9,
                "held_maps": 0,
                "article_consumed": 0,
                "document_consumed": 0,
            },
        ],
        value=NOW,
    )

    status = await _repo(pool).map_budget_status(
        default_limit=1, default_share=1, default_run_count=1
    )

    assert status["daily_map_limit"] == 60
    assert status["source"] == "db"
    assert status["editorial_day"] == TODAY
    assert status["daily_run_count"] == 4
    assert status["current_run"] == 3
    assert status["current_run_allowance"] == 15
    assert status["current_allowance"] == 45
    assert status["current_remaining"] == 35
    assert status["next_run_at"].isoformat() == "2026-07-25T18:00:00+09:00"
    assert status["buckets"]["fresh"] == {
        "quota": 45,
        "consumed": 10,
        "remaining": 35,
    }
    assert status["buckets"]["backlog"] == {
        "quota": 15,
        "consumed": 0,
        "remaining": 15,
    }
    assert status["total_consumed"] == 10
    assert status["total_remaining"] == 50
    assert status["triage"] == {"scored": 12, "pending": 3}
    # The per-domain block reports each share separately; the global numbers
    # above stay the sum, so an existing /ops client is unaffected.
    core, crypto = status["domains"]
    assert core["coverage_domain"] == "core"
    assert core["daily_map_limit"] == 40
    assert core["article_map_limit"] == 40
    assert core["document_map_limit"] == 0
    assert core["reader_exposed"] is True
    assert core["consumed"] == {"fresh": 10, "backlog": 0}
    assert core["current_allowance"] == 30
    assert core["current_remaining"] == 20
    assert core["total_remaining"] == 30
    assert crypto["coverage_domain"] == "crypto"
    assert crypto["article_map_limit"] == 13
    assert crypto["document_map_limit"] == 7
    assert crypto["subtypes"] == {
        "article": {"consumed": 0, "remaining": 13},
        "document": {"consumed": 0, "remaining": 7},
    }
    assert crypto["reader_exposed"] is False
    assert crypto["total_consumed"] == 0
    # Collected but never mapped: the DR-13208 quarantine, not the reader gate.
    assert crypto["quarantined"] == 9
    assert crypto["held_maps"] == 0


class ChangeConnection:
    def __init__(self, previous):
        self.previous = previous
        self.calls: list[tuple[str, str, tuple]] = []

    def transaction(self):
        return _Transaction()

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        if "FOR UPDATE" in query:
            return self.previous
        return {
            "daily_map_limit": args[0],
            "fresh_share_percent": args[1],
            "daily_run_count": args[2],
            "updated_at": NOW,
        }


@pytest.mark.asyncio
async def test_set_budget_persists_and_appends_history():
    connection = ChangeConnection(
        {"daily_map_limit": 60, "fresh_share_percent": 75, "daily_run_count": 1}
    )
    pool = BudgetPool(connection)

    saved = await _repo(pool).set_map_budget(
        daily_map_limit=40, fresh_share_percent=60, daily_run_count=5
    )

    assert saved["daily_map_limit"] == 40
    assert saved["fresh_share_percent"] == 60
    assert saved["daily_run_count"] == 5
    assert saved["source"] == "db"
    history = [
        call for call in connection.calls if "map_budget_changes" in call[1]
    ]
    assert len(history) == 1
    assert history[0][2] == (60, 75, 1, 40, 60, 5)


@pytest.mark.asyncio
async def test_set_budget_writes_document_limit_and_preserves_omission():
    connection = ChangeConnection(
        {"daily_map_limit": 60, "fresh_share_percent": 75, "daily_run_count": 1}
    )

    await _repo(BudgetPool(connection)).set_map_budget(
        daily_map_limit=60,
        fresh_share_percent=75,
        daily_run_count=1,
        domains=[("core", 40, None), ("crypto", 20, 7)],
    )

    domain_writes = [
        call
        for call in connection.calls
        if call[0] == "execute" and "map_budget_domain_settings" in call[1]
    ]
    assert [call[2] for call in domain_writes] == [
        ("core", 40, None),
        ("crypto", 20, 7),
    ]
    normalized = " ".join(domain_writes[0][1].split())
    assert "WHEN $3::integer IS NULL" in normalized
    assert "THEN map_budget_domain_settings.document_map_limit" in normalized


@pytest.mark.asyncio
async def test_set_budget_rejects_out_of_range_before_touching_the_database():
    connection = ChangeConnection(None)
    pool = BudgetPool(connection)

    with pytest.raises(ValueError):
        await _repo(pool).set_map_budget(
            daily_map_limit=1001, fresh_share_percent=75, daily_run_count=1
        )

    assert connection.calls == []
