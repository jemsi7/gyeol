from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from app.api import app, state
from app.app_service import AppClientError


NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
TOKEN = "A" * 43


class FakeRecommendationAccountService:
    def __init__(self):
        self.calls = []
        self.error: AppClientError | None = None

    async def recommendations(self, **kwargs):
        self.calls.append(("recommendations", kwargs))
        if self.error:
            raise self.error
        return {
            "format_version": 1,
            "surface": kwargs["request"]["surface"],
            "strategy": "personalized",
            "ranking_version": "content-v1",
            "generated_at": NOW,
            "items": [],
            "next_cursor": None,
        }

    async def link_privacy_consent(self, **kwargs):
        self.calls.append(("link_privacy_consent", kwargs))
        if self.error:
            raise self.error
        return {
            "consent_id": kwargs["consent_id"],
            "account_id": uuid4(),
            "policy_version": "1.5.0",
            "policy_url": "https://example.test/privacy",
            "accepted_at": NOW,
            "linked_at": NOW,
        }


@pytest.fixture
def account_service():
    previous = getattr(state, "account_service", None)
    fake = FakeRecommendationAccountService()
    state.account_service = fake
    try:
        yield fake
    finally:
        if previous is not None:
            state.account_service = previous


def _home_body():
    return {
        "surface": "home",
        "context_article_id": None,
        "exclude_article_ids": ["a" * 64],
        "time_context": {
            "occurred_at": "2026-07-26T21:00:00+09:00",
            "time_zone": "Asia/Seoul",
            "utc_offset_minutes": 540,
        },
        "limit": 5,
        "cursor": None,
    }


@pytest.mark.asyncio
async def test_account_recommendation_home_and_consent_link_contract(account_service):
    headers = {"Authorization": f"Bearer {TOKEN}"}
    consent_id = uuid4()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        recommendation = await client.post(
            "/v1/account/recommendations",
            headers=headers,
            json=_home_body(),
        )
        linked = await client.post(
            f"/v1/account/privacy-consents/{consent_id}",
            headers=headers,
        )

    assert recommendation.status_code == 200
    assert recommendation.json()["ranking_version"] == "content-v1"
    assert recommendation.json()["surface"] == "home"
    assert linked.status_code == 200
    assert linked.json()["consent_id"] == str(consent_id)
    recommendation_call = account_service.calls[0][1]
    assert recommendation_call["bearer_token"] == TOKEN
    assert recommendation_call["request"]["time_context"]["occurred_at"].tzinfo


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "patch",
    [
        {"surface": "article_boundary", "context_article_id": None, "limit": 3},
        {
            "surface": "article_boundary",
            "context_article_id": "b" * 64,
            "limit": 2,
        },
        {
            "surface": "article_boundary",
            "context_article_id": "b" * 64,
            "limit": 3,
            "cursor": "not-allowed",
        },
        {"exclude_article_ids": ["A" * 64]},
        {"exclude_article_ids": ["a" * 64, "a" * 64]},
    ],
)
async def test_account_recommendation_strict_request_matrix(account_service, patch):
    body = {**_home_body(), **patch}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/account/recommendations",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json=body,
        )

    assert response.status_code == 422
    assert account_service.calls == []


@pytest.mark.asyncio
async def test_account_recommendation_requires_bearer_and_preserves_stable_errors(
    account_service,
):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        missing = await client.post(
            "/v1/account/recommendations",
            json=_home_body(),
        )
        account_service.error = AppClientError(409, "privacy_consent_required")
        stale = await client.post(
            "/v1/account/recommendations",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json=_home_body(),
        )

    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "invalid_account_session"
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "privacy_consent_required"
