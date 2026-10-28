from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from app.article_embedding_worker import ArticleEmbeddingWorker
from app.article_vector_store import (
    ArticleEmbeddingCursor,
    ArticleEmbeddingProgress,
    ArticleVectorHit,
    ArticleVectorWorkItem,
)
from app.config import Settings
from app.openrouter import OpenRouterPermanentError, OpenRouterTransientError


NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def _settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "VECTOR_DATABASE_URL": "postgresql://vector.internal/articles",
        "OPENROUTER_API_KEY": "test-key",
        "ARTICLE_EMBEDDING_POLL_SECONDS": 5,
    }
    values.update(overrides)
    return Settings(**values)


class FakeRepo:
    def __init__(self, changes=None, *, total=3, count_error: Exception | None = None):
        self.changes = list(changes or [])
        self.total = total
        self.count_error = count_error
        self.count_calls = 0
        self.calls = []

    async def list_article_embedding_changes(self, **kwargs):
        self.calls.append(kwargs)
        return list(self.changes)

    async def available_article_count(self):
        self.count_calls += 1
        if self.count_error:
            raise self.count_error
        return self.total


class FakeVector:
    def __init__(self, work=None, *, mark_results=None, progress_error=None, hits=None):
        self.work = list(work or [])
        self.mark_results = list(mark_results or [])
        self.progress_error = progress_error
        self.cursor = ArticleEmbeddingCursor()
        self.targets = []
        self.indexed = []
        self.retried = []
        self.dead = []
        self.started = 0
        self.closed = 0
        self.hits = list(hits or [])
        self.nearest_calls = []

    async def start(self):
        self.started += 1
        return True

    async def close(self):
        self.closed += 1

    async def get_cursor(self):
        return self.cursor

    async def enqueue_changes(self, targets, cursor):
        self.targets.extend(targets)
        self.cursor = cursor
        return len(targets)

    async def claim_due(self, limit):
        claimed, self.work = self.work[:limit], self.work[limit:]
        return claimed

    async def mark_indexed(self, article_id, source_hash, embedding, *, total_articles):
        self.indexed.append((article_id, source_hash, len(embedding), total_articles))
        if self.mark_results:
            return self.mark_results.pop(0)
        return ArticleEmbeddingProgress(1, total_articles, 100 / total_articles, NOW)

    async def mark_retry(self, article_ids, code):
        self.retried.append((list(article_ids), code))

    async def mark_dead(self, article_ids, code):
        self.dead.append((list(article_ids), code))

    async def progress(self, total_articles):
        if self.progress_error:
            raise self.progress_error
        return ArticleEmbeddingProgress(1, total_articles, 100 / total_articles, NOW)

    async def nearest_articles(self, embedding, **kwargs):
        self.nearest_calls.append((list(embedding), kwargs))
        return list(self.hits)


class FakeEmbeddings:
    def __init__(self, error: Exception | None = None, *, text_delay: float = 0):
        self.error = error
        self.calls = []
        self.opened = 0
        self.closed = 0
        self.text_calls = []
        self.text_delay = text_delay

    async def __aenter__(self):
        self.opened += 1
        return self

    async def __aexit__(self, *_args):
        self.closed += 1

    async def embed_documents(self, texts):
        self.calls.append(list(texts))
        if self.error:
            raise self.error
        return [[float(index)] * 768 for index in range(len(texts))]

    async def embed_texts(self, texts):
        self.text_calls.append(list(texts))
        if self.text_delay:
            await asyncio.sleep(self.text_delay)
        if self.error:
            raise self.error
        return [[0.25] * 768 for _ in texts]


def _work(article: str, attempts: int = 1) -> ArticleVectorWorkItem:
    return ArticleVectorWorkItem(article, article[-1] * 64, f"headline: {article}", attempts)


