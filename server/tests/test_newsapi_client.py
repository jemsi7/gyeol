"""Offline NewsAPI adapter response, retry, and secret-safety contracts."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.config import NEWSAPI_MAX_ATTEMPTS, Settings
from app.newsapi_client import (
    NewsApiClient,
    NewsApiPermanentError,
    NewsApiTransientError,
)


NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
KEY = "newsapi-test-secret"


def _settings(**values) -> Settings:
    return Settings(
        _env_file=None,
        NEWSAPI_ENABLED=True,
        NEWSAPI_API_KEY=KEY,
        **values,
    )


@pytest.mark.asyncio
async def test_newsapi_uses_header_and_exact_params():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "totalResults": 1,
                "articles": [
                    {
                        "source": {"id": "tech", "name": "Tech"},
                        "title": "  Agent release  ",
                        "url": "https://Tech.Example/release",
                        "urlToImage": "https://img.example/release.jpg",
                        "publishedAt": "2026-07-19T11:30:00Z",
                        "content": "truncated [200 chars]",
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        async with NewsApiClient(_settings(), http) as client:
            articles = await client.fetch(
                '"AI agent"',
                start=NOW - timedelta(hours=24),
                end=NOW,
            )

    assert len(requests) == 1
    request = requests[0]
    assert request.headers["X-Api-Key"] == KEY
    assert KEY not in str(request.url)
    assert dict(request.url.params) == {
        "q": '"AI agent"',
        "searchIn": "title,description",
        "from": "2026-07-18T12:00:00Z",
        "to": "2026-07-19T12:00:00Z",
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": "100",
        "page": "1",
    }
    assert len(articles) == 1
    article = articles[0]
    assert article.title == "Agent release"
    assert article.domain == "tech.example"
    assert article.language == "English"
    assert article.seendate == datetime(2026, 7, 19, 11, 30, tzinfo=UTC)
    assert article.source_query == 'newsapi:"AI agent"'
    assert article.raw["_provider"] == "newsapi"
    assert KEY not in repr(article.raw)


@pytest.mark.asyncio
async def test_newsapi_normalizes_and_deduplicates_usable_articles():
    payload = {
        "status": "ok",
        "articles": [
            {"title": "First", "url": "https://news.example/same"},
            {"title": "Duplicate", "url": "https://news.example/same"},
            {"title": "[Removed]", "url": "https://news.example/removed"},
            {"title": "No URL"},
            {"title": 123, "url": "https://news.example/numeric-title"},
            {"title": "Local", "url": "file:///tmp/article"},
            {
                "title": "Malformed date",
                "url": "https://news.example/date",
                "publishedAt": "not-a-date",
                "urlToImage": "data:image/png;base64,no",
            },
            {
                "title": "Numeric date",
                "url": "https://news.example/numeric-date",
                "publishedAt": 123,
            },
        ],
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        async with NewsApiClient(_settings(), http) as client:
            articles = await client.fetch("technology", start=NOW - timedelta(days=1), end=NOW)

    assert [article.title for article in articles] == [
        "First",
        "Malformed date",
        "Numeric date",
    ]
    assert articles[1].seendate is None
    assert articles[1].socialimage_url is None
    assert articles[2].seendate is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "code", "safe_code"),
    (
        (401, "apiKeyInvalid", "api_key_invalid"),
        (401, "apiKeyDisabled", "api_key_disabled"),
        (401, "apiKeyMissing", "api_key_missing"),
        (400, "parameterInvalid", "parameter_invalid"),
        (400, "parametersMissing", "parameters_missing"),
        (400, "sourcesTooMany", "sources_too_many"),
        (400, "sourceDoesNotExist", "source_does_not_exist"),
        (429, "apiKeyExhausted", "api_key_exhausted"),
    ),
)
async def test_permanent_provider_errors_are_not_retried(status, code, safe_code):
    request_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            status,
            json={"status": "error", "code": code, "message": f"secret {KEY}"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        async with NewsApiClient(_settings(), http) as client:
            with pytest.raises(NewsApiPermanentError) as caught:
                await client.fetch("AI", start=NOW - timedelta(days=1), end=NOW)

    assert request_count == 1
    assert caught.value.code == safe_code
    assert KEY not in str(caught.value)


@pytest.mark.asyncio
async def test_transient_errors_retry_at_most_four_attempts():
    requests = 0
    waits: list[float] = []

    async def no_wait(seconds: float) -> None:
        waits.append(seconds)

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests < NEWSAPI_MAX_ATTEMPTS:
            return httpx.Response(
                429,
                json={"status": "error", "code": "rateLimited", "message": KEY},
            )
        return httpx.Response(200, json={"status": "ok", "articles": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        async with NewsApiClient(_settings(), http, sleep=no_wait) as client:
            assert await client.fetch("AI", start=NOW - timedelta(days=1), end=NOW) == []

    assert requests == 4
    assert waits == [2.0, 4.0, 8.0]


@pytest.mark.asyncio
async def test_invalid_json_exhausts_bounded_transient_attempts():
    requests = 0

    async def no_wait(_seconds: float) -> None:
        return None

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, text="not-json")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        async with NewsApiClient(_settings(), http, sleep=no_wait) as client:
            with pytest.raises(NewsApiTransientError, match="newsapi_invalid_json"):
                await client.fetch("AI", start=NOW - timedelta(days=1), end=NOW)

    assert requests == NEWSAPI_MAX_ATTEMPTS


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ("transport", "server", "invalid-response"))
async def test_other_transient_failures_retry_then_recover(failure):
    requests = 0
    waits: list[float] = []

    async def no_wait(seconds: float) -> None:
        waits.append(seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            if failure == "transport":
                raise httpx.ConnectError("offline", request=request)
            if failure == "server":
                return httpx.Response(503, text="unavailable")
            return httpx.Response(200, json={"status": "ok", "articles": {}})
        return httpx.Response(200, json={"status": "ok", "articles": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        async with NewsApiClient(_settings(), http, sleep=no_wait) as client:
            assert await client.fetch("AI", start=NOW - timedelta(days=1), end=NOW) == []

    assert requests == 2
    assert waits == [2.0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    (
        {"query": ""},
        {"query": "x" * 501},
        {"page_size": 0},
        {"page_size": 101},
        {"page": 0},
        {"start": NOW, "end": NOW},
    ),
)
async def test_invalid_client_arguments_fail_before_network(arguments):
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"status": "ok", "articles": []})

    values = {
        "query": "AI",
        "start": NOW - timedelta(days=1),
        "end": NOW,
    }
    values.update(arguments)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        async with NewsApiClient(_settings(), http) as client:
            with pytest.raises(NewsApiPermanentError, match="newsapi_parameter_invalid"):
                await client.fetch(**values)

    assert requests == 0


@pytest.mark.asyncio
async def test_owned_client_closes_and_external_client_remains_open():
    owned_adapter = NewsApiClient(_settings())
    async with owned_adapter:
        owned_http = owned_adapter._client
        assert owned_http is not None
        assert owned_http.is_closed is False
    assert owned_http.is_closed is True
    assert owned_adapter._client is None

    external_http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: None))
    async with NewsApiClient(_settings(), external_http):
        pass
    assert external_http.is_closed is False
    await external_http.aclose()


@pytest.mark.asyncio
async def test_empty_key_fails_before_opening_or_requesting_client():
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500)

    settings = Settings(
        _env_file=None,
        NEWSAPI_ENABLED=True,
        NEWSAPI_API_KEY="",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(NewsApiPermanentError, match="newsapi_key_missing"):
            async with NewsApiClient(settings, http):
                pass

    assert requests == 0


def test_newsapi_budget_reserves_all_retry_attempts_and_masks_key():
    settings = _settings()

    assert settings.newsapi_logical_run_limit() == 225
    assert settings.newsapi_logical_run_limit() * NEWSAPI_MAX_ATTEMPTS == 900
    assert KEY not in repr(settings)
