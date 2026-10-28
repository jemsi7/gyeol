"""Worker telemetry schema, aggregation and repository contract tests."""

from datetime import UTC, datetime, timedelta

import pytest

from app.config import Settings
from app.db import SCHEMA_SQL
from app.ops import build_worker_metrics
from app.repository import Repository
from app.telemetry import WorkerTelemetry


NOW = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)


def _instance(
    instance_id: str,
    *,
    started_minutes_ago: int,
    seen_seconds_ago: int = 0,
    stopped_at=None,
    capacities=None,
):
    return {
        "instance_id": instance_id,
        "started_at": NOW - timedelta(minutes=started_minutes_ago),
        "last_seen_at": NOW - timedelta(seconds=seen_seconds_ago),
        "stopped_at": stopped_at,
        "capacities": capacities
        or {"ingest": 1, "image": 2, "llm_map": 2, "retry_drain": 1},
    }


def _activity(instance_id: str, kind: str, start_minutes_ago: int, finished_at=None):
    return {
        "instance_id": instance_id,
        "worker_kind": kind,
        "started_at": NOW - timedelta(minutes=start_minutes_ago),
        "finished_at": finished_at,
    }


def test_ops_settings_and_schema_contract():
    settings = Settings(_env_file=None)
    assert settings.ops_window_minutes == 15
    assert settings.ops_heartbeat_seconds == 5
    assert settings.ops_stale_after_seconds == 20
    assert settings.ops_telemetry_retention_hours == 24
    assert "CREATE TABLE IF NOT EXISTS worker_instances" in SCHEMA_SQL
    assert "CREATE TABLE IF NOT EXISTS worker_activity" in SCHEMA_SQL
    assert "'retry_drain'" in SCHEMA_SQL
    assert "idx_worker_activity_open" in SCHEMA_SQL


def test_worker_metrics_calculate_current_and_rolling_utilization():
    instances = [_instance("live", started_minutes_ago=15)]
    activities = [
        _activity("live", "llm_map", 5),
        _activity("live", "llm_map", 10, NOW - timedelta(minutes=5)),
    ]

    metrics = build_worker_metrics(
        now=NOW,
        instances=instances,
        activities=activities,
        window_minutes=15,
        stale_after_s=20,
    )

    llm = next(row for row in metrics if row["kind"] == "llm_map")
    assert llm["active_slots"] == 1
    assert llm["capacity"] == 2
    assert llm["current_percent"] == 50.0
    assert llm["busy_seconds"] == 600.0
    assert llm["capacity_seconds"] == 1800.0
    assert llm["utilization_percent"] == 33.3


def test_stale_instance_is_historical_only_and_activity_caps_at_last_heartbeat():
    instances = [
        _instance("live", started_minutes_ago=15),
        _instance(
            "stale",
            started_minutes_ago=10,
            seen_seconds_ago=120,
            capacities={"image": 1},
        ),
    ]
    activities = [_activity("stale", "image", 5)]

    metrics = build_worker_metrics(
        now=NOW,
        instances=instances,
        activities=activities,
        window_minutes=15,
        stale_after_s=20,
    )

    image = next(row for row in metrics if row["kind"] == "image")
    assert image["active_slots"] == 0
    assert image["capacity"] == 2
    assert image["current_percent"] == 0.0
    assert image["busy_seconds"] == 180.0
    assert image["capacity_seconds"] == 2280.0
    assert image["utilization_percent"] == 7.9


def test_worker_metrics_return_all_kinds_with_zero_capacity():
    metrics = build_worker_metrics(now=NOW, instances=[], activities=[])
    assert [row["kind"] for row in metrics] == [
        "ingest",
        "image",
        "llm_map",
        "retry_drain",
    ]
    assert all(row["current_percent"] == 0 for row in metrics)
    assert all(row["utilization_percent"] == 0 for row in metrics)


class FakePool:
    def __init__(self, fetchval_result=0):
        self.query = ""
        self.args = ()
        self.executions = []
        self.fetchval_result = fetchval_result

    async def fetch(self, query, *args):
        self.query = query
        self.args = args
        return []

    async def fetchval(self, query, *args):
        self.query = query
        self.args = args
        return self.fetchval_result

    async def execute(self, query, *args):
        self.executions.append((query, args))


class FakeDB:
    def __init__(self):
        self.pool = FakePool()


