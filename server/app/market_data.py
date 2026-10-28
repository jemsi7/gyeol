"""Current value of a tagged number, or nothing at all (C-13204, DR-13204).

This is the one external dependency the product has deliberately avoided until
now, so it is built to disappear quietly. Every failure mode -- unsupported
metric, unmapped symbol, HTTP error, timeout, rate limit, malformed payload --
returns ``None``. Nothing raises into the pipeline, and a missing value renders
as no annotation rather than as an error.

Free tier only for now: the open question this feature must answer first is
whether a number's subject can be identified at all, and a paid contract signed
before that answer would be wasted if the answer is no.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Mapping

import httpx
import yaml

log = logging.getLogger("gdelt.market_data")

# Metrics a free tier can answer reliably. holdings/fee/hashrate are tagged by
# C-13203 but deliberately unsupported here: no free source gives them per
# subject with a stable contract, and a wrong number is worse than none.
SUPPORTED_TAGS: Final[frozenset[str]] = frozenset(
    {"price", "marketcap", "tvl", "supply"}
)
DEFILLAMA_BASE: Final[str] = "https://api.llama.fi"
COINGECKO_BASE: Final[str] = "https://api.coingecko.com/api/v3"
REQUEST_TIMEOUT_S: Final[float] = 3.0
RATE_LIMIT_BLOCK_S: Final[float] = 60.0
MAX_CONCURRENT_FETCHES: Final[int] = 4


@dataclass(frozen=True, slots=True)
class MarketValue:
    value: float
    unit: str
    observed_at: datetime
    provider: str
    attribution: str = ""


class SymbolDirectory:
    """Ticker -> provider id. Symbols are never sent to an API directly.

    Duplicate tickers are the main source of wrong-subject lookups: several
    listed assets share a symbol, and asking an API for "one BTC price" by
    symbol silently picks whichever it prefers.
    """

    def __init__(self, coins: Mapping[str, str] | None = None):
        self._coins = {key.upper(): value for key, value in (coins or {}).items()}

    def __len__(self) -> int:
        return len(self._coins)

    @classmethod
    def load(cls, path: str) -> "SymbolDirectory":
        if not os.path.exists(path):
            log.warning("market data id map %s not found; price lookups stay empty", path)
            return cls()
        with open(path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        coins = data.get("coins") or {}
        if not isinstance(coins, dict):
            raise ValueError("market data coins must be a mapping")
        resolved: dict[str, str] = {}
        for symbol, coin_id in coins.items():
            if not isinstance(symbol, str) or not isinstance(coin_id, str):
                raise ValueError("market data coin entries must be strings")
            if not symbol.strip() or not coin_id.strip():
                raise ValueError("market data coin entries must not be blank")
            resolved[symbol.strip().upper()] = coin_id.strip()
        return cls(resolved)

    def coin_id(self, symbol: str) -> str | None:
        return self._coins.get((symbol or "").strip().upper())


def _positive(raw: Any) -> float | None:
    """A market value must be a real positive number to be worth showing."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    if value <= 0 or value != value or value in (float("inf"), float("-inf")):
        return None
    return value


