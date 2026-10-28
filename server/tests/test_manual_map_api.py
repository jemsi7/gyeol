from __future__ import annotations

import httpx
import pytest

from app.api import app, state
from app.config import Settings
from app.mapper import MapOutcome


ARTICLE_ID = "a" * 64


class Repo:
    async def get_article(self, article_id):
        assert article_id == ARTICLE_ID
        return {
            "id": article_id,
            "url": "https://news.example/article",
            "title": "Article",
            "domain": "news.example",
            "socialimage_url": None,
        }


class FakeEngine:
    outcome = MapOutcome(ARTICLE_ID, "ok", n_images=0)
    last_article = None

    async def map_article(self, article, *, force, reset_pipeline):
        type(self).last_article = article
        assert article.id == ARTICLE_ID
        assert force is True
        assert reset_pipeline is True
        return [self.outcome]


@pytest.fixture(autouse=True)
def configured_state():
    state.settings = Settings(
        _env_file=None,
        ENABLE_MAPPING=True,
        OPENROUTER_API_KEY="test",
    )
    state.repo = Repo()
    state.engine = FakeEngine()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["ok", "retry", "dead", "failed"])
async def test_manual_map_endpoint_serializes_slots_outcome(status):
    FakeEngine.outcome = MapOutcome(
        ARTICLE_ID,
        status,
        n_images=2,
        error=None if status == "ok" else "safe failure",
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(f"/articles/{ARTICLE_ID}/map")

    assert response.status_code == 200
    assert response.json() == {
        "article_id": ARTICLE_ID,
        "outcome": {
            "article_id": ARTICLE_ID,
            "status": status,
            "n_images": 2,
            "error": None if status == "ok" else "safe failure",
        },
    }


@pytest.mark.asyncio
async def test_manual_map_accepts_cerebras_only_and_keeps_provider_internal():
    state.settings = Settings(
        _env_file=None,
        ENABLE_MAPPING=True,
        CEREBRAS_API_KEY="test",
    )
    FakeEngine.outcome = MapOutcome(
        ARTICLE_ID,
        "ok",
        n_images=1,
        provider="cerebras",
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        response = await client.post(f"/articles/{ARTICLE_ID}/map")

    assert response.status_code == 200
    assert response.json()["outcome"] == {
        "article_id": ARTICLE_ID,
        "status": "ok",
        "n_images": 1,
        "error": None,
    }


@pytest.mark.asyncio
async def test_manual_map_hydrates_primary_document_attribution():
    class DocumentRepo(Repo):
        async def get_article(self, article_id):
            row = await super().get_article(article_id)
            return {
                **row,
                "coverage_domain": "crypto",
                "document_type": "governance_proposal",
                "document_source_name": "aave-governance",
            }

    state.repo = DocumentRepo()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        response = await client.post(f"/articles/{ARTICLE_ID}/map")

    assert response.status_code == 200
    article = FakeEngine.last_article
    assert article.coverage_domain == "crypto"
    assert article.document_type == "governance_proposal"
    assert article.document_source_name == "aave-governance"
