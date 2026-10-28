"""NewsAPI provider routing, gap filtering, and common Map handoff tests."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.engine as engine_module
from app.config import Settings
from app.engine import LoaderEngine
from app.jobs import Job
from app.models import Article
from app.newsapi_client import NewsApiTransientError
from app.repository import Repository


def _article(article_id: str) -> Article:
    return Article(
        id=article_id,
        url=f"https://specialist.example/{article_id}",
        title=f"Article {article_id}",
        seendate=None,
        domain="specialist.example",
        language="English",
        sourcecountry=None,
        url_mobile=None,
        socialimage_url=f"https://images.example/{article_id}.jpg",
        source_query="newsapi:AI",
    )


def _job(name: str = "newsapi:ai-models-agents", query: str = "AI") -> Job:
    return Job(
        name=name,
        query=query,
        provider="newsapi",
        timespan=None,
        window_hours=24,
        max_records=100,
        language="en",
        search_in="title,description",
        sort_by="publishedAt",
    )


def _settings(**values) -> Settings:
    defaults = {
        "NEWSAPI_ENABLED": True,
        "NEWSAPI_API_KEY": "test-key",
        "ENABLE_MAPPING": False,
    }
    defaults.update(values)
    return Settings(_env_file=None, **defaults)


class FakeNewsApiClient:
    responses: dict[str, list[Article] | Exception] = {}
    calls: list[tuple[str, dict]] = []
    constructed = 0

    def __init__(self, _settings):
        type(self).constructed += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def fetch(self, query: str, **kwargs) -> list[Article]:
        type(self).calls.append((query, kwargs))
        response = type(self).responses.get(query, [])
        if isinstance(response, Exception):
            raise response
        return list(response)


class FakeImageFetcher:
    batches: list[list[str]] = []

    def __init__(self, _settings, repo, **_kwargs):
        self.repo = repo

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def attach_images(self, articles: list[Article]) -> int:
        ids = [article.id for article in articles]
        type(self).batches.append(ids)
        if hasattr(self.repo, "events"):
            self.repo.events.append(("images", ids))
        return len(ids)


class NewsRepo:
    def __init__(self, existing_ids: set[str] | None = None, *, allowed=True):
        self.ids = set(existing_ids or set())
        self.allowed = allowed
        self.reservations: list[tuple] = []
        self.missing_requests: list[list[str]] = []
        self.upserts: list[str] = []
        self.finishes: list[tuple[int, dict]] = []
        self.events: list[tuple] = []

    async def start_bounded_load_run(self, name, query, **kwargs):
        self.reservations.append((name, query, kwargs))
        return len(self.reservations) if self.allowed else None

    async def missing_article_ids(self, article_ids: list[str]) -> set[str]:
        self.missing_requests.append(list(article_ids))
        self.events.append(("missing", list(article_ids)))
        return {article_id for article_id in article_ids if article_id not in self.ids}

    async def upsert_article(self, article: Article) -> str:
        self.events.append(("upsert", article.id))
        self.upserts.append(article.id)
        if article.id in self.ids:
            return "updated"
        self.ids.add(article.id)
        return "inserted"

    async def finish_run(self, run_id: int, **values) -> None:
        self.finishes.append((run_id, values))


@pytest.fixture(autouse=True)
def _reset_fakes():
    FakeNewsApiClient.responses = {}
    FakeNewsApiClient.calls = []
    FakeNewsApiClient.constructed = 0
    FakeImageFetcher.batches = []


@pytest.mark.asyncio
async def test_newsapi_dispatch_filters_existing_ids_and_defers_map_queue(
    monkeypatch,
):
    existing = _article("existing")
    fresh = _article("fresh")
    FakeNewsApiClient.responses = {"AI": [existing, fresh]}
    monkeypatch.setattr(engine_module, "NewsApiClient", FakeNewsApiClient)
    monkeypatch.setattr(engine_module, "ImageFetcher", FakeImageFetcher)
    repo = NewsRepo({existing.id})
    engine = LoaderEngine(
        _settings(ENABLE_MAPPING=True, OPENROUTER_API_KEY="test"),
        repo,
    )
    engine.process_queue = AsyncMock(
        return_value={"claimed": 1, "mapped": 1, "failed": 0}
    )

    result = await engine.run_job(_job())

    assert result.ok is True
    assert result.skipped_reason is None
    assert (result.fetched, result.inserted, result.updated) == (2, 1, 0)
    assert (result.images_stored, result.mapped, result.maps_failed) == (1, 0, 0)
    assert repo.reservations == [
        (
            "newsapi:ai-models-agents",
            "AI",
            {
                "provider_prefix": "newsapi:",
                "max_runs": 225,
                "window_hours": 24.0,
            },
        )
    ]
    assert repo.missing_requests == [[existing.id, fresh.id]]
    assert FakeImageFetcher.batches == [[fresh.id]]
    assert repo.upserts == [fresh.id]
    assert repo.events == [
        ("missing", [existing.id, fresh.id]),
        ("images", [fresh.id]),
        ("upsert", fresh.id),
    ]
    engine.process_queue.assert_not_awaited()
    query, fetch = FakeNewsApiClient.calls[0]
    assert query == "AI"
    assert fetch["end"] - fetch["start"] == timedelta(hours=24)
    assert fetch["start"].tzinfo is not None
    assert fetch | {"start": None, "end": None} == {
        "start": None,
        "end": None,
        "page_size": 100,
        "language": "en",
        "search_in": "title,description",
        "sort_by": "publishedAt",
        "page": 1,
    }
    assert repo.finishes[0][1] == {
        "fetched": 2,
        "inserted": 1,
        "updated": 0,
        "images_stored": 1,
        "ok": True,
        "mapped": 0,
        "maps_failed": 0,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enabled", "key", "budget_allowed", "reason", "reservation_count"),
    (
        (False, "test-key", True, "newsapi_disabled", 0),
        (True, "", True, "newsapi_key_missing", 0),
        (True, "test-key", False, "newsapi_budget_exhausted", 1),
    ),
)
async def test_newsapi_gates_skip_without_network_or_run_mutation(
    monkeypatch,
    enabled,
    key,
    budget_allowed,
    reason,
    reservation_count,
):
    monkeypatch.setattr(engine_module, "NewsApiClient", FakeNewsApiClient)
    repo = NewsRepo(allowed=budget_allowed)
    engine = LoaderEngine(
        Settings(
            _env_file=None,
            NEWSAPI_ENABLED=enabled,
            NEWSAPI_API_KEY=key,
            ENABLE_MAPPING=False,
        ),
        repo,
    )

    result = await engine.run_job(_job())

    assert result.ok is True
    assert result.skipped_reason == reason
    assert FakeNewsApiClient.constructed == 0
    assert len(repo.reservations) == reservation_count
    assert repo.missing_requests == []
    assert repo.upserts == []
    assert repo.finishes == []


@pytest.mark.asyncio
async def test_budget_reservation_failure_makes_no_provider_request(monkeypatch):
    class FailingRepo(NewsRepo):
        async def start_bounded_load_run(self, *_args, **_kwargs):
            raise RuntimeError("budget database unavailable")

    monkeypatch.setattr(engine_module, "NewsApiClient", FakeNewsApiClient)
    result = await LoaderEngine(_settings(), FailingRepo()).run_job(_job())

    assert result.ok is False
    assert result.error == "budget database unavailable"
    assert FakeNewsApiClient.constructed == 0


@pytest.mark.asyncio
async def test_cross_lane_duplicate_is_fetched_twice_but_stored_and_imaged_once(
    monkeypatch,
):
    shared = _article("shared")
    FakeNewsApiClient.responses = {"lane-one": [shared], "lane-two": [shared]}
    monkeypatch.setattr(engine_module, "NewsApiClient", FakeNewsApiClient)
    monkeypatch.setattr(engine_module, "ImageFetcher", FakeImageFetcher)
    repo = NewsRepo()
    engine = LoaderEngine(_settings(), repo)

    results = await engine.run_all(
        [
            _job("newsapi:one", "lane-one"),
            _job("newsapi:two", "lane-two"),
        ]
    )

    assert [result.fetched for result in results] == [1, 1]
    assert [result.inserted for result in results] == [1, 0]
    assert FakeImageFetcher.batches == [[shared.id], []]
    assert repo.upserts == [shared.id]


@pytest.mark.asyncio
async def test_newsapi_failure_is_audited_and_later_provider_job_still_runs(
    monkeypatch,
):
    FakeNewsApiClient.responses = {
        "bad": NewsApiTransientError("server_error", 503),
        "good": [],
    }
    monkeypatch.setattr(engine_module, "NewsApiClient", FakeNewsApiClient)
    monkeypatch.setattr(engine_module, "ImageFetcher", FakeImageFetcher)
    repo = NewsRepo()
    engine = LoaderEngine(_settings(), repo)

    results = await engine.run_all(
        [_job("newsapi:bad", "bad"), _job("newsapi:good", "good")]
    )

    assert [result.ok for result in results] == [False, True]
    assert results[0].error == "newsapi_server_error"
    assert [query for query, _kwargs in FakeNewsApiClient.calls] == ["bad", "good"]
    assert len(repo.finishes) == 2
    assert repo.finishes[0][1]["ok"] is False
    assert repo.finishes[1][1]["ok"] is True


@pytest.mark.asyncio
async def test_unknown_provider_fails_before_database_or_network():
    job = Job(name="bad", query="AI", provider="unknown")  # type: ignore[arg-type]
    repo = NewsRepo()

    with pytest.raises(ValueError, match="unsupported job provider"):
        await LoaderEngine(_settings(), repo).run_job(job)

    assert repo.reservations == []
    assert FakeNewsApiClient.constructed == 0


class _MissingPool:
    def __init__(self):
        self.calls = []

    async def fetch(self, query, *args):
        self.calls.append((query, args))
        return [{"id": "new"}]


@pytest.mark.asyncio
async def test_repository_missing_ids_uses_one_deduplicated_batch_query():
    pool = _MissingPool()
    repo = Repository(SimpleNamespace(pool=pool))

    missing = await repo.missing_article_ids(["existing", "new", "new", ""])

    assert missing == {"new"}
    assert len(pool.calls) == 1
    query, args = pool.calls[0]
    assert "unnest($1::text[])" in query
    assert "LEFT JOIN articles" in query
    assert "stored.id IS NULL" in query
    assert args == (["existing", "new"],)


@pytest.mark.asyncio
async def test_repository_missing_ids_skips_empty_batch_roundtrip():
    pool = _MissingPool()
    repo = Repository(SimpleNamespace(pool=pool))

    assert await repo.missing_article_ids([]) == set()
    assert pool.calls == []
