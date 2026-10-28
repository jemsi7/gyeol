from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.config import Settings
from app.image_rag import ImageRagService


def _settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "VECTOR_DATABASE_URL": "postgresql://vector.internal/rag",
        "OPENROUTER_API_KEY": "test-key",
    }
    values.update(overrides)
    return Settings(**values)


def _ledger(count: int = 5) -> dict:
    return {
        "claims": [
            {"id": f"CL-{index:03d}", "text": f"exact claim {index}"}
            for index in range(1, count + 1)
        ]
    }


def _planning(count: int, *, image_refs=None, chart_refs=None) -> dict:
    image_refs = image_refs or {}
    chart_refs = chart_refs or {}
    return {
        "plan": {
            "scenes": [
                {
                    "id": f"S-{index:02d}",
                    "role": "evidence" if index > 1 else "lead",
                    "title": f"Scene {index}",
                    "claim_refs": [f"CL-{index:03d}"],
                    "image_ref": image_refs.get(index),
                    "chart_ref": chart_refs.get(index),
                }
                for index in range(1, count + 1)
            ]
        }
    }


class NoCall:
    def __getattr__(self, name):
        raise AssertionError(f"unexpected collaborator call: {name}")


@pytest.mark.asyncio
async def test_retrieval_makes_zero_calls_when_target_is_already_met():
    service = ImageRagService(_settings(), NoCall(), None, vector_store=NoCall(), embeddings=NoCall())
    planning = _planning(3, image_refs={2: 1, 3: 2})

    result = await service.retrieve(
        "article",
        title="title",
        ledger=_ledger(3),
        planning=planning,
        images=[
            {"image_id": "cover"},
            {"image_id": "scene"},
            {"image_id": "scene-two"},
        ],
        cover_ref=0,
    )

    assert (result.target_views, result.current_views, result.missing_views) == (3, 3, 0)
    assert result.candidates == []


@dataclass
class Hit:
    image_id: str
    similarity: float


class FakeEmbeddings:
    def __init__(self, *, error: Exception | None = None):
        self.error = error
        self.queries: list[str] = []

    async def embed_texts(self, queries):
        self.queries = list(queries)
        if self.error:
            raise self.error
        return [[float(index)] * 768 for index in range(len(queries))]


class FakeVector:
    def __init__(self, batches):
        self.batches = list(batches)
        self.calls: list[dict] = []

    async def search(self, vector, **kwargs):
        self.calls.append(kwargs)
        return self.batches.pop(0)


class FakeRepo:
    def __init__(self, rows):
        self.rows = rows
        self.requested: list[str] = []

    async def rag_candidate_records(self, image_ids, *, exclude_article_id):
        self.requested = list(image_ids)
        assert exclude_article_id == "current"
        return [self.rows[image_id] for image_id in image_ids if image_id in self.rows]


def _row(image_id: str) -> dict:
    return {
        "image_id": image_id,
        "origin_article_id": f"origin-{image_id}",
        "origin_title": f"Origin {image_id}",
        "origin_url": f"https://news.example/{image_id}",
        "origin_domain": "news.example",
        "source_url": f"https://images.example/{image_id}.jpg",
        "content_type": "image/jpeg",
        "byte_size": 100,
        "backend": "s3",
        "storage_ref": f"gdelt/{'a' * 64}.jpg",
    }


@pytest.mark.asyncio
async def test_retrieval_ranks_deduplicates_excludes_current_and_drops_stale_rows():
    current_id = "current-image"
    vector = FakeVector(
        [
            [Hit(current_id, 0.99), Hit("a", 0.90), Hit("stale", 0.80)],
            [Hit("a", 0.95), Hit("b", 0.75)],
            [],
            [],
            [],
        ]
    )
    embeddings = FakeEmbeddings()
    repo = FakeRepo({"a": _row("a"), "b": _row("b")})
    service = ImageRagService(
        _settings(), repo, None, vector_store=vector, embeddings=embeddings
    )
    service.ready = True

    result = await service.retrieve(
        "current",
        title="Cross-modal article",
        ledger=_ledger(5),
        planning=_planning(5),
        images=[{"image_id": current_id}],
        cover_ref=None,
    )

    assert (result.target_views, result.current_views, result.missing_views) == (4, 0, 4)
    assert [candidate.image_id for candidate in result.candidates] == ["a", "b"]
    assert [candidate.scene_id for candidate in result.candidates] == ["S-01", "S-02"]
    assert repo.requested == ["a", "stale", "b"]
    assert current_id in vector.calls[0]["exclude_ids"]
    assert "CL-001: exact claim 1" in embeddings.queries[0]


@pytest.mark.asyncio
async def test_retrieval_failure_preserves_target_and_returns_empty_candidates():
    service = ImageRagService(
        _settings(),
        FakeRepo({}),
        None,
        vector_store=FakeVector([]),
        embeddings=FakeEmbeddings(error=RuntimeError("provider payload")),
    )
    service.ready = True

    result = await service.retrieve(
        "current",
        title="title",
        ledger=_ledger(3),
        planning=_planning(3),
        images=[],
        cover_ref=None,
    )

    assert (result.target_views, result.current_views, result.missing_views) == (3, 0, 3)
    assert result.candidates == []
