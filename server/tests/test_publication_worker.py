from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from app.config import Settings
from app.publication_worker import PublicationScheduleWorker


class WorkerRepo:
    def __init__(self, outcomes=None):
        self.rows = [{"id": uuid4()} for _ in (outcomes or [])]
        self.outcomes = list(outcomes or [])
        self.claims = 0
        self.retries: list[tuple] = []

    async def claim_due_editorial_schedules(self, *, limit, lease_seconds):
        self.claims += 1
        rows, self.rows = self.rows[:limit], self.rows[limit:]
        return rows

    async def publish_claimed_editorial_schedule(self, schedule_id):
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return {"kind": outcome}

    async def retry_editorial_schedule(
        self, schedule_id, *, error_code, max_attempts
    ):
        self.retries.append((schedule_id, error_code, max_attempts))
        return {"kind": "failed" if len(self.retries) > 1 else "retry"}


@pytest.mark.asyncio
async def test_run_once_isolates_rows_and_uses_bounded_retry():
    repo = WorkerRepo(["published", RuntimeError("db secret"), "conflict"])
    worker = PublicationScheduleWorker(
        Settings(_env_file=None, PUBLICATION_MAX_ATTEMPTS=5), repo
    )

    result = await worker.run_once()

    assert result == {
        "claimed": 3,
        "published": 1,
        "retried": 1,
        "failed": 1,
        "skipped": 0,
    }
    assert [item[1:] for item in repo.retries] == [
        ("publication_unavailable", 5),
        ("publication_conflict", 5),
    ]


@pytest.mark.asyncio
async def test_terminal_schedule_is_never_retried():
    repo = WorkerRepo(["schedule_terminal", "not_found"])
    worker = PublicationScheduleWorker(Settings(_env_file=None), repo)

    result = await worker.run_once()

    assert result["skipped"] == 2
    assert repo.retries == []


@pytest.mark.asyncio
async def test_start_runs_immediately_and_stop_is_idempotent():
    repo = WorkerRepo([])
    settings = Settings(
        _env_file=None,
        PUBLICATION_POLL_SECONDS=1,
        PUBLICATION_WORKER_ENABLED=True,
    )
    worker = PublicationScheduleWorker(settings, repo)

    assert worker.start() is True
    assert worker.start() is False
    for _ in range(20):
        if repo.claims:
            break
        await asyncio.sleep(0)
    assert repo.claims == 1
    await worker.stop()
    await worker.stop()


@pytest.mark.asyncio
async def test_disabled_worker_creates_no_task_or_db_read():
    repo = WorkerRepo([])
    worker = PublicationScheduleWorker(
        Settings(_env_file=None, PUBLICATION_WORKER_ENABLED=False), repo
    )
    assert worker.start() is False
    await worker.stop()
    assert repo.claims == 0


def test_publication_worker_settings_are_bounded():
    settings = Settings(_env_file=None)
    assert settings.publication_worker_enabled is True
    assert settings.publication_poll_seconds == 15
    assert settings.publication_max_attempts == 5
    for values in (
        {"PUBLICATION_POLL_SECONDS": 0},
        {"PUBLICATION_POLL_SECONDS": 61},
        {"PUBLICATION_MAX_ATTEMPTS": 0},
        {"PUBLICATION_MAX_ATTEMPTS": 11},
    ):
        with pytest.raises(ValueError):
            Settings(_env_file=None, **values)
