"""Append-only Map attempt telemetry and redaction contracts."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.db import SCHEMA_SQL
from app.editorial_pipeline import EditorialPipeline
from app.repository import Repository


class RecordingPool:
    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    async def execute(self, query, *args):
        self.calls.append((query, args))
        return "INSERT 0 1"


def test_attempt_event_schema_is_append_oriented_and_indexed():
    assert "CREATE TABLE IF NOT EXISTS map_attempt_events" in SCHEMA_SQL
    assert "idx_map_attempt_events_article_time" in SCHEMA_SQL
    assert "idx_map_attempt_events_stage_code_time" in SCHEMA_SQL
    assert "'started','cached','succeeded','failed'" in SCHEMA_SQL


@pytest.mark.asyncio
async def test_repository_redacts_secrets_and_bounds_event_metadata():
    pool = RecordingPool()
    repo = Repository(SimpleNamespace(pool=pool))

    await repo.append_map_attempt_event(
        "article-1",
        stage="evidence",
        outcome="failed",
        error_code="provider_transient",
        error_detail=(
            "Authorization: Bearer hidden sk-or-secret "
            "https://provider.example/path?token=private"
        ),
        retryable=True,
        correction_index=2,
        model="test/model",
        structured_output=True,
        metadata={
            "schema": "article_map_evidence_v2",
            "secret": "Bearer another-secret",
            "nested": {"must": "drop"},
        },
    )

    query, args = pool.calls[0]
    assert "INSERT INTO map_attempt_events" in query
    assert args[:4] == (
        "article-1",
        "evidence",
        "failed",
        "provider_transient",
    )
    serialized = " ".join(str(value) for value in args)
    assert "hidden" not in serialized
    assert "sk-or-secret" not in serialized
    assert "token=private" not in serialized
    metadata = json.loads(args[-1])
    assert metadata["schema"] == "article_map_evidence_v2"
    assert metadata["secret"] == "[redacted]"
    assert "nested" not in metadata


@pytest.mark.asyncio
async def test_pipeline_event_write_failure_is_non_fatal(caplog):
    class BrokenRepo:
        async def append_map_attempt_event(self, *_args, **_kwargs):
            raise RuntimeError("database unavailable")

    pipeline = EditorialPipeline(
        Settings(_env_file=None, OPENROUTER_API_KEY="test"),
        BrokenRepo(),
        object(),
    )

    await pipeline._event(
        "article-1",
        stage="source",
        outcome="started",
    )

    assert "code=map_event_write_failed" in caplog.text
    assert "database unavailable" not in caplog.text
