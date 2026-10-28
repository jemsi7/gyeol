"""Cache and deadline around market lookups (I-13204-b, DR-13204).

The facade runs while a reader response is being assembled, so it has two jobs
beyond caching: never serve a stale number as current, and never make the
reader wait. Both are failure-shaped, so most of these tests break something
and assert that the response still arrives.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.market_data import CACHE_TTL_S, LOOKUP_DEADLINE_S, MarketDataFacade, MarketValue


NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
BTC = ("price", "asset", "BTC")
ETH = ("price", "asset", "ETH")


def _row(value=64000.0, *, age_s=0.0, attribution="CoinGecko") -> dict:
    return {
        "value": value,
        "unit": "USD",
        "provider": "coingecko",
        "attribution": attribution,
        "observed_at": NOW,
        "fetched_at": NOW - timedelta(seconds=age_s),
    }


def _value(value=64000.0) -> MarketValue:
    return MarketValue(
        value=value,
        unit="USD",
        observed_at=NOW,
        provider="coingecko",
        attribution="CoinGecko",
    )


class FakeRepo:
    def __init__(self, cached=None, *, read_error=None, write_error=None):
        self.cached = dict(cached or {})
        self.read_error = read_error
        self.write_error = write_error
        self.written: list[dict] = []

    async def read_market_values(self, subjects):
        if self.read_error:
            raise self.read_error
        return {key: self.cached[key] for key in subjects if key in self.cached}

    async def write_market_values(self, entries):
        if self.write_error:
            raise self.write_error
        self.written.extend(entries)
        return len(entries)


class FakeClient:
    def __init__(self, values=None, *, delay=0.0, error=None):
        self.values = dict(values or {})
        self.delay = delay
        self.error = error
        self.asked: list[tuple[str, str, str]] = []

    async def fetch_many(self, subjects):
        self.asked.extend(subjects)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return {key: self.values[key] for key in subjects if key in self.values}


def _facade(client, repo, **kwargs) -> MarketDataFacade:
    return MarketDataFacade(client, repo, now=lambda: NOW, **kwargs)


@pytest.mark.asyncio
async def test_a_fresh_cache_entry_skips_the_provider():
    client = FakeClient()
    facade = _facade(client, FakeRepo({BTC: _row()}))

    found = await facade.current([BTC])

    assert found[BTC].value == 64000.0
    assert client.asked == []


@pytest.mark.asyncio
async def test_an_expired_entry_is_refetched_and_rewritten():
    client = FakeClient({BTC: _value(70000.0)})
    repo = FakeRepo({BTC: _row(age_s=CACHE_TTL_S + 1)})
    facade = _facade(client, repo)

    found = await facade.current([BTC])

    assert found[BTC].value == 70000.0
    assert client.asked == [BTC]
    assert repo.written and repo.written[0]["subject_id"] == "BTC"


@pytest.mark.asyncio
async def test_an_expired_entry_that_cannot_refresh_is_absent():
    # Showing a fifteen-minute-old number as "now" is the exact misreading
    # this feature exists to prevent, so the stale value is not reused.
    client = FakeClient({})
    facade = _facade(client, FakeRepo({BTC: _row(age_s=CACHE_TTL_S + 1)}))

    assert await facade.current([BTC]) == {}


@pytest.mark.asyncio
async def test_a_missing_key_is_absent_not_null():
    client = FakeClient({BTC: _value()})
    facade = _facade(client, FakeRepo())

    found = await facade.current([BTC, ETH])

    assert list(found) == [BTC]
    assert ETH not in found


@pytest.mark.asyncio
async def test_a_cache_read_failure_still_answers():
    client = FakeClient({BTC: _value()})
    facade = _facade(client, FakeRepo(read_error=RuntimeError("db down")))

    found = await facade.current([BTC])

    assert found[BTC].value == 64000.0


@pytest.mark.asyncio
async def test_a_cache_write_failure_still_answers():
    client = FakeClient({BTC: _value()})
    facade = _facade(client, FakeRepo(write_error=RuntimeError("db down")))

    found = await facade.current([BTC])

    # The value is already usable; persisting it is best effort.
    assert found[BTC].value == 64000.0


@pytest.mark.asyncio
async def test_a_provider_failure_keeps_whatever_the_cache_gave():
    client = FakeClient(error=RuntimeError("provider down"))
    facade = _facade(client, FakeRepo({BTC: _row()}))

    found = await facade.current([BTC, ETH])

    assert list(found) == [BTC]


@pytest.mark.asyncio
async def test_a_slow_provider_is_cut_off_at_the_deadline():
    client = FakeClient({ETH: _value()}, delay=0.4)
    facade = _facade(client, FakeRepo({BTC: _row()}), deadline_s=0.05)

    started = asyncio.get_running_loop().time()
    found = await facade.current([BTC, ETH])
    elapsed = asyncio.get_running_loop().time() - started

    # The cached half still answers; the slow half is dropped.
    assert list(found) == [BTC]
    assert elapsed < 0.3


@pytest.mark.asyncio
async def test_duplicate_subjects_are_asked_once():
    client = FakeClient({BTC: _value()})
    facade = _facade(client, FakeRepo())

    await facade.current([BTC, BTC, BTC])

    assert client.asked == [BTC]


@pytest.mark.asyncio
async def test_no_subjects_touches_nothing():
    client = FakeClient()
    repo = FakeRepo()
    assert await _facade(client, repo).current([]) == {}
    assert client.asked == []


@pytest.mark.asyncio
async def test_a_row_without_a_fetch_time_counts_as_stale():
    row = _row()
    row["fetched_at"] = None
    client = FakeClient({BTC: _value(70000.0)})
    facade = _facade(client, FakeRepo({BTC: row}))

    found = await facade.current([BTC])

    assert found[BTC].value == 70000.0


def test_defaults_are_bounded():
    assert CACHE_TTL_S == 900.0
    assert LOOKUP_DEADLINE_S == 0.5
