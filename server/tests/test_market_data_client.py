"""Market and on-chain value lookups (I-13204-a, DR-13204).

The product has avoided external data dependencies until now, so this one is
built to vanish rather than complain: every failure path returns ``None`` and
nothing raises into the Map pipeline. Most of these tests assert that a broken
provider produces silence, not an error.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from app.market_data import (
    MAX_CONCURRENT_FETCHES,
    RATE_LIMIT_BLOCK_S,
    SUPPORTED_TAGS,
    MarketDataClient,
    SymbolDirectory,
)


IDS_FILE = Path(__file__).resolve().parents[1] / "market_data_ids.yaml"
NOW = datetime(2026, 8, 12, tzinfo=UTC)


def _client(handler, *, directory=None, now=None) -> MarketDataClient:
    transport = httpx.MockTransport(handler)
    return MarketDataClient(
        httpx.AsyncClient(transport=transport),
        directory=directory or SymbolDirectory({"BTC": "bitcoin"}),
        now=now or (lambda: NOW),
    )


def _json(payload, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return handler


def _coin(price=None, market_cap=None, supply=None) -> dict:
    market: dict = {}
    if price is not None:
        market["current_price"] = {"usd": price}
    if market_cap is not None:
        market["market_cap"] = {"usd": market_cap}
    if supply is not None:
        market["circulating_supply"] = supply
    return {"market_data": market}


# --- supported surface -------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("tag", ["holdings", "fee", "hashrate", "nonsense"])
async def test_unsupported_metrics_never_touch_the_network(tag):
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(200, json={})

    result = await _client(handler).fetch(
        tag=tag, subject_kind="asset", subject_id="BTC"
    )
    # Tagging may label metrics no free source answers; that is not an error.
    assert result is None
    assert calls == []


def test_supported_tags_are_the_documented_four():
    assert SUPPORTED_TAGS == {"price", "marketcap", "tvl", "supply"}


# --- coin metrics ------------------------------------------------------------

@pytest.mark.asyncio
async def test_price_uses_the_mapped_id_not_the_symbol():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json=_coin(price=64000.5))

    value = await _client(handler).fetch(
        tag="price", subject_kind="asset", subject_id="btc"
    )
    assert value is not None
    assert value.value == pytest.approx(64000.5)
    assert value.unit == "USD"
    assert value.provider == "coingecko"
    assert value.attribution == "CoinGecko"
    # The ticker never reaches the API; the resolved id does.
    assert "/coins/bitcoin" in seen[0]
    assert "/coins/btc" not in seen[0]


@pytest.mark.asyncio
async def test_an_unmapped_ticker_yields_nothing_and_asks_nobody():
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(200, json=_coin(price=1))

    result = await _client(handler, directory=SymbolDirectory({})).fetch(
        tag="price", subject_kind="asset", subject_id="ZZZ"
    )
    # Guessing an id would produce a confident wrong number.
    assert result is None
    assert calls == []


@pytest.mark.asyncio
async def test_supply_is_a_count_not_a_currency():
    value = await _client(_json(_coin(supply=19_700_000))).fetch(
        tag="supply", subject_kind="asset", subject_id="BTC"
    )
    assert value.unit == "count"
    assert value.value == pytest.approx(19_700_000)


@pytest.mark.asyncio
async def test_coin_metrics_require_an_asset_subject():
    result = await _client(_json(_coin(price=1))).fetch(
        tag="price", subject_kind="protocol", subject_id="BTC"
    )
    assert result is None


# --- tvl ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_protocol_tvl_reads_the_scalar():
    value = await _client(_json({"tvl": 4.8e10})).fetch(
        tag="tvl", subject_kind="protocol", subject_id="aave-v3"
    )
    assert value.value == pytest.approx(4.8e10)
    assert value.provider == "defillama"


@pytest.mark.asyncio
async def test_protocol_tvl_falls_back_to_the_series_tail():
    payload = {"tvl": [{"totalLiquidityUSD": 1.0}, {"totalLiquidityUSD": 2.5e9}]}
    value = await _client(_json(payload)).fetch(
        tag="tvl", subject_kind="protocol", subject_id="aave-v3"
    )
    assert value.value == pytest.approx(2.5e9)


@pytest.mark.asyncio
async def test_chain_tvl_matches_by_name_or_gecko_id():
    payload = [
        {"name": "Solana", "gecko_id": "solana", "tvl": 1.0e9},
        {"name": "Ethereum", "gecko_id": "ethereum", "tvl": 4.8e10},
    ]
    for subject in ("Ethereum", "ethereum"):
        value = await _client(_json(payload)).fetch(
            tag="tvl", subject_kind="chain", subject_id=subject
        )
        assert value.value == pytest.approx(4.8e10)


@pytest.mark.asyncio
async def test_an_unknown_chain_yields_nothing():
    payload = [{"name": "Solana", "gecko_id": "solana", "tvl": 1.0}]
    result = await _client(_json(payload)).fetch(
        tag="tvl", subject_kind="chain", subject_id="nowhere"
    )
    assert result is None


# --- failure modes all collapse to None --------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 404, 500, 503])
async def test_http_errors_are_silent(status):
    result = await _client(_json(_coin(price=1), status=status)).fetch(
        tag="price", subject_kind="asset", subject_id="BTC"
    )
    assert result is None


@pytest.mark.asyncio
async def test_transport_errors_are_silent():
    def handler(request):
        raise httpx.ConnectTimeout("slow", request=request)

    result = await _client(handler).fetch(
        tag="price", subject_kind="asset", subject_id="BTC"
    )
    assert result is None


@pytest.mark.asyncio
async def test_malformed_payloads_are_silent():
    def handler(request):
        return httpx.Response(200, content=b"not json")

    assert await _client(handler).fetch(
        tag="price", subject_kind="asset", subject_id="BTC"
    ) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [0, -1, None, True, "64000"])
async def test_non_positive_or_non_numeric_values_are_rejected(raw):
    # A zero or negative market value is not a real reading.
    result = await _client(_json(_coin(price=raw))).fetch(
        tag="price", subject_kind="asset", subject_id="BTC"
    )
    assert result is None


def test_non_finite_values_are_rejected():
    from app.market_data import _positive

    # Strict JSON cannot carry these, but a lenient provider or a later parser
    # change could, and inf would render as a nonsense comparison.
    for raw in (float("inf"), float("-inf"), float("nan")):
        assert _positive(raw) is None
    assert _positive(1.5) == 1.5


@pytest.mark.asyncio
async def test_a_rate_limit_holds_the_whole_provider_off():
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(429, json={})

    clock = {"now": NOW}
    client = _client(handler, now=lambda: clock["now"])
    assert await client.fetch(tag="price", subject_kind="asset", subject_id="BTC") is None
    assert await client.fetch(tag="price", subject_kind="asset", subject_id="BTC") is None
    # Retrying a 429 is asking to be limited twice.
    assert len(calls) == 1

    clock["now"] = NOW + timedelta(seconds=RATE_LIMIT_BLOCK_S + 1)
    assert await client.fetch(tag="price", subject_kind="asset", subject_id="BTC") is None
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_one_provider_block_does_not_silence_the_other():
    def handler(request):
        if "llama" in str(request.url):
            return httpx.Response(200, json={"tvl": 1.0e9})
        return httpx.Response(429, json={})

    client = _client(handler)
    assert await client.fetch(tag="price", subject_kind="asset", subject_id="BTC") is None
    value = await client.fetch(
        tag="tvl", subject_kind="protocol", subject_id="aave-v3"
    )
    assert value is not None


# --- bulk --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_many_omits_failures_and_dedupes():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if "bitcoin" in str(request.url):
            return httpx.Response(200, json=_coin(price=64000))
        return httpx.Response(500, json={})

    client = _client(handler)
    found = await client.fetch_many(
        [
            ("price", "asset", "BTC"),
            ("price", "asset", "BTC"),
            ("price", "asset", "ZZZ"),
        ]
    )
    # Missing keys are absent rather than present-and-null.
    assert list(found) == [("price", "asset", "BTC")]
    assert len([c for c in calls if "bitcoin" in c]) == 1


@pytest.mark.asyncio
async def test_fetch_many_handles_an_empty_request():
    assert await _client(_json({})).fetch_many([]) == {}


def test_concurrency_is_bounded():
    assert MAX_CONCURRENT_FETCHES == 4


# --- symbol directory --------------------------------------------------------

def test_directory_is_case_and_space_insensitive():
    directory = SymbolDirectory({"btc": "bitcoin"})
    assert directory.coin_id(" BtC ") == "bitcoin"
    assert directory.coin_id("") is None


def test_missing_directory_file_is_empty_not_fatal(tmp_path):
    assert len(SymbolDirectory.load(str(tmp_path / "absent.yaml"))) == 0


def test_shipped_directory_loads_and_covers_the_majors():
    directory = SymbolDirectory.load(str(IDS_FILE))
    assert len(directory) >= 25
    for symbol, expected in (("BTC", "bitcoin"), ("ETH", "ethereum")):
        assert directory.coin_id(symbol) == expected


@pytest.mark.parametrize("coins", [{"BTC": ""}, {"": "bitcoin"}, {"BTC": 1}])
def test_malformed_directory_entries_fail_closed(tmp_path, coins):
    import yaml

    path = tmp_path / "ids.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "coins": coins}), encoding="utf-8")
    with pytest.raises(ValueError):
        SymbolDirectory.load(str(path))