@pytest.mark.asyncio
async def test_c11600_query_embedding_and_nearest_use_bounded_search_contract():
    vector = FakeVector(hits=[ArticleVectorHit("b" * 64, 0.8)])
    embeddings = FakeEmbeddings()
    worker = ArticleEmbeddingWorker(
        _settings(
            ARTICLE_SEARCH_RAG_LIMIT=5,
            ARTICLE_SEARCH_RAG_MIN_SIMILARITY=0.45,
        ),
        FakeRepo(),
        vector_store=vector,
        embeddings=embeddings,
    )
    await worker.sync()

    query_vector = await worker.embed_search_query("  ＡＩ   인프라  ")
    hits = await worker.nearest_search_articles(
        query_vector or [],
        exclude_ids=["a" * 64, "a" * 64],
    )

    assert embeddings.text_calls == [["AI 인프라"]]
    assert query_vector == [0.25] * 768
    assert hits == [ArticleVectorHit("b" * 64, 0.8)]
    assert vector.nearest_calls == [
        (
            [0.25] * 768,
            {
                "exclude_ids": ["a" * 64, "a" * 64],
                "limit": 5,
                "min_similarity": 0.45,
            },
        )
    ]


@pytest.mark.asyncio
async def test_c11600_query_embedding_disabled_unready_timeout_and_failure_fail_open():
    disabled = ArticleEmbeddingWorker(
        _settings(ARTICLE_SEARCH_RAG_ENABLED=False),
        FakeRepo(),
        vector_store=FakeVector(),
        embeddings=FakeEmbeddings(),
    )
    assert await disabled.embed_search_query("AI") is None

    unready = ArticleEmbeddingWorker(
        _settings(), FakeRepo(), vector_store=FakeVector(), embeddings=FakeEmbeddings()
    )
    assert await unready.embed_search_query("AI") is None

    slow = ArticleEmbeddingWorker(
        _settings(ARTICLE_SEARCH_RAG_TIMEOUT_SECONDS=0.05),
        FakeRepo(),
        vector_store=FakeVector(),
        embeddings=FakeEmbeddings(text_delay=0.1),
    )
    await slow.sync()
    assert await slow.embed_search_query("AI") is None

    failed = ArticleEmbeddingWorker(
        _settings(),
        FakeRepo(),
        vector_store=FakeVector(),
        embeddings=FakeEmbeddings(OpenRouterTransientError("query secret")),
    )
    await failed.sync()
    assert await failed.embed_search_query("AI") is None
    assert await failed.nearest_search_articles(
        [0.1] * 768,
        exclude_ids=[],
    ) == []


@pytest.mark.asyncio
async def test_c10007_sync_indexes_documents_advances_cursor_and_records_success():
    changes = [
        {
            "article_id": "a" * 64,
            "map_status": "ok",
            "map_data": {"headline": "기사 A"},
            "eligible": True,
            "changed_at": NOW,
        },
        {
            "article_id": "b" * 64,
            "map_status": "failed",
            "map_data": None,
            "eligible": False,
            "changed_at": NOW,
        },
    ]
    progress = ArticleEmbeddingProgress(2, 3, 66.7, NOW)
    vector = FakeVector(
        [_work("a" * 64), _work("c" * 64)],
        mark_results=[progress, None],
    )
    embeddings = FakeEmbeddings()
    worker = ArticleEmbeddingWorker(
        _settings(), FakeRepo(changes, total=3), vector_store=vector,
        embeddings=embeddings,
    )

    stats = await worker.sync()
    await worker.stop()

    assert (stats.discovered, stats.claimed, stats.indexed) == (2, 2, 1)
    assert stats.progress == progress
    assert vector.cursor == ArticleEmbeddingCursor(NOW, "b" * 64)
    assert vector.targets[0].eligible is True
    assert vector.targets[1].eligible is False
    assert len(embeddings.calls) == 1 and len(embeddings.calls[0]) == 2
    assert all(item[-1] == 3 for item in vector.indexed)
    assert vector.started == 1 and vector.closed == 1


@pytest.mark.asyncio
async def test_c10007_transient_retries_below_limit_and_deads_at_limit():
    vector = FakeVector([_work("a" * 64, 1), _work("b" * 64, 5)])
    worker = ArticleEmbeddingWorker(
        _settings(ARTICLE_EMBEDDING_MAX_ATTEMPTS=5),
        FakeRepo(total=2),
        vector_store=vector,
        embeddings=FakeEmbeddings(OpenRouterTransientError("provider secret")),
    )

    stats = await worker.sync()

    assert (stats.retried, stats.dead, stats.indexed) == (1, 1, 0)
    assert vector.retried == [(["a" * 64], "embedding_transient")]
    assert vector.dead == [(["b" * 64], "embedding_transient")]


