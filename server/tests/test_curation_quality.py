from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from app.curation import (
    CURATION_INPUT_REVISION,
    MAX_GENERATED_CURATION_ROUTES,
    CurationValidationError,
    build_curation_candidate,
    curation_input_hash,
    curation_plan_payload,
    curation_refresh_slot,
    validate_curation_plan,
)
from app.curation_audience import CurationAudienceAggregate
from app.curation_prompts import (
    CURATION_TOOLS,
    build_curation_agent_messages,
    build_curation_tools,
)


def _row(index: int, *, status: str = "ok", critic: str = "pass", thin: bool = False):
    paragraph = "장기 구조 변화의 원인과 근거를 구체적으로 설명하는 문장입니다. " * (1 if thin else 4)
    scenes = [
        {
            "id": f"S-{scene + 1:02d}",
            "title": f"근거 장면 {scene + 1}",
            "summary": paragraph if scene == 0 else "",
            "body": paragraph,
            "secondary": "",
            "quote": "",
        }
        for scene in range(4)
    ]
    return {
        "article_id": f"article-{index}",
        "title": f"원문 제목 {index}",
        "domain": f"source-{index % 4}.example",
        "published_at": datetime(2026, 7, 21, index % 20, tzinfo=UTC),
        "status": status,
        "data": {
            "format_version": 3,
            "headline": f"구조를 설명하는 기사 {index}",
            "summary": paragraph,
            "keywords": ["인공지능", "인프라", f"주제{index}"],
            "body_markdown": "RAW-BODY-SENTINEL",
            "selected_claim_refs": ["CL-001", "CL-002", "CL-003", "CL-004"],
            "required_claim_refs": ["CL-001", "CL-002"],
            "scenes": scenes,
            "editorial": {
                "critic_verdict": critic,
                "source_hash": f"{index:064x}",
            },
        },
    }


def _quality(**overrides):
    value = {
        "durability": 4,
        "evidence_depth": 4,
        "explanatory_value": 4,
        "originality": 3,
        "structural_significance": 4,
        "ephemeral_risk": 1,
        "slop_risk": 0,
        "structural_breaking": False,
    }
    value.update(overrides)
    return value


def _plan(count: int = 2):
    roles = ["start", "context", "evidence", "counterpoint"]
    routes = []
    for route_index in range(count):
        routes.append(
            {
                "slug": f"durable-route-{route_index + 1}",
                "title": f"오래 남는 읽기 경로 {route_index + 1}",
                "summary": "지금의 변화가 앞으로 무엇을 바꾸는지 네 편으로 이해합니다.",
                "topic": f"구조 변화 {route_index + 1}",
                "representative": route_index == 0,
                "items": [
                    {
                        "article_id": f"article-{route_index * 4 + item_index}",
                        "role": roles[item_index],
                        "reason": "다음 기사의 맥락을 이해하는 데 필요한 충분한 선정 이유입니다.",
                        "lasting_value": "며칠 뒤에도 구조적 변화를 이해하는 근거로 남는 기사입니다.",
                        "quality": _quality(),
                    }
                    for item_index in range(4)
                ],
            }
        )
    return {
        "search_suggestions": ["AI 인프라", "전력과 데이터센터", "반도체 공급망"],
        "routes": routes,
    }


def _candidates(count: int = 8):
    return [build_curation_candidate(_row(index)) for index in range(count)]


def test_prefilter_requires_critic_claims_and_readable_copy():
    assert build_curation_candidate(_row(0)) is not None
    assert build_curation_candidate(_row(1, status="degraded")) is None
    assert build_curation_candidate(_row(2, critic="revise")) is None
    assert build_curation_candidate(_row(3, thin=True)) is None
    missing_claims = _row(4)
    missing_claims["data"]["selected_claim_refs"] = ["CL-001"]
    assert build_curation_candidate(missing_claims) is None


def test_validator_rejects_ephemeral_slop_and_accepts_structural_breaking():
    candidates = _candidates()
    valid = _plan()
    assert len(validate_curation_plan(valid, candidates).routes) == 2

    ephemeral = deepcopy(valid)
    ephemeral["routes"][0]["items"][0]["quality"].update(
        ephemeral_risk=4, structural_breaking=False
    )
    with pytest.raises(CurationValidationError, match="quality_gate_failed"):
        validate_curation_plan(ephemeral, candidates)

    breaking = deepcopy(valid)
    breaking["routes"][0]["items"][0]["quality"].update(
        ephemeral_risk=4,
        structural_breaking=True,
        structural_significance=4,
        evidence_depth=4,
    )
    plan = validate_curation_plan(breaking, candidates)
    assert plan.routes[0].items[0].quality.structural_breaking is True

    slop = deepcopy(valid)
    slop["routes"][0]["items"][0]["quality"]["slop_risk"] = 2
    with pytest.raises(CurationValidationError, match="quality_gate_failed"):
        validate_curation_plan(slop, candidates)


