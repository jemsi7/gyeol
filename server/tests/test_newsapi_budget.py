"""Rolling NewsAPI request-budget reservation contracts."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.config import NEWSAPI_MAX_ATTEMPTS, Settings
from app.repository import Repository


class _Transaction:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        self.connection.events.append("transaction:enter")
        return self

    async def __aexit__(self, *_exc):
        self.connection.events.append("transaction:exit")


class _Connection:
    def __init__(self, recent: int, run_id: int = 42):
        self.recent = recent
        self.run_id = run_id
        self.events: list[str] = []
        self.calls: list[tuple[str, str, tuple]] = []

    def transaction(self):
        return _Transaction(self)

    async def execute(self, query, *args):
        self.events.append("lock")
        self.calls.append(("execute", query, args))

    async def fetchval(self, query, *args):
        if "SELECT count(*)" in query:
            self.events.append("count")
            value = self.recent
        else:
            self.events.append("insert")
            value = self.run_id
        self.calls.append(("fetchval", query, args))
        return value


class _Acquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_exc):
        return None


class _Pool:
    def __init__(self, recent: int):
        self.connection = _Connection(recent)

    def acquire(self):
        return _Acquire(self.connection)


def _repo(recent: int) -> tuple[Repository, _Pool]:
    pool = _Pool(recent)
    return Repository(SimpleNamespace(pool=pool)), pool


@pytest.mark.asyncio
async def test_newsapi_budget_allows_224th_recent_count_and_reserves_run():
    repo, pool = _repo(224)

    run_id = await repo.start_bounded_load_run(
        "newsapi:ai-models-agents",
        "AI",
        provider_prefix="newsapi:",
        max_runs=225,
    )

    assert run_id == 42
    assert pool.connection.events == [
        "transaction:enter",
        "lock",
        "count",
        "insert",
        "transaction:exit",
    ]
    lock = pool.connection.calls[0]
    assert "pg_advisory_xact_lock" in lock[1]
    assert lock[2] == ("source-api-budget:newsapi:",)
    count = pool.connection.calls[1]
    assert "job_name LIKE $1" in count[1]
    assert "started_at > now()" in count[1]
    assert count[2] == ("newsapi:%", 24.0)
    insert = pool.connection.calls[2]
    assert "INSERT INTO load_runs" in insert[1]
    assert insert[2] == ("newsapi:ai-models-agents", "AI")


@pytest.mark.asyncio
async def test_newsapi_budget_denies_225th_recent_count_without_insert():
    repo, pool = _repo(225)

    run_id = await repo.start_bounded_load_run(
        "newsapi:ai-models-agents",
        "AI",
        provider_prefix="newsapi:",
        max_runs=225,
    )

    assert run_id is None
    assert pool.connection.events == [
        "transaction:enter",
        "lock",
        "count",
        "transaction:exit",
    ]
    assert all("INSERT INTO load_runs" not in call[1] for call in pool.connection.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "values",
    (
        {"job_name": "gdelt:AI"},
        {"provider_prefix": ""},
        {"provider_prefix": "newsapi:%"},
        {"max_runs": 0},
        {"window_hours": 0},
    ),
)
async def test_bounded_run_rejects_invalid_budget_inputs_before_db(values):
    repo, pool = _repo(0)
    arguments = {
        "job_name": "newsapi:test",
        "query": "AI",
        "provider_prefix": "newsapi:",
        "max_runs": 225,
        "window_hours": 24.0,
    }
    arguments.update(values)

    with pytest.raises(ValueError):
        await repo.start_bounded_load_run(**arguments)

    assert pool.connection.calls == []


@pytest.mark.parametrize(
    ("budget", "logical_runs"),
    ((4, 1), (899, 224), (900, 225)),
)
def test_budget_helper_reserves_worst_case_attempts(budget, logical_runs):
    settings = Settings(
        _env_file=None,
        NEWSAPI_REQUEST_BUDGET_24H=budget,
    )

    assert settings.newsapi_logical_run_limit() == logical_runs
    assert logical_runs * NEWSAPI_MAX_ATTEMPTS <= budget


def test_normal_schedule_and_hard_ceiling_leave_user_margin():
    normal_requests = 4 * 48
    settings = Settings(_env_file=None)

    assert normal_requests == 192
    assert settings.newsapi_logical_run_limit() * NEWSAPI_MAX_ATTEMPTS == 900
    assert 1000 - settings.newsapi_request_budget_24h == 100


class _SerializedState:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.recent = 0


class _SerializedTransaction:
    def __init__(self, state: _SerializedState):
        self.state = state

    async def __aenter__(self):
        await self.state.lock.acquire()
        return self

    async def __aexit__(self, *_exc):
        self.state.lock.release()


class _SerializedConnection:
    def __init__(self, state: _SerializedState):
        self.state = state

    def transaction(self):
        return _SerializedTransaction(self.state)

    async def execute(self, _query, *_args):
        return None

    async def fetchval(self, query, *_args):
        if "SELECT count(*)" in query:
            await asyncio.sleep(0)
            return self.state.recent
        self.state.recent += 1
        return self.state.recent


class _SerializedPool:
    def __init__(self):
        self.state = _SerializedState()

    def acquire(self):
        return _Acquire(_SerializedConnection(self.state))


@pytest.mark.asyncio
async def test_transaction_serializes_two_competing_budget_reservations():
    pool = _SerializedPool()
    repo = Repository(SimpleNamespace(pool=pool))

    results = await asyncio.gather(
        repo.start_bounded_load_run(
            "newsapi:first",
            "AI",
            provider_prefix="newsapi:",
            max_runs=1,
        ),
        repo.start_bounded_load_run(
            "newsapi:second",
            "AI",
            provider_prefix="newsapi:",
            max_runs=1,
        ),
    )

    assert sorted(results, key=lambda value: value is None) == [1, None]
    assert pool.state.recent == 1
