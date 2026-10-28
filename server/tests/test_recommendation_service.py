from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

from app.account_service import AccountService
from app.app_service import AppClientError
from app.apple_identity import TokenCipher
from app.config import Settings


NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
ACCOUNT_ID = uuid4()
TOKEN = base64.urlsafe_b64encode(b"x" * 32).rstrip(b"=").decode()
OTHER_TOKEN = base64.urlsafe_b64encode(b"y" * 32).rstrip(b"=").decode()
CONTEXT_ID = "f" * 64


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        APPLE_SIGN_IN_ENABLED=True,
        APPLE_SIGN_IN_CLIENT_ID="com.example.gyeol",
        APPLE_SIGN_IN_TEAM_ID="TEAMEXAMPLE",
        APPLE_SIGN_IN_KEY_ID="SIGNINKEY1",
        APPLE_SIGN_IN_PRIVATE_KEY="unused-provider-private-key",
        APPLE_TOKEN_ENCRYPTION_KEY=Fernet.generate_key().decode(),
        PRIVACY_POLICY_VERSION="1.5.0",
        PUBLIC_BASE_URL="https://example.test",
    )


def _feature_row(index: int, *, context: bool = False) -> dict:
    article_id = CONTEXT_ID if context else f"{index:064x}"
    return {
        "article_id": article_id,
        "terms": ["ai", "반도체"] if context else ["ai", f"주제{index}"],
        "term_weights": {"ai": 1.0, ("반도체" if context else f"주제{index}"): 1.0},
        "representative_topic": "반도체" if context else f"주제{index}",
        "domain": "context.example" if context else f"source{index}.example",
        "published_at": NOW - timedelta(minutes=index),
        "quality_score": 1.0,
        "headline": "현재 기사" if context else f"추천 기사 {index}",
    }


class FakeRecommendationRepository:
    def __init__(self):
        self.current_consent = True
        self.snapshot_delay = 0.0
        self.candidate_calls = 0
        self.linked = True
        self.exposures = []
        self.recorded_exposure_count: int | None = None

    async def account_for_session(self, token_hash):
        return {
            "id": ACCOUNT_ID,
            "session_id": uuid4(),
            "apple_refresh_token": b"encrypted",
        }

    async def has_current_account_consent(self, account_id, policy_version):
        assert account_id == ACCOUNT_ID
        assert policy_version == "1.5.0"
        return self.current_consent

    async def recommendation_snapshot_at(self):
        if self.snapshot_delay:
            await asyncio.sleep(self.snapshot_delay)
        return NOW

    async def recommendation_signal_rows(self, account_id, **kwargs):
        assert kwargs["limit"] == 500
        return [
            {
                "event_type": "search",
                "occurred_at": NOW - timedelta(minutes=index),
                "utc_offset_minutes": 0,
                "local_date": NOW.date(),
                "local_hour": 12,
                "query": f"AI 검색어{index}",
                "article_id": None,
            }
            for index in range(20)
        ]

    async def recommendation_feedback_rows(self, account_id, **kwargs):
        return []

    async def recommendation_saved_rows(self, account_id, **kwargs):
        return []

    async def recommendation_exposure_rows(
        self, account_id, *, snapshot_at, continuation, limit
    ):
        if continuation:
            return [
                row for row in self.exposures
                if row["surface"] == "home" and row["served_at"] >= snapshot_at
            ]
        initial = [
            row for row in self.exposures
            if row["surface"] == "home" and row["page_kind"] == "initial"
        ]
        return list(reversed(initial))

    async def record_recommendation_exposures(self, account_id, **kwargs):
        for item in kwargs["items"]:
            self.exposures.append(
                {
                    **item,
                    "request_id": kwargs["request_id"],
                    "surface": kwargs["surface"],
                    "page_kind": kwargs["page_kind"],
                    "served_at": kwargs["served_at"],
                }
            )
        return (
            len(kwargs["items"])
            if self.recorded_exposure_count is None
            else self.recorded_exposure_count
        )

    async def recommendation_article_features(self, article_ids):
        return [_feature_row(0, context=True)] if CONTEXT_ID in article_ids else []

    async def recommendation_candidates(self, **kwargs):
        self.candidate_calls += 1
        return [
            {**_feature_row(index), "candidate_sources": ["core" if index < 4 else "now"]}
            for index in range(1, 12)
        ]

    async def recommendation_feed_rows(self, article_ids):
        return [
            {
                "article_id": article_id,
                "source_url": f"https://source.example/{article_id}",
                "domain": f"{article_id[-1]}.example",
                "published_at": NOW,
                "source_language": "ko",
                "sourcecountry": "South Korea",
                "status": "ok",
                "data": {
                    "format_version": 3,
                    "language": "ko",
                    "headline": f"기사 {article_id[-1]}",
                    "summary": "요약",
                    "keywords": ["ai"],
                    "body_markdown": "",
                    "scenes": [],
                },
            }
            for article_id in article_ids
        ]

    async def link_account_privacy_consent(
        self, account_id, consent_id, policy_version
    ):
        if not self.linked:
            return None
        return {
            "consent_id": consent_id,
            "account_id": account_id,
            "policy_version": policy_version,
            "policy_url": "https://example.test/privacy",
            "accepted_at": NOW,
            "linked_at": NOW,
        }