@pytest.mark.parametrize("mutation", ["duplicate", "representative", "role", "domain"])
def test_validator_requires_disjoint_routes_one_representative_and_source_diversity(mutation):
    candidates = _candidates()
    raw = _plan()
    if mutation == "duplicate":
        raw["routes"][1]["items"][0]["article_id"] = "article-0"
        expected = "candidate_duplicate"
    elif mutation == "representative":
        raw["routes"][1]["representative"] = True
        expected = "invalid_plan_shape"
    elif mutation == "role":
        raw["routes"][0]["items"][0]["role"] = "evidence"
        expected = "invalid_plan_shape"
    else:
        candidates = [replace(candidate, domain="only.example") for candidate in candidates]
        expected = "route_source_diversity_failed"
    with pytest.raises(CurationValidationError, match=expected):
        validate_curation_plan(raw, candidates)


def test_validator_accepts_four_routes_and_rejects_five():
    assert MAX_GENERATED_CURATION_ROUTES == 4
    assert len(validate_curation_plan(_plan(4), _candidates(16)).routes) == 4

    with pytest.raises(CurationValidationError, match="catalog_too_small"):
        validate_curation_plan(_plan(5), _candidates(20))


def test_input_hash_is_slot_and_pool_sensitive_and_refresh_slot_is_stable():
    now = datetime(2026, 7, 21, 7, 59, tzinfo=UTC)
    slot = curation_refresh_slot(now, 21600)
    assert slot == "2026-07-21T06:00:00Z"
    assert curation_refresh_slot(now, 21600) == slot

    first = curation_input_hash(
        date(2026, 7, 21),
        refresh_slot=slot,
        pool_fingerprint="a" * 32,
    )
    assert first == curation_input_hash(
        date(2026, 7, 21),
        refresh_slot=slot,
        pool_fingerprint="a" * 32,
    )
    assert first != curation_input_hash(
        date(2026, 7, 21),
        refresh_slot="2026-07-21T12:00:00Z",
        pool_fingerprint="a" * 32,
    )
    assert first != curation_input_hash(
        date(2026, 7, 21),
        refresh_slot=slot,
        pool_fingerprint="b" * 32,
    )


def test_input_hash_is_contract_revision_sensitive():
    editorial_day = date(2026, 7, 21)
    refresh_slot = "2026-07-21T06:00:00Z"
    pool_fingerprint = "a" * 32
    legacy_payload = {
        "editorial_day": editorial_day.isoformat(),
        "refresh_slot": refresh_slot,
        "pool_fingerprint": pool_fingerprint,
    }
    legacy_hash = hashlib.sha256(
        json.dumps(
            legacy_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    assert CURATION_INPUT_REVISION == "global-agent-v4"
    assert curation_input_hash(
        editorial_day,
        refresh_slot=refresh_slot,
        pool_fingerprint=pool_fingerprint,
    ) != legacy_hash
    legacy_v3_hash = hashlib.sha256(
        json.dumps(
            {
                "contract_revision": "global-agent-v3",
                **legacy_payload,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert curation_input_hash(
        editorial_day,
        refresh_slot=refresh_slot,
        pool_fingerprint=pool_fingerprint,
    ) != legacy_v3_hash


def test_agent_prompt_has_aggregate_no_bulk_candidates_and_three_strict_tools():
    audience = CurationAudienceAggregate(
        eligible_user_count=120,
        sampled_user_count=100,
        interests=(),
        domains=(),
        engagement={},
        feedback={},
    )
    messages = build_curation_agent_messages(
        audience,
        editorial_day=date(2026, 7, 21),
        refresh_slot="2026-07-21T06:00:00Z",
    )
    system, user = messages
    assert "slop" in system["content"].lower()
    assert "global_service_audience" in user["content"]
    assert "RAW-BODY-SENTINEL" not in user["content"]
    assert '"maximum_routes":4' in user["content"]
    assert '"candidates"' not in user["content"]
    assert [tool["function"]["name"] for tool in CURATION_TOOLS] == [
        "search_articles",
        "get_article_details",
        "submit_catalog",
    ]
    routes_schema = CURATION_TOOLS[-1]["function"]["parameters"]["properties"][
        "routes"
    ]
    assert routes_schema["minItems"] == 2
    assert routes_schema["maxItems"] == 4
    assert len(user["content"]) < 20_000


def test_prompt_and_tool_schema_share_configured_route_bounds():
    audience = CurationAudienceAggregate(
        eligible_user_count=0,
        sampled_user_count=0,
        interests=(),
        domains=(),
        engagement={},
        feedback={},
    )
    messages = build_curation_agent_messages(
        audience,
        editorial_day=date(2026, 7, 29),
        refresh_slot="2026-07-29T06:00:00Z",
        min_routes=2,
        max_routes=3,
    )
    tool_definitions = build_curation_tools(min_routes=2, max_routes=3)
    assert '"minimum_routes":2' in messages[1]["content"]
    assert '"maximum_routes":3' in messages[1]["content"]
    routes_schema = tool_definitions[-1]["function"]["parameters"]["properties"][
        "routes"
    ]
    assert routes_schema["minItems"] == 2
    assert routes_schema["maxItems"] == 3

    with pytest.raises(ValueError, match="invalid curation route bounds"):
        build_curation_tools(min_routes=2, max_routes=5)


def test_plan_payload_roundtrips_through_the_same_validator():
    candidates = _candidates()
    plan = validate_curation_plan(_plan(), candidates)
    payload = curation_plan_payload(plan)
    assert len(validate_curation_plan(payload, candidates).routes) == 2
