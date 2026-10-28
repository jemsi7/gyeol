from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.config import Settings
from app.curation import (
    CurationPlan,
    CurationQuality,
    CurationRoute,
    CurationRouteItem,
    CurationValidationError,
    build_curation_candidate,
)
from app.curation_agent import CurationAgentResult
from app.curation_worker import CurationCatalogWorker
from app.openrouter import OpenRouterPermanentError


CATALOG_ID = UUID("11111111-1111-1111-1111-111111111111")
ACCOUNT_1 = UUID("22222222-2222-2222-2222-222222222222")
ACCOUNT_2 = UUID("33333333-3333-3333-3333-333333333333")


def _row(index: int):
    paragraph = "구조적 변화와 장기 영향을 여러 근거로 설명하는 충분히 긴 문장입니다. " * 4
    return {
        "article_id": f"article-{index}",
        "title": f"기사 {index}",
        "domain": f"source-{index % 4}.example",
        "published_at": datetime(2026, 7, 21, tzinfo=UTC),
        "status": "ok",
        "data": {
            "format_version": 3,
            "headline": f"구조 기사 {index}",
            "summary": paragraph,
            "keywords": ["구조", "근거", f"주제{index}"],
            "selected_claim_refs": ["CL-001", "CL-002", "CL-003", "CL-004"],
            "required_claim_refs": ["CL-001"],
            "scenes": [
                {
                    "title": f"장면 {scene}",
                    "summary": paragraph if scene == 0 else "",
                    "body": paragraph,
                    "secondary": "",
                    "quote": "",
                }
                for scene in range(4)
            ],
            "editorial": {
                "critic_verdict": "pass",
                "source_hash": f"{index:064x}",
            },
        },
    }


def _candidates():
    return tuple(build_curation_candidate(_row(index)) for index in range(8))


def _plan():
    quality = CurationQuality(4, 4, 4, 3, 4, 1, 0, False)
    roles = ("start", "context", "evidence", "counterpoint")
    routes = []
    for route in range(2):
        routes.append(
            CurationRoute(
                slug=f"route-{route + 1}",
                title=f"오래 남는 읽기 경로 {route + 1}",
                summary="장기 구조 변화의 원인과 영향을 네 편으로 이해하는 읽기 경로입니다.",
                topic=f"구조 변화 {route + 1}",
                representative=route == 0,
                items=tuple(
                    CurationRouteItem(
                        article_id=f"article-{route * 4 + item}",
                        role=roles[item],
                        reason="다음 맥락을 이해하기에 충분한 근거를 가진 선정 이유입니다.",
                        lasting_value="며칠 뒤에도 구조를 설명하는 근거로 남는 기사입니다.",
                        quality=quality,
                    )
                    for item in range(4)
                ),
            )
        )
    return CurationPlan(
        ("AI 인프라", "전력과 데이터센터", "반도체 공급망"), tuple(routes)
    )


class FakeRepo:
    def __init__(self, *, candidate_count=8, claimed=True, order=None):
        self.candidate_count = candidate_count
        self.claimed = claimed
        self.order = order if order is not None else []
        self.claim_calls = []
        self.sample_calls = []
        self.signal_calls = []
        self.published = []
        self.retried = []
        self.pool_reads = 0

    async def curation_pool_state(self, **_kwargs):
        self.order.append("pool")
        self.pool_reads += 1
        return {
            "candidate_count": self.candidate_count,
            "pool_fingerprint": "a" * 32,
        }

    async def claim_curation_catalog(self, **kwargs):
        self.order.append("claim")
        self.claim_calls.append(kwargs)
        return {"id": CATALOG_ID} if self.claimed else None

    async def curation_sample_accounts(self, **kwargs):
        self.order.append("sample")
        self.sample_calls.append(kwargs)
        return {
            "eligible_user_count": 2,
            "account_ids": [ACCOUNT_1, ACCOUNT_2],
        }

    async def curation_audience_signal_rows(self, **kwargs):
        self.order.append("signals")
        self.signal_calls.append(kwargs)
        return []

    async def publish_curation_catalog(self, catalog_id, **kwargs):
        self.order.append("publish")
        self.published.append((catalog_id, kwargs))

    async def retry_curation_catalog(self, catalog_id, **kwargs):
        self.order.append("retry")
        self.retried.append((catalog_id, kwargs))
        return "failed" if kwargs["error_code"] == "provider_permanent" else "queued"


class FakeAgent:
    def __init__(self, result, *, configured=True, order=None):
        self.result = result
        self.configured = configured
        self.order = order if order is not None else []
        self.calls = []

    async def generate(self, audience, **kwargs):
        self.order.append("agent")
        self.calls.append((audience, kwargs))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _result(*, candidates=None):
    return CurationAgentResult(
        plan=_plan(),
        candidates=tuple(candidates or _candidates()),
    )


def _settings(**values):
    configured = {
        "OPENROUTER_API_KEY": "test-key",
        "CURATION_POLL_SECONDS": 5,
        **values,
    }
    return Settings(_env_file=None, **configured)


