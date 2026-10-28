from __future__ import annotations

import math

import pytest

from app.config import Settings
from app.vector_store import (
    PGVECTOR_VERSION,
    VECTOR_SCHEMA_SQL,
    VectorStore,
    embedding_literal,
    error_code,
)


def _settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "VECTOR_DATABASE_URL": "postgresql://vector.internal/rag",
    }
    values.update(overrides)
    return Settings(**values)


class FakePool:
    def __init__(self, *, version: str = PGVECTOR_VERSION, rows=None):
        self.version = version
        self.rows = list(rows or [])
        self.calls: list[tuple[str, str, tuple]] = []

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        return "OK"

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", query, args))
        return self.version

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        return self.rows


@pytest.mark.asyncio
async def test_vector_start_requires_exact_extension_and_builds_hnsw_schema():
    pool = FakePool()
    store = VectorStore(_settings(), pool=pool)

    assert await store.start() is True
    assert store.ready is True
    assert "vector(768)" in VECTOR_SCHEMA_SQL
    assert "USING hnsw (embedding vector_cosine_ops)" in VECTOR_SCHEMA_SQL
    assert "google/gemini-embedding-2" in VECTOR_SCHEMA_SQL
    assert "REFERENCES articles" not in VECTOR_SCHEMA_SQL
    assert [call[0] for call in pool.calls] == ["execute", "fetchval", "execute"]

    bad = VectorStore(_settings(), pool=FakePool(version="0.8.1"))
    assert await bad.start() is False
    assert bad.ready is False


def test_embedding_literal_rejects_wrong_length_bool_and_nonfinite():
    assert embedding_literal([0, 1.5], dimensions=2) == "[0.0,1.5]"
    with pytest.raises(ValueError, match="exactly 2"):
        embedding_literal([0], dimensions=2)
    with pytest.raises(ValueError, match="finite"):
        embedding_literal([True, 0], dimensions=2)
    for value in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError, match="finite"):
            embedding_literal([value, 0], dimensions=2)


@pytest.mark.asyncio
async def test_search_uses_cosine_cutoff_exclusions_and_stable_limit():
    pool = FakePool(
        rows=[
            {"image_id": "b", "similarity": 0.88},
            {"image_id": "c", "similarity": 0.75},
        ]
    )
    store = VectorStore(_settings(), pool=pool)

    hits = await store.search(
        [0.0] * 768,
        limit=3,
        min_similarity=0.75,
        exclude_ids={"a"},
    )

    assert [(hit.image_id, hit.similarity) for hit in hits] == [
        ("b", 0.88),
        ("c", 0.75),
    ]
    kind, query, args = pool.calls[-1]
    assert kind == "fetch"
    assert "embedding <=> $4::vector(768)" in query
    assert "NOT (image_id = ANY($5::text[]))" in query
    assert args[2] == 0.75
    assert args[4] == ["a"]
    assert args[5] == 3


def test_error_code_is_bounded_and_does_not_preserve_provider_text():
    assert error_code(" Rate Limited (429) ") == "rate_limited_429"
    assert len(error_code("x" * 200)) == 64
