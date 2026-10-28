"""GDELT response classification, retry, pacing, and window regressions."""

import logging
from unittest.mock import AsyncMock

import httpx
import pytest
from tenacity import wait_none

from app.config import Settings
from app.gdelt_client import (
    GdeltClient,
    GdeltRequestError,
    GdeltTransientError,
    _effective_min_interval_s,
)
from app.models import Article


def _settings() -> Settings:
    return Settings(_env_file=None, GDELT_MIN_INTERVAL_S=5.0)


def _article(article_id: str, title: str | None = None) -> Article:
    return Article(
        id=article_id,
        url=f"https://news.example/{article_id}",
        title=title or article_id,
        seendate=None,
        domain="news.example",
        language="English",
        sourcecountry="United States",
        url_mobile=None,
        socialimage_url=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "message"),
    (
        (
            "Your query was too short or too long.",
            "request rejected by GDELT (query length or syntax)",
        ),
        (
            "Timespan is too short.",
            "request rejected by GDELT (timespan too short)",
        ),
    ),
)
async def test_request_validation_errors_are_permanent_and_not_retried(
    body: str,
    message: str,
):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=f"  {body}\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GdeltClient(_settings(), http)

        with pytest.raises(GdeltRequestError, match=message.replace("(", r"\(").replace(")", r"\)")):
            await client.fetch("technology", timespan="1h")

    assert len(requests) == 1


@pytest.mark.asyncio
async def test_rate_limit_is_transient_and_retries_to_json(monkeypatch):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, text="rate limit provider details")
        return httpx.Response(
            200,
            json={"articles": [{"url": "https://news.example/one", "title": "One"}]},
        )

    monkeypatch.setattr(GdeltClient._get_json.retry, "wait", wait_none())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GdeltClient(_settings(), http)
        client._throttle = AsyncMock()

        articles = await client.fetch("technology", timespan="1h")

    assert len(requests) == 2
    assert [article.url for article in articles] == ["https://news.example/one"]


@pytest.mark.asyncio
async def test_invalid_json_remains_bounded_transient(monkeypatch):
    request_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, text='{"articles": [')

    monkeypatch.setattr(GdeltClient._get_json.retry, "wait", wait_none())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GdeltClient(_settings(), http)
        client._throttle = AsyncMock()

        with pytest.raises(GdeltTransientError, match="invalid JSON from GDELT"):
            await client.fetch("technology", timespan="1h")

    assert request_count == 4


@pytest.mark.asyncio
async def test_salvage_and_empty_response_contract():
    responses = iter(
        (
            httpx.Response(200, text='notice {"articles": []} trailing'),
            httpx.Response(200, text="  \n"),
        )
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return next(responses)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        first_client = GdeltClient(_settings(), http)
        second_client = GdeltClient(_settings(), http)

        assert await first_client.fetch("technology", timespan="1h") == []
        assert await second_client.fetch("technology", timespan="1h") == []


@pytest.mark.asyncio
async def test_non_rate_limited_4xx_is_not_retried():
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(400, request=request, text="provider details")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GdeltClient(_settings(), http)

        with pytest.raises(httpx.HTTPStatusError):
            await client.fetch("technology", timespan="1h")

    assert request_count == 1


@pytest.mark.parametrize(
    ("configured", "expected"),
    ((-1.0, 5.0), (0.0, 5.0), (1.0, 5.0), (5.0, 5.0), (8.0, 8.0)),
)
def test_effective_interval_never_drops_below_five_seconds(
    configured: float,
    expected: float,
):
    assert _effective_min_interval_s(configured) == expected


def test_default_interval_is_five_seconds():
    assert Settings(_env_file=None).gdelt_min_interval_s == 5.0


@pytest.mark.asyncio
async def test_fetch_window_keeps_successful_slices_after_transient_failure(caplog):
    client = GdeltClient(_settings())
    first = _article("first")
    last = _article("last")
    client.fetch = AsyncMock(
        side_effect=(
            [first],
            GdeltTransientError("rate limited (429)"),
            [last],
        )
    )

    with caplog.at_level(logging.WARNING, logger="gdelt.client"):
        articles = await client.fetch_window("technology", hours=18, slice_hours=6)

    assert [article.id for article in articles] == ["first", "last"]
    assert client.fetch.await_count == 3
    assert "succeeded=2 failed=1 total=3" in caplog.text
    assert "last_error=GdeltTransientError" in caplog.text


@pytest.mark.asyncio
async def test_fetch_window_raises_when_every_slice_is_transient():
    client = GdeltClient(_settings())
    client.fetch = AsyncMock(
        side_effect=(
            GdeltTransientError("first transient"),
            httpx.ConnectError("second transient"),
        )
    )

    with pytest.raises(httpx.ConnectError, match="second transient"):
        await client.fetch_window("technology", hours=12, slice_hours=6)

    assert client.fetch.await_count == 2


@pytest.mark.asyncio
async def test_fetch_window_stops_immediately_on_permanent_error():
    client = GdeltClient(_settings())
    client.fetch = AsyncMock(
        side_effect=GdeltRequestError("request rejected by GDELT")
    )

    with pytest.raises(GdeltRequestError, match="request rejected by GDELT"):
        await client.fetch_window("technology", hours=24, slice_hours=6)

    assert client.fetch.await_count == 1


@pytest.mark.asyncio
async def test_fetch_window_deduplicates_across_partial_successes():
    client = GdeltClient(_settings())
    duplicate_first = _article("duplicate", "older title")
    duplicate_last = _article("duplicate", "newer title")
    client.fetch = AsyncMock(
        side_effect=(
            [duplicate_first],
            GdeltTransientError("temporary"),
            [duplicate_last],
        )
    )

    articles = await client.fetch_window("technology", hours=18, slice_hours=6)

    assert len(articles) == 1
    assert articles[0].id == "duplicate"
    assert articles[0].title == "newer title"
