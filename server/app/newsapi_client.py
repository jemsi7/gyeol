"""Secret-safe async adapter for NewsAPI's /v2/everything endpoint."""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from .config import NEWSAPI_MAX_ATTEMPTS, Settings
from .models import Article

log = logging.getLogger("newsapi.client")

_PERMANENT_CODES = {
    "apiKeyDisabled",
    "apiKeyExhausted",
    "apiKeyInvalid",
    "apiKeyMissing",
    "parameterInvalid",
    "parametersMissing",
    "sourcesTooMany",
    "sourceDoesNotExist",
}


class NewsApiError(RuntimeError):
    """A redacted provider failure safe for run audit and logs."""

    def __init__(self, code: str, status_code: int | None = None):
        self.code = code
        self.status_code = status_code
        super().__init__(f"newsapi_{code}")


class NewsApiPermanentError(NewsApiError):
    """A request/key failure that retrying cannot repair."""


class NewsApiTransientError(NewsApiError):
    """A bounded transport/provider failure that may recover."""


class NewsApiClient:
    def __init__(
        self,
        settings: Settings,
        client: Optional[httpx.AsyncClient] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.s = settings
        self._client = client
        self._owns_client = client is None
        self._sleep = sleep

    async def __aenter__(self) -> "NewsApiClient":
        if not self.s.newsapi_api_key.get_secret_value().strip():
            raise NewsApiPermanentError("key_missing", 401)
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.s.newsapi_timeout_s,
                headers={"User-Agent": self.s.newsapi_user_agent},
                follow_redirects=True,
            )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def fetch(
        self,
        query: str,
        *,
        start: datetime,
        end: datetime,
        page_size: int = 100,
        language: str = "en",
        search_in: str = "title,description",
        sort_by: str = "publishedAt",
        page: int = 1,
    ) -> list[Article]:
        query = query.strip()
        if not query or len(query) > 500:
            raise NewsApiPermanentError("parameter_invalid", 400)
        if not 1 <= page_size <= 100 or page < 1 or start >= end:
            raise NewsApiPermanentError("parameter_invalid", 400)

        params: dict[str, Any] = {
            "q": query,
            "searchIn": search_in,
            "from": _wire_datetime(start),
            "to": _wire_datetime(end),
            "language": language,
            "sortBy": sort_by,
            "pageSize": page_size,
            "page": page,
        }
        data: dict[str, Any] | None = None
        for attempt in range(1, NEWSAPI_MAX_ATTEMPTS + 1):
            try:
                data = await self._request_once(params)
                break
            except NewsApiTransientError:
                if attempt >= NEWSAPI_MAX_ATTEMPTS:
                    raise
                await self._sleep(float(2**attempt))
        assert data is not None

        articles: list[Article] = []
        seen_ids: set[str] = set()
        for item in data["articles"]:
            if not isinstance(item, dict):
                continue
            article = Article.from_newsapi(
                item,
                source_query=query,
                language=language,
            )
            if article is None or article.id in seen_ids:
                continue
            seen_ids.add(article.id)
            articles.append(article)
        log.info("NewsAPI returned %d usable article(s)", len(articles))
        return articles

    async def _request_once(self, params: dict[str, Any]) -> dict[str, Any]:
        assert self._client is not None
        key = self.s.newsapi_api_key.get_secret_value().strip()
        try:
            response = await self._client.get(
                self.s.newsapi_base_url,
                params=params,
                headers={"X-Api-Key": key},
            )
        except httpx.TransportError as exc:
            raise NewsApiTransientError("transport") from exc

        data = _decode_object(response)
        provider_code = data.get("code") if data is not None else None
        if response.status_code >= 500:
            raise NewsApiTransientError("server_error", response.status_code)
        if response.status_code == 429:
            if provider_code == "apiKeyExhausted":
                raise NewsApiPermanentError("api_key_exhausted", 429)
            raise NewsApiTransientError("rate_limited", 429)
        if response.status_code >= 400:
            raise NewsApiPermanentError(
                _safe_permanent_code(provider_code, response.status_code),
                response.status_code,
            )
        if data is None:
            raise NewsApiTransientError("invalid_json", response.status_code)
        if data.get("status") == "error":
            if provider_code == "apiKeyExhausted":
                raise NewsApiPermanentError("api_key_exhausted", response.status_code)
            if provider_code in _PERMANENT_CODES:
                raise NewsApiPermanentError(
                    _safe_permanent_code(provider_code, response.status_code),
                    response.status_code,
                )
            raise NewsApiTransientError("provider_error", response.status_code)
        if data.get("status") != "ok" or not isinstance(data.get("articles"), list):
            raise NewsApiTransientError("invalid_response", response.status_code)
        return data


def _decode_object(response: httpx.Response) -> dict[str, Any] | None:
    try:
        value = json.loads(response.text)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _safe_permanent_code(provider_code: Any, status_code: int) -> str:
    mapping = {
        "apiKeyDisabled": "api_key_disabled",
        "apiKeyExhausted": "api_key_exhausted",
        "apiKeyInvalid": "api_key_invalid",
        "apiKeyMissing": "api_key_missing",
        "parameterInvalid": "parameter_invalid",
        "parametersMissing": "parameters_missing",
        "sourcesTooMany": "sources_too_many",
        "sourceDoesNotExist": "source_does_not_exist",
    }
    return mapping.get(provider_code, f"http_{status_code}")


def _wire_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    wire = value.astimezone(timezone.utc).isoformat(timespec="seconds")
    return wire.replace("+00:00", "Z")