@pytest.mark.asyncio
async def test_c10007_permanent_error_deads_batch_without_progress():
    vector = FakeVector([_work("a" * 64), _work("b" * 64)])
    worker = ArticleEmbeddingWorker(
        _settings(), FakeRepo(total=2), vector_store=vector,
        embeddings=FakeEmbeddings(OpenRouterPermanentError("denied")),
    )

    stats = await worker.sync()

    assert stats.indexed == 0 and stats.dead == 2
    assert vector.dead == [(["a" * 64, "b" * 64], "embedding_permanent")]
    assert vector.indexed == []


@pytest.mark.asyncio
async def test_c10007_main_count_failure_skips_provider_and_fails_open():
    embeddings = FakeEmbeddings()
    vector = FakeVector([_work("a" * 64)])
    worker = ArticleEmbeddingWorker(
        _settings(),
        FakeRepo(total=2, count_error=RuntimeError("main dsn secret")),
        vector_store=vector,
        embeddings=embeddings,
    )

    stats = await worker.sync()

    assert stats.error_code == "sync_failed"
    assert embeddings.calls == []
    assert vector.retried == [(["a" * 64], "main_count_unavailable")]


@pytest.mark.asyncio
async def test_c10007_no_due_work_skips_main_count_and_provider():
    repo = FakeRepo(total=4)
    embeddings = FakeEmbeddings()
    worker = ArticleEmbeddingWorker(
        _settings(),
        repo,
        vector_store=FakeVector(),
        embeddings=embeddings,
    )

    stats = await worker.sync()

    assert stats.claimed == 0
    assert repo.count_calls == 0
    assert embeddings.calls == []


@pytest.mark.asyncio
async def test_c10008_progress_ready_disabled_and_unavailable_are_bounded():
    ready = ArticleEmbeddingWorker(
        _settings(), FakeRepo(total=4), vector_store=FakeVector(),
        embeddings=FakeEmbeddings(),
    )
    assert await ready.progress(4) == {
        "status": "ready",
        "embedded_count": 1,
        "total_count": 4,
        "percent": 25.0,
        "updated_at": NOW,
    }

    disabled = ArticleEmbeddingWorker(
        _settings(ARTICLE_EMBEDDING_ENABLED=False),
        FakeRepo(total=4), vector_store=FakeVector(), embeddings=FakeEmbeddings(),
    )
    assert await disabled.progress(4) == {
        "status": "disabled",
        "embedded_count": None,
        "total_count": None,
        "percent": None,
        "updated_at": None,
    }

    unavailable = ArticleEmbeddingWorker(
        _settings(), FakeRepo(total=4),
        vector_store=FakeVector(progress_error=RuntimeError("vector secret")),
        embeddings=FakeEmbeddings(),
    )
    assert await unavailable.progress(4) == {
        "status": "unavailable",
        "embedded_count": None,
        "total_count": None,
        "percent": None,
        "updated_at": None,
    }


@pytest.mark.asyncio
async def test_c10008_progress_rounds_half_up_consistently():
    worker = ArticleEmbeddingWorker(
        _settings(),
        FakeRepo(total=80),
        vector_store=FakeVector(),
        embeddings=FakeEmbeddings(),
    )

    progress = await worker.progress(80)

    assert progress["embedded_count"] == 1
    assert progress["total_count"] == 80
    assert progress["percent"] == 1.3


@pytest.mark.asyncio
async def test_c10007_worker_loop_uses_configured_poll_and_stops_cleanly(
    monkeypatch,
):
    worker = ArticleEmbeddingWorker(
        _settings(ARTICLE_EMBEDDING_POLL_SECONDS=7),
        FakeRepo(),
        vector_store=FakeVector(),
        embeddings=FakeEmbeddings(),
    )
    sync_calls = 0
    timeouts = []

    async def sync_once():
        nonlocal sync_calls
        sync_calls += 1
        worker._stop_event.set()

    async def record_wait(awaitable, *, timeout):
        timeouts.append(timeout)
        return await awaitable

    monkeypatch.setattr(worker, "sync", sync_once)
    monkeypatch.setattr("app.article_embedding_worker.asyncio.wait_for", record_wait)

    await worker._run()

    assert sync_calls == 1
    assert timeouts == [7.0]