@pytest.mark.asyncio
async def test_worker_claims_before_sampling_and_skips_profile_when_not_due():
    agent = FakeAgent(_result())
    short_repo = FakeRepo(candidate_count=7)
    result = await CurationCatalogWorker(_settings(), short_repo, agent).run_once()
    assert result["kind"] == "insufficient_candidates"
    assert short_repo.order == ["pool"]
    assert agent.calls == []

    gated_repo = FakeRepo(candidate_count=8, claimed=False)
    result = await CurationCatalogWorker(_settings(), gated_repo, agent).run_once()
    assert result["kind"] == "not_due"
    assert gated_repo.order == ["pool", "claim"]
    assert gated_repo.sample_calls == []
    assert agent.calls == []


@pytest.mark.asyncio
async def test_worker_builds_global_aggregate_and_publishes_dedicated_model_once():
    order = []
    repo = FakeRepo(order=order)
    agent = FakeAgent(_result(), order=order)
    now = datetime(2026, 7, 21, 1, tzinfo=UTC)
    worker = CurationCatalogWorker(_settings(), repo, agent, clock=lambda: now)

    result = await worker.run_once()

    assert result == {"kind": "published", "candidates": 8, "routes": 2, "published": 1}
    assert order == ["pool", "claim", "sample", "signals", "agent", "publish"]
    audience, agent_args = agent.calls[0]
    assert audience.prompt_dict()["scope"] == "global_service_audience"
    assert audience.sampled_user_count == 2
    assert "account" not in str(audience.prompt_dict()).lower()
    assert agent_args["refresh_slot"].endswith("Z")
    assert repo.published[0][0] == CATALOG_ID
    assert (
        repo.published[0][1]["model"]
        == "deepseek/deepseek-v4-flash"
    )
    assert repo.published[0][1]["valid_until"] > now
    assert repo.retried == []


@pytest.mark.asyncio
async def test_worker_reuses_same_sample_seed_for_same_refresh_slot():
    now = datetime(2026, 7, 21, 7, 59, tzinfo=UTC)
    repos = [FakeRepo(), FakeRepo()]
    for repo in repos:
        await CurationCatalogWorker(
            _settings(),
            repo,
            FakeAgent(_result()),
            clock=lambda: now,
        ).run_once()

    assert repos[0].claim_calls[0]["input_hash"] == repos[1].claim_calls[0]["input_hash"]
    assert repos[0].sample_calls[0]["seed"] == repos[1].sample_calls[0]["seed"]


@pytest.mark.asyncio
async def test_worker_double_validation_rejects_agent_result_before_publish():
    same_domain = tuple(
        replace(candidate, domain="only.example") for candidate in _candidates()
    )
    repo = FakeRepo()
    result = await CurationCatalogWorker(
        _settings(),
        repo,
        FakeAgent(_result(candidates=same_domain)),
    ).run_once()

    assert result["kind"] == "queued"
    assert repo.published == []
    assert repo.retried[0][1]["error_code"] == "route_source_diversity_failed"


@pytest.mark.asyncio
async def test_worker_agent_and_permanent_provider_errors_are_safe():
    validation_repo = FakeRepo()
    result = await CurationCatalogWorker(
        _settings(),
        validation_repo,
        FakeAgent(CurationValidationError("agent_loop_exhausted")),
    ).run_once()
    assert result["kind"] == "queued"
    assert validation_repo.retried[0][1]["error_code"] == "agent_loop_exhausted"

    provider_repo = FakeRepo()
    result = await CurationCatalogWorker(
        _settings(),
        provider_repo,
        FakeAgent(OpenRouterPermanentError("provider secret detail")),
    ).run_once()
    assert result["kind"] == "failed"
    assert provider_repo.retried[0][1]["error_code"] == "provider_permanent"


@pytest.mark.asyncio
async def test_worker_start_is_single_and_stop_is_idempotent():
    repo = FakeRepo(candidate_count=0)
    agent = FakeAgent(_result())
    worker = CurationCatalogWorker(
        _settings(SCHEDULER_ENABLED=False), repo, agent
    )
    assert worker.start() is True
    assert worker.start() is False
    for _ in range(20):
        if repo.pool_reads:
            break
        await asyncio.sleep(0)
    assert repo.pool_reads == 1
    await worker.stop()
    await worker.stop()


def test_worker_settings_are_bounded_and_use_dedicated_model():
    settings = _settings()
    assert settings.curation_refresh_min_seconds == 21600
    assert settings.curation_candidate_limit == 48
    assert settings.curation_max_routes == 4
    assert settings.curation_agent_model == "deepseek/deepseek-v4-flash"
    for values in (
        {"CURATION_POLL_SECONDS": 4},
        {"CURATION_REFRESH_MIN_SECONDS": 3599},
        {"CURATION_CANDIDATE_LIMIT": 7},
        {"CURATION_MAX_ROUTES": 5},
        {"CURATION_VALID_HOURS": 23},
    ):
        with pytest.raises(ValueError):
            _settings(**values)

    worker = CurationCatalogWorker(
        Settings(_env_file=None, CURATION_WORKER_ENABLED=False),
        FakeRepo(),
        FakeAgent(_result(), configured=False),
    )
    assert worker.start() is False