class MarketDataClient:
    """Thin, fail-open reads from the free tiers of two providers."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        directory: SymbolDirectory | None = None,
        timeout_s: float = REQUEST_TIMEOUT_S,
        now: Any = None,
    ):
        self._client = client
        self._directory = directory or SymbolDirectory()
        self._timeout_s = timeout_s
        self._now = now or (lambda: datetime.now(UTC))
        self._blocked_until: dict[str, datetime] = {}

    def _blocked(self, provider: str) -> bool:
        until = self._blocked_until.get(provider)
        return until is not None and self._now() < until

    def _block(self, provider: str) -> None:
        # A 429 answered by retrying is a 429 answered twice; hold the whole
        # provider off rather than letting every lookup rediscover the limit.
        self._blocked_until[provider] = self._now() + timedelta(
            seconds=RATE_LIMIT_BLOCK_S
        )

    async def _get(self, provider: str, url: str, params: dict | None = None) -> Any:
        if self._blocked(provider):
            return None
        try:
            response = await self._client.get(
                url, params=params, timeout=self._timeout_s
            )
        except httpx.HTTPError as error:
            log.debug("market data fetch failed provider=%s error=%s", provider, type(error).__name__)
            return None
        if response.status_code == 429:
            log.warning("market data rate limited provider=%s", provider)
            self._block(provider)
            return None
        if response.status_code != 200:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    async def fetch(
        self, *, tag: str, subject_kind: str, subject_id: str
    ) -> MarketValue | None:
        """Current value for one tagged subject, or ``None`` for anything else."""
        if tag not in SUPPORTED_TAGS:
            # Not an error: C-13203 may tag metrics no free source can answer.
            return None
        if not (subject_id or "").strip():
            return None
        if tag == "tvl":
            return await self._fetch_tvl(subject_kind, subject_id.strip())
        return await self._fetch_coin_metric(tag, subject_kind, subject_id.strip())

    async def _fetch_tvl(self, subject_kind: str, subject_id: str) -> MarketValue | None:
        if subject_kind == "protocol":
            payload = await self._get(
                "defillama", f"{DEFILLAMA_BASE}/protocol/{subject_id}"
            )
            value = _positive((payload or {}).get("tvl") if isinstance(payload, dict) else None)
            if value is None and isinstance(payload, dict):
                # Some protocol payloads carry a time series instead of a scalar.
                series = payload.get("tvl")
                if isinstance(series, list) and series:
                    last = series[-1]
                    if isinstance(last, dict):
                        value = _positive(last.get("totalLiquidityUSD"))
            if value is None:
                return None
            return MarketValue(
                value=value,
                unit="USD",
                observed_at=self._now(),
                provider="defillama",
                attribution="DefiLlama",
            )
        if subject_kind == "chain":
            payload = await self._get("defillama", f"{DEFILLAMA_BASE}/v2/chains")
            if not isinstance(payload, list):
                return None
            wanted = subject_id.casefold()
            for entry in payload:
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get("name") or "").casefold()
                gecko = str(entry.get("gecko_id") or "").casefold()
                if wanted in (name, gecko):
                    value = _positive(entry.get("tvl"))
                    if value is None:
                        return None
                    return MarketValue(
                        value=value,
                        unit="USD",
                        observed_at=self._now(),
                        provider="defillama",
                        attribution="DefiLlama",
                    )
        return None

    async def _fetch_coin_metric(
        self, tag: str, subject_kind: str, subject_id: str
    ) -> MarketValue | None:
        if subject_kind != "asset":
            return None
        coin_id = self._directory.coin_id(subject_id)
        if coin_id is None:
            # An unmapped ticker is exactly the ambiguity this directory exists
            # to prevent; guessing would produce a confident wrong number.
            return None
        payload = await self._get(
            "coingecko",
            f"{COINGECKO_BASE}/coins/{coin_id}",
            params={
                "localization": "false",
                "tickers": "false",
                "community_data": "false",
                "developer_data": "false",
                "sparkline": "false",
            },
        )
        if not isinstance(payload, dict):
            return None
        market = payload.get("market_data")
        if not isinstance(market, dict):
            return None
        field, unit = {
            "price": ("current_price", "USD"),
            "marketcap": ("market_cap", "USD"),
            "supply": ("circulating_supply", "count"),
        }[tag]
        raw = market.get(field)
        if unit == "USD":
            raw = raw.get("usd") if isinstance(raw, dict) else None
        value = _positive(raw)
        if value is None:
            return None
        return MarketValue(
            value=value,
            unit=unit,
            observed_at=self._now(),
            provider="coingecko",
            attribution="CoinGecko",
        )

    async def fetch_many(
        self, subjects: "list[tuple[str, str, str]]"
    ) -> dict[tuple[str, str, str], MarketValue]:
        """Bounded concurrent lookups; missing keys simply do not appear."""
        unique = list(dict.fromkeys(subjects))
        if not unique:
            return {}
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_FETCHES)

        async def one(key: tuple[str, str, str]):
            async with semaphore:
                tag, subject_kind, subject_id = key
                return key, await self.fetch(
                    tag=tag, subject_kind=subject_kind, subject_id=subject_id
                )

        results = await asyncio.gather(
            *(one(key) for key in unique), return_exceptions=True
        )
        found: dict[tuple[str, str, str], MarketValue] = {}
        for item in results:
            if isinstance(item, BaseException):
                continue
            key, value = item
            if value is not None:
                found[key] = value
        return found

CACHE_TTL_S: Final[float] = 900.0
LOOKUP_DEADLINE_S: Final[float] = 0.5


class MarketDataFacade:
    """Cache-first bulk lookup that never delays or breaks a reader response.

    Two rules carry the design. A stale entry is never served: the whole point
    of the comparison is to say what a number is *now*, and presenting a
    fifteen-minute-old value as current is the misreading this feature exists
    to prevent. And the lookup runs inside a request, so it answers within a
    deadline with whatever it has rather than making the reader wait.
    """

    def __init__(
        self,
        client: MarketDataClient,
        repo: Any,
        *,
        ttl_s: float = CACHE_TTL_S,
        deadline_s: float = LOOKUP_DEADLINE_S,
        now: Any = None,
    ):
        self._client = client
        self._repo = repo
        self._ttl_s = ttl_s
        self._deadline_s = deadline_s
        self._now = now or (lambda: datetime.now(UTC))

    def _fresh(self, row: Mapping[str, Any]) -> bool:
        fetched_at = row.get("fetched_at")
        if not isinstance(fetched_at, datetime):
            return False
        return (self._now() - fetched_at).total_seconds() < self._ttl_s

    async def current(
        self, subjects: "list[tuple[str, str, str]]"
    ) -> dict[tuple[str, str, str], MarketValue]:
        keys = list(dict.fromkeys(subjects))
        if not keys:
            return {}
        try:
            cached = await self._repo.read_market_values(keys)
        except Exception:  # noqa: BLE001 - the cache must not break a response
            log.warning("market value cache read failed; continuing uncached")
            cached = {}

        found: dict[tuple[str, str, str], MarketValue] = {}
        stale: list[tuple[str, str, str]] = []
        for key in keys:
            row = cached.get(key)
            if row is None:
                stale.append(key)
                continue
            if not self._fresh(row):
                # Deliberately not reused: an expired value that fails to
                # refresh is absent, not "close enough".
                stale.append(key)
                continue
            found[key] = MarketValue(
                value=float(row["value"]),
                unit=str(row["unit"]),
                observed_at=row["observed_at"],
                provider=str(row["provider"]),
                attribution=str(row.get("attribution") or ""),
            )
        if not stale:
            return found

        try:
            fetched = await asyncio.wait_for(
                self._client.fetch_many(stale), timeout=self._deadline_s
            )
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            # Whatever the cache already gave us is still a valid answer.
            return found
        found.update(fetched)

        if fetched:
            try:
                await self._repo.write_market_values(
                    [
                        {
                            "tag": key[0],
                            "subject_kind": key[1],
                            "subject_id": key[2],
                            "value": value.value,
                            "unit": value.unit,
                            "provider": value.provider,
                            "attribution": value.attribution,
                            "observed_at": value.observed_at,
                        }
                        for key, value in fetched.items()
                    ]
                )
            except Exception:  # noqa: BLE001 - the value is already usable
                log.warning("market value cache write failed")
        return found