@pytest.mark.asyncio
async def test_queue_items_join_article_context_and_apply_status_filter():
    db = FakeDB()
    repo = Repository(db)

    assert await repo.queue_items(25, "dead") == []
    assert "JOIN articles" in db.pool.query
    assert "q.status = $2" in db.pool.query
    assert db.pool.args == (25, "dead")


@pytest.mark.asyncio
async def test_available_article_count_uses_feed_eligibility_contract():
    db = FakeDB()
    db.pool.fetchval_result = 37
    repo = Repository(db)

    assert await repo.available_article_count() == 37
    assert "JOIN article_maps" in db.pool.query
    assert "LEFT JOIN editorial_publications" in db.pool.query
    assert "m.status IN ('ok', 'degraded')" in db.pool.query
    assert "m.data IS NOT NULL" in db.pool.query
    assert "p.draft_id IS NULL OR p.state = 'published'" in db.pool.query
    assert db.pool.args == ()


@pytest.mark.asyncio
async def test_worker_telemetry_cleanup_binds_timedelta_interval():
    db = FakeDB()
    repo = Repository(db)

    await repo.worker_telemetry_cleanup(24)

    assert len(db.pool.executions) == 2
    assert all(args == (timedelta(hours=24),) for _, args in db.pool.executions)
    assert all("$1::interval" in query for query, _ in db.pool.executions)


class FakeTelemetryRepo:
    def __init__(self, fail_registration: bool = False):
        self.fail_registration = fail_registration
        self.events = []
        self.next_id = 10

    async def worker_instance_start(self, instance_id, capacities):
        if self.fail_registration:
            raise RuntimeError("telemetry unavailable")
        self.events.append(("instance_start", instance_id, capacities))

    async def worker_telemetry_cleanup(self, retention_hours):
        self.events.append(("cleanup", retention_hours))

    async def worker_instance_heartbeat(self, instance_id):
        self.events.append(("heartbeat", instance_id))

    async def worker_activity_start(self, instance_id, worker_kind, work_key):
        self.next_id += 1
        self.events.append(("activity_start", self.next_id, worker_kind, work_key))
        return self.next_id

    async def worker_activity_finish(self, activity_id, outcome):
        self.events.append(("activity_finish", activity_id, outcome))

    async def worker_instance_stop(self, instance_id):
        self.events.append(("instance_stop", instance_id))


@pytest.mark.asyncio
async def test_worker_telemetry_records_lifecycle_success_and_failure():
    repo = FakeTelemetryRepo()
    settings = Settings(
        _env_file=None,
        OPENROUTER_API_KEY="test-key",
        IMAGE_CONCURRENCY=4,
        MAP_CONCURRENCY=2,
    )
    telemetry = WorkerTelemetry(settings, repo)

    await telemetry.start()
    async with telemetry.activity("image", "https://example.test/image.jpg"):
        pass
    with pytest.raises(RuntimeError, match="worker failed"):
        async with telemetry.activity("llm_map", "article-1"):
            raise RuntimeError("worker failed")
    await telemetry.stop()

    assert repo.events[0][0] == "instance_start"
    capacities = repo.events[0][2]
    assert capacities["image"] == 4
    assert capacities["llm_map"] == 2
    assert ("activity_finish", 11, "success") in repo.events
    assert ("activity_finish", 12, "failed") in repo.events
    assert repo.events[-1][0] == "instance_stop"


def test_worker_telemetry_exposes_map_capacity_for_cerebras_only():
    telemetry = WorkerTelemetry(
        Settings(
            _env_file=None,
            ENABLE_MAPPING=True,
            CEREBRAS_API_KEY="test-key",
            MAP_CONCURRENCY=3,
        ),
        FakeTelemetryRepo(),
    )

    assert telemetry.capacities["llm_map"] == 3
    assert telemetry.capacities["retry_drain"] == 1
    assert telemetry.capacities["curation"] == 0
    assert telemetry.capacities["triage"] == 0


@pytest.mark.asyncio
async def test_worker_telemetry_registration_failure_does_not_break_work():
    repo = FakeTelemetryRepo(fail_registration=True)
    telemetry = WorkerTelemetry(Settings(_env_file=None), repo)
    reached = False

    async with telemetry.activity("ingest", "job"):
        reached = True

    assert reached is True
    assert repo.events == []