def _service():
    settings = _settings()
    repository = FakeRecommendationRepository()
    return (
        AccountService(
            settings,
            repository,
            object(),
            TokenCipher(settings.apple_token_encryption_key.get_secret_value()),
        ),
        repository,
    )


def _request(*, surface="home", cursor=None, context_article_id=None, limit=5):
    return {
        "surface": surface,
        "context_article_id": context_article_id,
        "exclude_article_ids": [],
        "time_context": {
            "occurred_at": NOW,
            "time_zone": "Asia/Seoul",
            "utc_offset_minutes": 540,
        },
        "limit": limit,
        "cursor": cursor,
    }


@pytest.mark.asyncio
async def test_home_first_and_continuation_are_personalized_and_session_scoped():
    service, repository = _service()

    first = await service.recommendations(
        bearer_token=TOKEN,
        request=_request(),
        request_base_url="https://example.test/",
    )
    second = await service.recommendations(
        bearer_token=TOKEN,
        request=_request(cursor=first["next_cursor"]),
        request_base_url="https://example.test/",
    )

    assert first["surface"] == "home"
    assert first["strategy"] == "personalized"
    assert first["ranking_version"] == "content-v1"
    assert len(first["items"]) == 5
    assert first["next_cursor"]
    assert not (
        {item["article_id"] for item in first["items"]}
        & {item["article_id"] for item in second["items"]}
    )
    calls_before = repository.candidate_calls
    with pytest.raises(AppClientError) as raised:
        await service.recommendations(
            bearer_token=OTHER_TOKEN,
            request=_request(cursor=first["next_cursor"]),
            request_base_url="https://example.test/",
        )
    assert raised.value.status_code == 400
    assert raised.value.code == "invalid_recommendation_cursor"
    assert repository.candidate_calls == calls_before


@pytest.mark.asyncio
async def test_home_general_revisit_replaces_at_least_three_of_first_five():
    service, _ = _service()

    first = await service.recommendations(
        bearer_token=TOKEN,
        request=_request(),
        request_base_url="https://example.test/",
    )
    revisit = await service.recommendations(
        bearer_token=TOKEN,
        request=_request(),
        request_base_url="https://example.test/",
    )

    first_ids = {item["article_id"] for item in first["items"]}
    revisit_ids = {item["article_id"] for item in revisit["items"]}
    assert len(first_ids - revisit_ids) >= 3


@pytest.mark.asyncio
async def test_boundary_requires_current_context_and_returns_three_without_cursor():
    service, _ = _service()

    response = await service.recommendations(
        bearer_token=TOKEN,
        request=_request(
            surface="article_boundary",
            context_article_id=CONTEXT_ID,
            limit=3,
        ),
        request_base_url="https://example.test/",
    )

    assert response["surface"] == "article_boundary"
    assert len(response["items"]) == 3
    assert response["next_cursor"] is None


@pytest.mark.asyncio
async def test_recommendation_fails_when_exposure_count_does_not_match_items():
    service, repository = _service()
    repository.recorded_exposure_count = 0

    with pytest.raises(AppClientError) as raised:
        await service.recommendations(
            bearer_token=TOKEN,
            request=_request(),
            request_base_url="https://example.test/",
        )

    assert raised.value.status_code == 503
    assert raised.value.code == "recommendation_unavailable"


@pytest.mark.asyncio
async def test_current_consent_is_required_before_recommendation_reads():
    service, repository = _service()
    repository.current_consent = False

    with pytest.raises(AppClientError) as raised:
        await service.recommendations(
            bearer_token=TOKEN,
            request=_request(),
            request_base_url="https://example.test/",
        )

    assert raised.value.status_code == 409
    assert raised.value.code == "privacy_consent_required"
    assert repository.candidate_calls == 0


@pytest.mark.asyncio
async def test_recommendation_timeout_is_stable_503_without_query_log(caplog):
    service, repository = _service()
    repository.snapshot_delay = 1.3
    caplog.set_level("WARNING")

    with pytest.raises(AppClientError) as raised:
        await service.recommendations(
            bearer_token=TOKEN,
            request=_request(),
            request_base_url="https://example.test/",
        )

    assert raised.value.status_code == 503
    assert raised.value.code == "recommendation_unavailable"
    assert "AI 검색" not in caplog.text
    assert TOKEN not in caplog.text


@pytest.mark.asyncio
async def test_signed_in_current_consent_link_is_idempotent_and_stale_fails():
    service, repository = _service()
    consent_id = uuid4()

    linked = await service.link_privacy_consent(
        bearer_token=TOKEN, consent_id=consent_id
    )
    assert linked["consent_id"] == consent_id
    assert linked["policy_version"] == "1.5.0"

    repository.linked = False
    with pytest.raises(AppClientError) as raised:
        await service.link_privacy_consent(
            bearer_token=TOKEN, consent_id=consent_id
        )
    assert raised.value.status_code == 422
    assert raised.value.code == "privacy_consent_required"
