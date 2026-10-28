"""Startup and scheduler wiring for the persistent Map queue."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

import app.api as api_module
import app.cli as cli_module
import app.scheduler as scheduler_module
from app.config import Settings
from app.scheduler import RefreshScheduler


class FakeDatabase:
    events: list[tuple] = []

    def __init__(self, _settings):
        self.pool = object()

    async def connect(self):
        self.events.append(("db_connect",))

    async def ensure_schema(self):
        self.events.append(("schema",))

    async def close(self):
        self.events.append(("db_close",))


class FakeRepository:
    events: list[tuple] = []

    def __init__(self, _db):
        pass

    async def reconcile_map_queue(self, *, max_attempts):
        self.events.append(("reconcile", max_attempts))
        return 0


class FakeTelemetry:
    events: list[tuple] = []

    def __init__(self, _settings, _repo):
        self.instance_id = "fake-telemetry-owner"

    async def start(self):
        self.events.append(("telemetry_start",))

    async def stop(self):
        self.events.append(("telemetry_stop",))


class FakeScheduler:
    events: list[tuple] = []

    def __init__(self, _settings, _engine, _primary_document_worker=None):
        pass

    def start(self):
        self.events.append(("scheduler_start",))

    def shutdown(self):
        self.events.append(("scheduler_stop",))


class FakeMapWorker:
    events: list[tuple] = []

    def __init__(self, _settings, _engine, *, lease_owner=None):
        self.events.append(("map_worker_owner", lease_owner))

    def start(self):
        self.events.append(("map_worker_start",))
        return True

    async def stop(self):
        self.events.append(("map_worker_stop",))


class FakePublicationWorker:
    events: list[tuple] = []

    def __init__(self, _settings, _repo):
        pass

    def start(self):
        self.events.append(("publication_worker_start",))
        return True

    async def stop(self):
        self.events.append(("publication_worker_stop",))


class FakePrimaryDocumentWorker:
    events: list[tuple] = []

    def __init__(self, _settings, _repo):
        pass

    async def close(self):
        self.events.append(("primary_document_worker_close",))


class FakeArticleEmbeddingWorker:
    events: list[tuple] = []

    def __init__(self, _settings, _repo):
        pass

    async def start(self):
        self.events.append(("article_embedding_start",))
        return True

    async def stop(self):
        self.events.append(("article_embedding_stop",))


class FakeAPNsProvider:
    events: list[tuple] = []

    def __init__(self, _settings):
        self.events.append(("apns_provider_init",))

    async def close(self):
        self.events.append(("apns_provider_close",))


class FakePushWorker:
    events: list[tuple] = []

    def __init__(self, _settings, _repo, _provider, _cipher):
        pass

    def start(self):
        self.events.append(("push_worker_start",))
        return True

    async def stop(self):
        self.events.append(("push_worker_stop",))


def _bind_events(events):
    FakeDatabase.events = events
    FakeRepository.events = events
    FakeTelemetry.events = events
    FakeScheduler.events = events
    FakeMapWorker.events = events
    FakePublicationWorker.events = events
    FakePrimaryDocumentWorker.events = events
    FakeArticleEmbeddingWorker.events = events
    FakeAPNsProvider.events = events
    FakePushWorker.events = events


@pytest.mark.asyncio
async def test_api_startup_reconciles_before_workers(monkeypatch):
    events: list[tuple] = []
    _bind_events(events)
    settings = Settings(
        _env_file=None,
        APP_ATTEST_ENABLED=False,
        SCHEDULER_ENABLED=False,
        MAP_MAX_ATTEMPTS=5,
    )
    monkeypatch.setattr(api_module, "get_settings", lambda: settings)
    monkeypatch.setattr(api_module, "Database", FakeDatabase)
    monkeypatch.setattr(api_module, "Repository", FakeRepository)
    monkeypatch.setattr(api_module, "WorkerTelemetry", FakeTelemetry)
    monkeypatch.setattr(api_module, "MapQueueWorker", FakeMapWorker)
    monkeypatch.setattr(
        api_module, "PublicationScheduleWorker", FakePublicationWorker
    )
    monkeypatch.setattr(
        api_module, "ArticleEmbeddingWorker", FakeArticleEmbeddingWorker
    )
    monkeypatch.setattr(api_module, "RefreshScheduler", FakeScheduler)
    monkeypatch.setattr(
        api_module, "PrimaryDocumentWorker", FakePrimaryDocumentWorker
    )

    async with api_module.lifespan(api_module.app):
        assert events[:9] == [
            ("db_connect",),
            ("schema",),
            ("reconcile", 5),
            ("article_embedding_start",),
            ("telemetry_start",),
            ("map_worker_owner", "fake-telemetry-owner"),
            ("map_worker_start",),
            ("publication_worker_start",),
            ("scheduler_start",),
        ]

    assert events[-7:] == [
        ("scheduler_stop",),
        ("primary_document_worker_close",),
        ("publication_worker_stop",),
        ("map_worker_stop",),
        ("article_embedding_stop",),
        ("telemetry_stop",),
        ("db_close",),
    ]


@pytest.mark.asyncio
async def test_api_lifespan_creates_and_closes_apns_only_when_explicitly_enabled(
    monkeypatch,
):
    events: list[tuple] = []
    _bind_events(events)
    settings = Settings(
        _env_file=None,
        APP_ATTEST_ENABLED=False,
        SCHEDULER_ENABLED=False,
        APPLE_SIGN_IN_ENABLED=True,
        APPLE_SIGN_IN_CLIENT_ID="com.example.textshorts",
        APPLE_SIGN_IN_TEAM_ID="TEAM123456",
        APPLE_SIGN_IN_KEY_ID="SIGNINKEY",
        APPLE_SIGN_IN_PRIVATE_KEY="provider-key-is-mocked",
        APPLE_TOKEN_ENCRYPTION_KEY=Fernet.generate_key().decode(),
        PUSH_NOTIFICATIONS_ENABLED=True,
        APNS_TEAM_ID="TEAM123456",
        APNS_KEY_ID="APNSKEY123",
        APNS_PRIVATE_KEY="apns-key-is-mocked",
        APNS_TOPIC="com.example.textshorts",
    )
    monkeypatch.setattr(api_module, "get_settings", lambda: settings)
    monkeypatch.setattr(api_module, "Database", FakeDatabase)
    monkeypatch.setattr(api_module, "Repository", FakeRepository)
    monkeypatch.setattr(api_module, "WorkerTelemetry", FakeTelemetry)
    monkeypatch.setattr(api_module, "MapQueueWorker", FakeMapWorker)
    monkeypatch.setattr(api_module, "PublicationScheduleWorker", FakePublicationWorker)
    monkeypatch.setattr(
        api_module, "ArticleEmbeddingWorker", FakeArticleEmbeddingWorker
    )
    monkeypatch.setattr(api_module, "RefreshScheduler", FakeScheduler)
    monkeypatch.setattr(
        api_module, "PrimaryDocumentWorker", FakePrimaryDocumentWorker
    )
    monkeypatch.setattr(api_module, "AppleIdentityProvider", lambda *_args: object())
    monkeypatch.setattr(api_module, "APNsProvider", FakeAPNsProvider)
    monkeypatch.setattr(api_module, "EditorialPushWorker", FakePushWorker)

    async with api_module.lifespan(api_module.app):
        assert ("apns_provider_init",) in events
        assert events.index(("publication_worker_start",)) < events.index(
            ("push_worker_start",)
        )

    assert events.index(("push_worker_stop",)) < events.index(
        ("publication_worker_stop",)
    )
    assert events.index(("apns_provider_close",)) < events.index(("db_close",))


@pytest.mark.asyncio
async def test_cli_reconciles_before_drain(monkeypatch, capsys):
    events: list[tuple] = []
    _bind_events(events)
    settings = Settings(
        _env_file=None,
        ENABLE_MAPPING=False,
        MAP_MAX_ATTEMPTS=6,
    )

    class FakeEngine:
        def __init__(self, _settings, _repo, _telemetry, image_rag=None):
            self.image_rag = image_rag

        async def sync_image_rag(self):
            events.append(("rag_sync",))
            pass

        async def process_queue(self, limit):
            events.append(("drain", limit))
            return {"claimed": 0, "mapped": 0, "failed": 0}

    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    monkeypatch.setattr(cli_module, "Database", FakeDatabase)
    monkeypatch.setattr(cli_module, "Repository", FakeRepository)
    monkeypatch.setattr(cli_module, "WorkerTelemetry", FakeTelemetry)
    monkeypatch.setattr(cli_module, "LoaderEngine", FakeEngine)
    monkeypatch.setattr(cli_module, "configure_logging", lambda _level: None)
    monkeypatch.setattr(sys, "argv", ["app.cli", "--drain-queue", "1"])

    await cli_module._main()

    assert events[:6] == [
        ("db_connect",),
        ("schema",),
        ("reconcile", 6),
        ("telemetry_start",),
        ("rag_sync",),
        ("drain", 1),
    ]
    assert events[-2:] == [("telemetry_stop",), ("db_close",)]
    assert '"claimed": 0' in capsys.readouterr().out


@pytest.mark.asyncio
async def test_scheduler_tick_only_runs_ingestion_work(monkeypatch):
    events: list[tuple] = []

    class Engine:
        async def sync_image_rag(self):
            events.append(("rag_sync",))

        async def run_all(self, jobs):
            events.append(("run_all", len(jobs)))
            return []

        async def process_queue(self):
            raise AssertionError("Map work belongs to the independent worker")

    monkeypatch.setattr(scheduler_module, "load_jobs", lambda _path: [object()])
    scheduler = RefreshScheduler(Settings(_env_file=None), Engine())

    await scheduler._tick()
    assert events == [("rag_sync",), ("run_all", 1)]


@pytest.mark.asyncio
async def test_scheduler_still_runs_jobs_when_rag_sync_fails(monkeypatch):
    events: list[tuple] = []

    class Engine:
        async def sync_image_rag(self):
            events.append(("rag_sync",))
            raise RuntimeError("vector DSN must stay redacted")

        async def run_all(self, jobs):
            events.append(("run_all", len(jobs)))
            return []

    monkeypatch.setattr(scheduler_module, "load_jobs", lambda _path: [object()])
    scheduler = RefreshScheduler(Settings(_env_file=None), Engine())

    await scheduler._tick()
    assert events == [("rag_sync",), ("run_all", 1)]


@pytest.mark.asyncio
async def test_scheduler_runs_primary_documents_after_jobs_and_isolates_failure(
    monkeypatch,
):
    events: list[tuple] = []

    class Engine:
        async def sync_image_rag(self):
            events.append(("rag_sync",))

        async def run_all(self, jobs):
            events.append(("run_all", len(jobs)))
            return []

    class FailingPrimaryDocuments:
        async def run_once(self):
            events.append(("primary_documents",))
            raise RuntimeError("one source failed")

    monkeypatch.setattr(scheduler_module, "load_jobs", lambda _path: [object()])
    scheduler = RefreshScheduler(
        Settings(_env_file=None),
        Engine(),
        FailingPrimaryDocuments(),
    )

    await scheduler._tick()

    assert events == [
        ("rag_sync",),
        ("run_all", 1),
        ("primary_documents",),
    ]
    assert scheduler._running is False


def test_readme_describes_full_queue_lifecycle_and_bounded_parallel_dispatch():
    readme = Path(__file__).parents[1].joinpath("README.md").read_text("utf-8")

    assert "`queued/processing/done/dead`" in readme
    assert "`MAP_PROCESSING_LEASE_S`" in readme
    assert "허용 범위와 hard max는 1~7" in readme
    assert "`MAP_REQUESTS_PER_MINUTE`" in readme
    assert "독립 `MapQueueWorker`" in readme
