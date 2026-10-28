"""Offline orchestration and gate tests for Delivery Map v3."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.db import SCHEMA_SQL
from app.editorial_pipeline import (
    EditorialPipeline,
    PipelineStageError,
    _chart_claim_refs,
    _recover_unique_single_substitution,
    artifact_hash,
    balanced_scene_batches,
    build_source_packet,
    build_rag_query,
    build_validation_feedback_prompt,
    canonicalize_critic_report,
    canonicalize_planning,
    eligible_rag_scenes,
    enrich_planning_media,
    image_density_target,
    normalize_evidence_claim_ledger,
    normalize_evidence_ledger,
    normalize_evidence_selection,
    normalize_editorial_brief,
    normalize_planner_proposal,
    normalize_planning,
    normalize_scene_batch,
    overlay_scene_invariants,
    project_claim_numeric_grounding,
    project_scene_numeric_grounding,
    prompt_version_for_stage,
    select_cover_image_ref,
    visible_image_views,
    validate_editorial_brief,
    validate_critic_report,
    validate_planning,
    validate_scene_batch,
    validate_evidence_claim_ledger,
    validate_evidence_ledger,
    validate_evidence_selection,
    validate_source_packet,
)
from app.editorial_prompts import (
    CRITIC_SYSTEM_PROMPT,
    EVIDENCE_CLAIM_SYSTEM_PROMPT,
    EVIDENCE_SYSTEM_PROMPT,
    PLANNING_SYSTEM_PROMPT,
    REPAIR_SYSTEM_PROMPT,
    SCENE_SYSTEM_PROMPT,
    build_evidence_prompt,
    build_evidence_claim_prompt,
    build_critic_prompt,
    build_repair_prompt,
)
from app.editorial_prompts import STAGE_PROMPT_VERSIONS
from app.map_output_schemas import (
    evidence_claim_text_output_schema,
    evidence_selection_output_schema,
)


BODY = """도시는 올해 가장 뜨거운 보행 구간 세 곳에 이동식 그늘을 설치했다. 이번 사업은 폭염 때에도 시민이 안전하게 이동할 수 있는 쉼터를 만드는 것이 목적이다. 현장 조사는 낮 시간대 보도 온도와 이용자 움직임을 함께 기록했다.

설치 뒤 시범 구간의 평균 체류 시간은 8분에서 13분으로 늘었다. 담당 연구팀은 그늘 자체보다 그늘이 이어지는 간격이 보행 선택을 바꿨다고 설명했다. 다만 조사는 세 구간에서 한 달 동안 진행돼 계절 전체의 효과로 일반화할 수는 없다.

시는 다음 여름까지 그늘 위치와 이용 기록을 다시 비교한 뒤 확대 여부를 결정한다. 연구팀은 결과를 공개하고 주민 의견을 추가로 받을 예정이다. 이번 기록은 시설 수보다 사람들이 실제로 머물 수 있었는지를 다음 판단 기준으로 남긴다. 구간별 온도와 시간대별 이용량도 함께 공개한다."""


TRANSLATED_EVIDENCE = (
    "The city installed portable shade structures along its hottest pedestrian routes.",
    "After installation, the average stay in the pilot area increased from 8 minutes to 13 minutes.",
    "The city will compare locations and usage records again before deciding whether to expand the program.",
)


ENGLISH_BODY = f"""{TRANSLATED_EVIDENCE[0]} The project is intended to give residents safer places to pause while walking during extreme heat. Field researchers also recorded pavement conditions and pedestrian movement throughout the day.

{TRANSLATED_EVIDENCE[1]} The research team said the spacing between shaded areas influenced walking choices more than the structures alone. The pilot covered a limited set of routes, so its findings cannot yet be generalized to an entire season.

{TRANSLATED_EVIDENCE[2]} Researchers plan to publish the results and gather additional resident feedback. The next review will focus on whether people could actually remain in the space, not simply on how many structures were installed."""


def source_packet(body: str = BODY, *, output_language: str = "auto") -> dict:
    return build_source_packet(
        title="도시의 이동식 그늘",
        source_url="https://news.example/shade",
        domain="news.example",
        body_text=body,
        images=[],
        visible_image_refs=set(),
        output_language=output_language,
    )


def translated_source_packet() -> dict:
    return build_source_packet(
        title="Portable shade changed how long pedestrians stayed",
        source_url="https://news.example/shade-english",
        domain="news.example",
        body_text=ENGLISH_BODY,
        images=[],
        visible_image_refs=set(),
        output_language="ko",
    )


def translated_evidence_selection_response(source: dict) -> dict:
    sentence_by_text = {
        sentence["text"]: sentence["id"]
        for paragraph in source["paragraphs"]
        for sentence in paragraph["sentences"]
    }
    return {
        "language": "ko",
        "claims": [
            {
                "id": "CL-001",
                "kind": "fact",
                "importance": "essential",
                "evidence": [
                    {"sentence_refs": [sentence_by_text[TRANSLATED_EVIDENCE[0]]]}
                ],
            },
            {
                "id": "CL-002",
                "kind": "number",
                "importance": "essential",
                "evidence": [
                    {"sentence_refs": [sentence_by_text[TRANSLATED_EVIDENCE[1]]]}
                ],
            },
            {
                "id": "CL-003",
                "kind": "context",
                "importance": "supporting",
                "evidence": [
                    {"sentence_refs": [sentence_by_text[TRANSLATED_EVIDENCE[2]]]}
                ],
            },
        ],
        "media": [],
        "visualizations": [],
    }


def translated_scene_response() -> dict:
    raw = scene_response(repaired=True)
    raw["scenes"][1]["quote"] = (
        "설치 뒤 시범 구간의 평균 체류 시간은 8분에서 13분으로 늘었다."
    )
    # DR-13260: the writer names the claim-evidence address; the server owns
    # the exact source span.
    raw["scenes"][1]["quote_evidence_ref"] = "CL-002/1"
    return raw


def evidence_response() -> dict:
    return {
        "language": "ko",
        "claims": [
            {
                "id": "CL-001",
                "kind": "fact",
                "importance": "essential",
                "text": "도시가 폭염 보행 구간에 이동식 그늘을 설치했다.",
                "evidence": [{
                    "paragraph_id": "P-001",
                    "quote": "도시는 올해 가장 뜨거운 보행 구간 세 곳에 이동식 그늘을 설치했다.",
                }],
            },
            {
                "id": "CL-002",
                "kind": "number",
                "importance": "essential",
                "text": "설치 뒤 평균 체류 시간이 8분에서 13분으로 늘었다.",
                "evidence": [{
                    "paragraph_id": "P-002",
                    "quote": "설치 뒤 시범 구간의 평균 체류 시간은 8분에서 13분으로 늘었다.",
                }],
            },
            {
                "id": "CL-003",
                "kind": "context",
                "importance": "supporting",
                "text": "시는 추가 기록 뒤 확대 여부를 결정한다.",
                "evidence": [{
                    "paragraph_id": "P-003",
                    "quote": "시는 다음 여름까지 그늘 위치와 이용 기록을 다시 비교한 뒤 확대 여부를 결정한다.",
                }],
            },
        ],
        "media": [],
        "visualizations": [],
    }


def evidence_selection_response(source: dict) -> dict:
    """Return the standard fixture as source-ref selection-only wire data."""
    raw = evidence_response()
    sentence_by_text = {
        sentence["text"]: sentence["id"]
        for paragraph in source["paragraphs"]
        for sentence in paragraph["sentences"]
    }
    for claim in raw["claims"]:
        quote = claim["evidence"][0]["quote"]
        claim.pop("text")
        claim["evidence"] = [{"sentence_refs": [sentence_by_text[quote]]}]
    return raw


def evidence_claim_text_response() -> dict:
    return {
        "language": "ko",
        "claims": [
            {
                "id": "CL-001",
                "text": "도시가 폭염 보행 구간에 이동식 그늘을 설치했다.",
            },
            {
                "id": "CL-002",
                "text": "설치 뒤 평균 체류 시간이 8분에서 13분으로 늘었다.",
            },
            {
                "id": "CL-003",
                "text": "시는 추가 기록 뒤 확대 여부를 결정한다.",
            },
        ],
    }


def evidence_stage_responses() -> list[dict]:
    return [evidence_response(), evidence_claim_text_response()]


def evidence_response_with_chart() -> dict:
    raw = evidence_response()
    evidence = "설치 뒤 시범 구간의 평균 체류 시간은 8분에서 13분으로 늘었다."
    raw["visualizations"] = [
        {
            "ref": 0,
            "kind": "line",
            "title": "평균 체류 시간 변화",
            "unit": "분",
            "alt_text": "평균 체류 시간이 8분에서 13분으로 증가",
            "series": [
                {
                    "name": "평균 체류 시간",
                    "mark": "line",
                    "points": [
                        {
                            "label": "설치 전",
                            "value": 8,
                            "raw_value": "8분",
                            "evidence": evidence,
                        },
                        {
                            "label": "설치 후",
                            "value": 13,
                            "raw_value": "13분",
                            "evidence": evidence,
                        },
                    ],
                }
            ],
        }
    ]
    return raw


def partial_chart_case() -> tuple[dict, dict]:
    quote = (
        "후속 기록은 평균 체류 시간이 8분에서 13분으로 늘어 "
        "5분 차이가 났다고 정리했다."
    )
    source = source_packet(BODY + "\n\n" + quote)
    raw = evidence_response_with_chart()
    raw["claims"][1]["text"] = (
        "평균 체류 시간이 8분에서 13분으로 늘어 5분 차이가 났다."
    )
    raw["claims"][1]["evidence"] = [
        {"paragraph_id": "P-004", "quote": quote}
    ]
    for point in raw["visualizations"][0]["series"][0]["points"]:
        point["evidence"] = quote
    return source, raw


def image_source_packet() -> tuple[dict, list[dict]]:
    images = [
        {
            "ref": 0,
            "image_id": "social-image",
            "source_url": "https://images.example/social.jpg",
            "role": "social",
        },
        {
            "ref": 1,
            "image_id": "content-one",
            "source_url": "https://images.example/content-1.jpg",
            "role": "content",
        },
        {
            "ref": 2,
            "image_id": "content-two",
            "source_url": "https://images.example/content-2.jpg",
            "role": "content",
        },
    ]
    source = build_source_packet(
        title="도시의 이동식 그늘",
        source_url="https://news.example/shade",
        domain="news.example",
        body_text=BODY,
        images=images,
        visible_image_refs={0, 1, 2},
    )
    return source, images


def evidence_response_with_media() -> dict:
    raw = evidence_response()
    raw["media"] = [
        {
            "ref": 0,
            "caption": "그늘이 설치된 보행 구간",
            "reason": "설치 현장을 보여준다",
            "claim_refs": ["CL-001"],
        },
        {
            "ref": 1,
            "caption": "그늘 아래 머무는 시민",
            "reason": "체류 시간 변화를 보완한다",
            "claim_refs": ["CL-002"],
        },
        {
            "ref": 2,
            "caption": "후속 조사 안내",
            "reason": "확대 판단의 다음 단계를 보여준다",
            "claim_refs": ["CL-003"],
        },
    ]
    return raw


def planning_response() -> dict:
    return {
        "brief": {
            "language": "ko",
            "headline": "이동식 그늘이 바꾼 보행의 시간",
            "subheadline": "세 구간의 기록에서 시작된 변화",
            "summary": "도시는 폭염 보행 구간에 이동식 그늘을 설치했다.",
            "keywords": ["폭염", "이동식 그늘", "보행"],
            "central_question": "작은 그늘이 보행 경험을 어떻게 바꿨나",
            "answer": "머물 수 있는 시간이 늘어났다.",
            "why_it_matters": "폭염 대응 시설을 실제 이용 변화로 평가할 수 있다.",
            "ordering_policy": "editorial_completeness",
            "required_claim_refs": ["CL-001", "CL-002"],
            "included_supporting_claim_refs": ["CL-003"],
            "omitted_supporting_claim_refs": [],
            "summary_claim_refs": ["CL-001"],
        },
        "scenes": [
            {
                "id": "S-01", "role": "lead", "label": "설치", "purpose": "변화를 연다",
                "title_hint": "폭염 구간에 놓인 그늘", "claim_refs": ["CL-001"],
                "image_ref": None, "chart_ref": None, "target_chars": 220,
                "transition": "이용 기록으로 이어진다",
            },
            {
                "id": "S-02", "role": "evidence", "label": "기록", "purpose": "핵심 수치를 보여준다",
                "title_hint": "8분에서 13분으로", "claim_refs": ["CL-002"],
                "image_ref": None, "chart_ref": None, "target_chars": 220,
                "transition": "다음 판단으로 이어진다",
            },
            {
                "id": "S-03", "role": "close", "label": "다음", "purpose": "후속 결정을 닫는다",
                "title_hint": "기록 뒤 정할 확대", "claim_refs": ["CL-003"],
                "image_ref": None, "chart_ref": None, "target_chars": 220,
                "transition": "기사를 마친다",
            },
        ],
    }


def five_scene_media_case() -> tuple[dict, dict]:
    source, _ = image_source_packet()
    ledger = normalize_evidence_ledger(evidence_response_with_media(), source)
    for claim_id, text in (
        ("CL-004", "연구팀은 결과와 주민 의견을 공개한다."),
        ("CL-005", "시설 수보다 실제 체류 여부를 다음 기준으로 남긴다."),
    ):
        claim = deepcopy(ledger["claims"][2])
        claim["id"] = claim_id
        claim["text"] = text
        ledger["claims"].append(claim)

    raw = planning_response()
    raw["brief"]["included_supporting_claim_refs"] = [
        "CL-003",
        "CL-004",
        "CL-005",
    ]
    raw["scenes"][2]["role"] = "context"
    raw["scenes"][2]["transition"] = "영향과 다음 기준으로 이어진다"
    raw["scenes"].extend(
        [
            {
                "id": "S-04",
                "role": "impact",
                "label": "공개",
                "purpose": "후속 검증을 보여준다",
                "title_hint": "결과와 주민 의견 공개",
                "claim_refs": ["CL-004"],
                "image_ref": None,
                "chart_ref": None,
                "target_chars": 180,
                "transition": "마지막 판단 기준으로 이어진다",
            },
            {
                "id": "S-05",
                "role": "close",
                "label": "기준",
                "purpose": "다음 판단 기준을 닫는다",
                "title_hint": "시설보다 실제 체류",
                "claim_refs": ["CL-005"],
                "image_ref": None,
                "chart_ref": None,
                "target_chars": 180,
                "transition": "기사를 마친다",
            },
        ]
    )
    return ledger, normalize_planning(raw)


def planning_lead_reserve_case() -> tuple[dict, dict, dict]:
    evidence = evidence_response()
    extra = deepcopy(evidence["claims"][2])
    extra["id"] = "CL-004"
    extra["text"] = "연구팀은 결과를 공개한다."
    extra["evidence"] = [{
        "paragraph_id": "P-003",
        "quote": "연구팀은 결과를 공개하고 주민 의견을 추가로 받을 예정이다.",
    }]
    evidence["claims"].append(extra)
    ledger = normalize_evidence_ledger(evidence)

    invalid = planning_response()
    invalid["brief"]["included_supporting_claim_refs"].append("CL-004")
    invalid["scenes"][1]["claim_refs"].append("CL-004")

    corrected = deepcopy(invalid)
    corrected["scenes"][1]["claim_refs"].remove("CL-004")
    corrected["scenes"][0]["claim_refs"].append("CL-004")
    return ledger, invalid, corrected


def scene_response(*, repaired: bool = False) -> dict:
    return {
        "scenes": [
            {
                "id": "S-01", "role": "lead", "label": "설치",
                "title": "가장 뜨거운 길에 놓인 그늘",
                "summary": "도시는 폭염 보행 구간에 이동식 그늘을 설치했다.",
                "body": "올해 가장 뜨거운 보행 구간 세 곳에 이동식 그늘이 들어섰다.",
                "secondary": "", "quote": "", "image_ref": None, "chart_ref": None,
                "claim_refs": ["CL-001"],
            },
            {
                "id": "S-02", "role": "evidence", "label": "기록",
                "title": "머무는 시간은 8분에서 13분으로",
                "summary": "",
                "body": (
                    "설치 뒤 시범 구간의 평균 체류 시간은 8분에서 13분으로 늘었다."
                    if repaired else
                    "설치 뒤 평균 체류 시간은 8분에서 13분으로 늘었다. 놀라운 대박이다."
                ),
                "secondary": "", "quote": "", "image_ref": None, "chart_ref": None,
                "claim_refs": ["CL-002"],
            },
            {
                "id": "S-03", "role": "close", "label": "다음",
                "title": "확대보다 먼저 남길 기록",
                "summary": "",
                "body": "시는 다음 여름까지 이용 기록을 다시 비교한 뒤 확대 여부를 정한다.",
                "secondary": "", "quote": "", "image_ref": None, "chart_ref": None,
                "claim_refs": ["CL-003"],
            },
        ]
    }


def scene_response_over_budget() -> dict:
    raw = scene_response(repaired=True)
    raw["scenes"][1]["secondary"] = "가" * 200
    return raw


class MemoryRepo:
    def __init__(self):
        self.artifacts: dict[tuple[str, str], dict] = {}

    async def get_pipeline_artifact(self, article_id: str, stage: str, input_hash: str):
        value = self.artifacts.get((article_id, stage))
        return deepcopy(value) if value and value["input_hash"] == input_hash else None

    async def upsert_pipeline_artifact(self, article_id: str, **kwargs):
        key = (article_id, kwargs["stage"])
        prior = self.artifacts.get(key)
        attempts = prior["attempts"] + 1 if prior and prior["input_hash"] == kwargs["input_hash"] else 1
        self.artifacts[key] = {**deepcopy(kwargs), "attempts": attempts}


class ScriptedLLM:
    def __init__(self, responses: list[object]):
        self.responses = [deepcopy(item) for item in responses]
        self.calls: list[tuple[str, str, int, str | None]] = []
        self.output_schemas: list[object | None] = []

    async def complete_json(
        self,
        system: str,
        user_text: str,
        images=None,
        *,
        output_schema=None,
    ):
        self.output_schemas.append(output_schema)
        self.calls.append(
            (
                system,
                user_text,
                len(images or []),
                output_schema.name if output_schema is not None else None,
            )
        )
        if not self.responses:
            raise AssertionError("unexpected LLM call")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def settings(**values) -> Settings:
    return Settings(
        _env_file=None,
        OPENROUTER_API_KEY="test",
        MAP_MIN_SOURCE_CHARS=300,
        MAP_MIN_SCENES=3,
        MAP_MAX_SCENES=9,
        MAP_SCENE_BATCH_SIZE=3,
        **values,
    )


@pytest.mark.asyncio
async def test_provider_identity_separates_pipeline_checkpoint_hashes():
    repo = MemoryRepo()
    first_llm = ScriptedLLM([{"ok": True}])
    second_llm = ScriptedLLM([{"ok": True}])
    first = EditorialPipeline(
        settings(),
        repo,
        first_llm,
        model_identity="openrouter:shared-model",
    )
    second = EditorialPipeline(
        settings(),
        repo,
        second_llm,
        model_identity="cerebras:shared-model",
    )

    kwargs = {
        "article_id": "article-provider-cache",
        "stage": "evidence",
        "input_payload": {"same": "input"},
        "system": "Return JSON.",
        "user_text": "Map it.",
        "normalize": lambda raw: raw,
        "validate": lambda _raw: [],
    }
    assert await first._stage_call(**kwargs) == {"ok": True}
    assert await second._stage_call(**kwargs) == {"ok": True}

    assert len(first_llm.calls) == 1
    assert len(second_llm.calls) == 1
    assert repo.artifacts[
        ("article-provider-cache", "evidence")
    ]["model"] == "cerebras:shared-model"


def test_source_packet_has_stable_exact_spans_and_rejects_thin_input():
    first = source_packet()
    second = source_packet()
    assert first["source_hash"] == second["source_hash"]
    assert validate_source_packet(first, min_chars=300) == []
    for paragraph in first["paragraphs"]:
        assert first["body_text"][paragraph["start"]:paragraph["end"]] == paragraph["text"]
        assert paragraph["sentences"]
        for sentence in paragraph["sentences"]:
            assert first["body_text"][sentence["start"]:sentence["end"]] == sentence["text"]
    thin = source_packet("짧은 기사")
    assert any("requires 300" in error for error in validate_source_packet(thin, min_chars=300))


def test_v5_prompt_versions_isolate_evidence_from_planner_and_scene(monkeypatch):
    payload = {"same": "input"}
    evidence_before = artifact_hash("evidence", "model", payload)
    scene_before = artifact_hash("scene:S-01", "model", payload)
    assert prompt_version_for_stage("evidence_refs") == "editorial-v5.1"
    assert prompt_version_for_stage("evidence") == "editorial-v5.1"
    assert prompt_version_for_stage("planner") == "editorial-v5.1"
    assert prompt_version_for_stage("scene:S-01") == "editorial-v5.1"
    assert prompt_version_for_stage("critic") == "editorial-v4.2"
    assert prompt_version_for_stage("repair:S-01") == "editorial-v4.4"
    assert "Never calculate a difference" in EVIDENCE_SYSTEM_PROMPT

    monkeypatch.setitem(STAGE_PROMPT_VERSIONS, "scene", "editorial-scene-next")

    assert artifact_hash("evidence", "model", payload) == evidence_before
    assert artifact_hash("scene:S-01", "model", payload) != scene_before


def test_output_language_is_hashed_and_sent_to_evidence_analysis():
    automatic = source_packet()
    english = source_packet(output_language="en")
    assert automatic["source_hash"] != english["source_hash"]
    assert '"output_language":"en"' in build_evidence_prompt(english)
    errors = validate_evidence_ledger(
        normalize_evidence_ledger(evidence_response()), english
    )
    assert "evidence language must match requested output language" in errors


def test_cover_prefers_visible_social_then_approved_media():
    source, _ = image_source_packet()
    ledger = normalize_evidence_ledger(evidence_response_with_media(), source)
    assert select_cover_image_ref(source, ledger) == 0

    source["images"][0]["visible_to_model"] = False
    assert select_cover_image_ref(source, ledger) == 0

    ledger["media"] = [ledger["media"][1]]
    assert select_cover_image_ref(source, ledger) == 1

    ledger["media"] = []
    assert select_cover_image_ref(source, ledger) is None


def test_evidence_rejects_more_than_three_media_items():
    source, _ = image_source_packet()
    source["images"].append({
        "ref": 3,
        "source_url": "https://images.example/content-3.jpg",
        "role": "content",
        "visible_to_model": True,
    })
    raw = evidence_response_with_media()
    raw["media"].append({
        "ref": 3,
        "caption": "추가 현장 이미지",
        "reason": "같은 설치 현장을 보완한다",
        "claim_refs": ["CL-001"],
    })
    errors = validate_evidence_ledger(normalize_evidence_ledger(raw, source), source)
    assert "media length exceeds 3" in errors


def test_three_scene_media_enrichment_stops_at_literal_three_view_target():
    source, _ = image_source_packet()
    ledger = normalize_evidence_ledger(evidence_response_with_media(), source)
    original = normalize_planning(planning_response())

    enriched = enrich_planning_media(original, ledger)

    assert [scene["image_ref"] for scene in original["plan"]["scenes"]] == [None] * 3
    assert [scene["image_ref"] for scene in enriched["plan"]["scenes"]] == [0, 1, 2]
    assert validate_planning(enriched, ledger, settings()) == []


def test_media_enrichment_excludes_cover_and_clears_planner_duplicate():
    source, _ = image_source_packet()
    ledger = normalize_evidence_ledger(evidence_response_with_media(), source)
    raw = planning_response()
    raw["scenes"][0]["image_ref"] = 0

    enriched = enrich_planning_media(
        normalize_planning(raw), ledger, excluded_refs={0}, cover_ref=0
    )

    assert [scene["image_ref"] for scene in enriched["plan"]["scenes"]] == [None, 1, 2]
    assert validate_planning(enriched, ledger, settings()) == []


def test_media_enrichment_skips_chart_scene_and_plan_rejects_no_claim_overlap():
    source, _ = image_source_packet()
    ledger = normalize_evidence_ledger(evidence_response_with_media(), source)
    raw = planning_response()
    raw["scenes"][1]["chart_ref"] = 0
    ledger["visualizations"] = normalize_evidence_ledger(
        evidence_response_with_chart(), source
    )["visualizations"]

    enriched = enrich_planning_media(normalize_planning(raw), ledger)

    assert [scene["image_ref"] for scene in enriched["plan"]["scenes"]] == [0, None, 2]
    invalid = deepcopy(enriched)
    invalid["plan"]["scenes"][2]["image_ref"] = 1
    assert any(
        "image_ref has no claim overlap" in error
        for error in validate_planning(invalid, ledger, settings())
    )


def test_five_scene_density_counts_cover_and_reserves_lead():
    ledger, value = five_scene_media_case()
    value["plan"]["scenes"][0]["image_ref"] = 1

    enriched = enrich_planning_media(value, ledger, cover_ref=0)
    refs = [scene["image_ref"] for scene in enriched["plan"]["scenes"]]

    assert refs == [None, 1, 2, None, None]
    assert 1 + sum(ref is not None for ref in refs) == 3
    assert validate_planning(enriched, ledger, settings()) == []


def test_five_scene_density_reaches_four_views_when_media_allows():
    ledger, value = five_scene_media_case()
    ledger["media"].append(
        {
            "ref": 3,
            "caption": "결과를 공개하는 자리",
            "reason": "후속 검증 절차를 보여준다",
            "claim_refs": ["CL-004"],
        }
    )

    enriched = enrich_planning_media(value, ledger, cover_ref=0)
    refs = [scene["image_ref"] for scene in enriched["plan"]["scenes"]]

    assert refs == [None, 1, 2, 3, None]
    assert 1 + sum(ref is not None for ref in refs) == 4
    assert validate_planning(enriched, ledger, settings()) == []


def test_five_scene_density_without_cover_assigns_three():
    ledger, value = five_scene_media_case()

    enriched = enrich_planning_media(value, ledger)
    refs = [scene["image_ref"] for scene in enriched["plan"]["scenes"]]

    assert refs == [0, 1, 2, None, None]
    assert len({ref for ref in refs if ref is not None}) == 3
    assert validate_planning(enriched, ledger, settings()) == []


def test_five_scene_density_uses_only_available_relevant_media():
    ledger, value = five_scene_media_case()
    ledger["media"] = [ledger["media"][2]]

    enriched = enrich_planning_media(value, ledger, cover_ref=0)
    refs = [scene["image_ref"] for scene in enriched["plan"]["scenes"]]

    assert refs == [None, None, 2, None, None]
    assert 1 + sum(ref is not None for ref in refs) == 2
    assert validate_planning(enriched, ledger, settings()) == []


def test_five_scene_density_maximizes_constrained_matching():
    ledger, value = five_scene_media_case()
    ledger["media"][0]["claim_refs"] = ["CL-001", "CL-002"]
    ledger["media"][1]["claim_refs"] = ["CL-001"]
    ledger["media"][2]["claim_refs"] = ["CL-003"]

    enriched = enrich_planning_media(value, ledger)
    refs = [scene["image_ref"] for scene in enriched["plan"]["scenes"]]

    assert refs == [1, 0, 2, None, None]
    assert validate_planning(enriched, ledger, settings()) == []


def test_four_scene_media_enrichment_preserves_legacy_behavior():
    ledger, value = five_scene_media_case()
    value["plan"]["scenes"] = value["plan"]["scenes"][:4]

    enriched = enrich_planning_media(value, ledger)

    assert [
        scene["image_ref"] for scene in enriched["plan"]["scenes"]
    ] == [0, 1, 2, None]


@pytest.mark.parametrize(
    ("scene_count", "target"),
    [(3, 3), (4, 4), (5, 4), (6, 5), (7, 6), (8, 6), (9, 6)],
)
def test_literal_four_fifths_image_density_target(scene_count, target):
    assert image_density_target(scene_count) == target


def test_density_helpers_count_unique_refs_and_select_only_empty_non_chart_scenes():
    planning = {
        "plan": {
            "scenes": [
                {"id": "S-01", "image_ref": None, "chart_ref": None, "claim_refs": ["CL-001"]},
                {"id": "S-02", "image_ref": 2, "chart_ref": None, "claim_refs": ["CL-002"]},
                {"id": "S-03", "image_ref": None, "chart_ref": 0, "claim_refs": ["CL-003"]},
                {"id": "S-04", "image_ref": None, "chart_ref": None, "claim_refs": ["CL-004"]},
            ]
        }
    }

    assert visible_image_views(planning, 0) == 2
    assert [scene["id"] for scene in eligible_rag_scenes(planning, 0)] == ["S-04"]


def test_rag_query_contains_exact_scene_claim_text():
    query = build_rag_query(
        article_title="기사 제목",
        scene={
            "id": "S-02",
            "role": "evidence",
            "title": "현장",
            "claim_refs": ["CL-002"],
        },
        ledger={"claims": [{"id": "CL-002", "text": "원문 그대로의 주장"}]},
    )

    assert query == (
        "task: search result | query: 기사 제목\n"
        "scene: S-02 | evidence | 현장\n"
        "claims:\nCL-002: 원문 그대로의 주장"
    )


def test_five_scene_density_preserves_unapproved_ref_for_validation():
    ledger, value = five_scene_media_case()
    value["plan"]["scenes"][3]["image_ref"] = 99

    enriched = enrich_planning_media(value, ledger)

    assert enriched["plan"]["scenes"][3]["image_ref"] == 99
    assert any(
        "image_ref is not approved media" in error
        for error in validate_planning(enriched, ledger, settings())
    )


def test_evidence_gate_rejects_non_exact_quote():
    source = source_packet()
    raw = evidence_response()
    raw["claims"][0]["evidence"][0]["quote"] = "원문에 없는 문장"
    errors = validate_evidence_ledger(normalize_evidence_ledger(raw), source)
    assert any("exact paragraph substring" in error for error in errors)


def test_sentence_refs_project_to_exact_legacy_evidence_quotes():
    source = source_packet()
    raw = evidence_response()
    sentence_by_text = {
        sentence["text"]: sentence["id"]
        for paragraph in source["paragraphs"]
        for sentence in paragraph["sentences"]
    }
    expected_quotes: list[str] = []
    for claim in raw["claims"]:
        quote = claim["evidence"][0]["quote"]
        expected_quotes.append(quote)
        claim["evidence"] = [
            {"sentence_refs": [sentence_by_text[quote]]}
        ]

    ledger = normalize_evidence_ledger(raw, source)

    assert [
        claim["evidence"][0]["quote"] for claim in ledger["claims"]
    ] == expected_quotes
    assert validate_evidence_ledger(ledger, source) == []


def test_unknown_sentence_ref_is_a_deterministic_evidence_error():
    source = source_packet()
    raw = evidence_response()
    raw["claims"][0]["evidence"] = [
        {"sentence_refs": ["P-999:S-999"]}
    ]

    ledger = normalize_evidence_ledger(raw, source)
    errors = validate_evidence_ledger(ledger, source)

    assert any("paragraph is unknown" in error for error in errors)


def test_chart_sentence_refs_ground_values_and_drop_fabricated_points():
    source = source_packet()
    raw = evidence_response()
    sentence = source["paragraphs"][1]["sentences"][0]
    raw["visualizations"] = [
        {
            "ref": 9,
            "kind": "line",
            "title": "평균 체류 시간 변화",
            "unit": "분",
            "alt_text": "8분에서 13분으로 증가",
            "series": [
                {
                    "name": "평균 체류 시간",
                    "mark": "line",
                    "points": [
                        {
                            "label": "설치 전",
                            "value": 8,
                            "raw_value": "8분",
                            "sentence_ref": sentence["id"],
                        },
                        {
                            "label": "설치 후",
                            "value": 13,
                            "raw_value": "13분",
                            "sentence_ref": sentence["id"],
                        },
                        {
                            "label": "조작값",
                            "value": 99,
                            "raw_value": "99분",
                            "sentence_ref": sentence["id"],
                        },
                    ],
                }
            ],
        }
    ]

    ledger = normalize_evidence_ledger(raw, source)

    assert len(ledger["visualizations"]) == 1
    assert ledger["visualizations"][0]["ref"] == 0
    points = ledger["visualizations"][0]["series"][0]["points"]
    assert [point["raw_value"] for point in points] == ["8분", "13분"]
    assert all(point["evidence"] == sentence["text"] for point in points)


@pytest.mark.parametrize(
    "claim_text",
    [
        "평균 체류 시간이 8분에서 13분으로 늘어 5분 차이가 났다.",
        "5분 차이는 평균 체류 시간이 8분에서 13분으로 늘어난 결과다.",
        "8분이던 평균 체류 시간은 5분 늘어 13분이 됐다.",
    ],
)
def test_evidence_normalizer_drops_partial_numeric_coverage_chart(claim_text):
    source, raw = partial_chart_case()
    raw["claims"][1]["text"] = claim_text

    ledger = normalize_evidence_ledger(raw, source)

    assert ledger["visualizations"] == []
    assert validate_evidence_ledger(ledger, source) == []


def test_evidence_normalizer_drops_chart_with_only_non_numeric_claim_owner():
    source, raw = partial_chart_case()
    raw["claims"][1]["text"] = "평균 체류 시간이 늘었다."

    ledger = normalize_evidence_ledger(raw, source)

    assert ledger["visualizations"] == []


@pytest.mark.parametrize("full_chart_first", [False, True])
def test_evidence_normalizer_keeps_only_full_coverage_chart_and_reindexes(
    full_chart_first,
):
    source, raw = partial_chart_case()
    partial_chart = raw["visualizations"][0]
    full_chart = deepcopy(partial_chart)
    full_chart["ref"] = 1
    full_chart["alt_text"] = "8분에서 13분으로 늘어난 5분 차이"
    full_chart["series"][0]["points"].append(
        {
            "label": "차이",
            "value": 5,
            "raw_value": "5분",
            "evidence": raw["claims"][1]["evidence"][0]["quote"],
        }
    )
    raw["visualizations"] = (
        [full_chart, partial_chart]
        if full_chart_first
        else [partial_chart, full_chart]
    )

    ledger = normalize_evidence_ledger(raw, source)

    assert len(ledger["visualizations"]) == 1
    chart = ledger["visualizations"][0]
    assert chart["ref"] == 0
    assert {
        point["raw_value"]
        for series in chart["series"]
        for point in series["points"]
    } == {"5분", "8분", "13분"}
    assert _chart_claim_refs(chart, ledger) == ["CL-002"]


def test_evidence_normalizer_recovers_only_unique_single_substitution_quote():
    source = source_packet()
    raw = evidence_response()
    exact = raw["claims"][0]["evidence"][0]["quote"]
    raw["claims"][0]["evidence"][0]["quote"] = exact.replace("도시", "도심", 1)

    ledger = normalize_evidence_ledger(raw, source)

    assert ledger["claims"][0]["evidence"][0]["quote"] == exact
    assert validate_evidence_ledger(ledger, source) == []


def test_evidence_quote_recovery_rejects_ambiguous_multi_change_and_number_change():
    assert _recover_unique_single_substitution("예약", "예산과 예산") == "예약"
    assert _recover_unique_single_substitution("마을", "도시") == "마을"
    assert _recover_unique_single_substitution("9분", "8분") == "9분"


@pytest.mark.parametrize("alias", ["qualifying", "qualification", "limitation"])
def test_evidence_limitation_aliases_normalize_to_caveat(alias):
    raw = evidence_response()
    raw["claims"][0]["kind"] = alias

    ledger = normalize_evidence_ledger(raw)

    assert ledger["claims"][0]["kind"] == "caveat"
    assert validate_evidence_ledger(ledger, source_packet()) == []


def test_evidence_unknown_kind_remains_fail_closed():
    raw = evidence_response()
    raw["claims"][0]["kind"] = "opinion"

    errors = validate_evidence_ledger(normalize_evidence_ledger(raw), source_packet())

    assert "claims[0].kind unsupported" in errors


def test_evidence_gate_rejects_supporting_caveat():
    raw = evidence_response()
    raw["claims"][2]["kind"] = "caveat"
    raw["claims"][2]["importance"] = "supporting"

    errors = validate_evidence_ledger(normalize_evidence_ledger(raw), source_packet())

    assert "claims[2] caveat must be essential" in errors


def test_evidence_prompt_requires_comparable_chart_and_complete_wire_shape():
    assert "must emit at least one visualization" in EVIDENCE_SYSTEM_PROMPT
    assert "ordered monthly sequence is\ndirectly comparable" in EVIDENCE_SYSTEM_PROMPT
    assert '"alt_text":"plain grounded comparison"' in EVIDENCE_SYSTEM_PROMPT
    assert '"raw_value":"12.3%"' in EVIDENCE_SYSTEM_PROMPT
    assert "Every caveat must be essential" in EVIDENCE_SYSTEM_PROMPT
    assert "reconciliation as two separate essential caveat claims" in EVIDENCE_SYSTEM_PROMPT
    assert "server, not you, owns scene IDs" in PLANNING_SYSTEM_PROMPT
    assert "image references" in PLANNING_SYSTEM_PROMPT
    assert "scene_proposals" in PLANNING_SYSTEM_PROMPT
    assert "Any explicit four-digit year" in EVIDENCE_SYSTEM_PROMPT
    assert "urgent escalation route" in EVIDENCE_SYSTEM_PROMPT
    assert "distinct stakeholder's attributed experience" in EVIDENCE_SYSTEM_PROMPT
    assert "routine or extended-hours service excludes urgent or severe" in EVIDENCE_SYSTEM_PROMPT
    assert "Do not write final scene prose" in PLANNING_SYSTEM_PROMPT
    assert "Never invent a claim ref" in PLANNING_SYSTEM_PROMPT
    assert "inventory every directly quoted" in EVIDENCE_SYSTEM_PROMPT
    assert "Never emit a free-form evidence quote" in EVIDENCE_SYSTEM_PROMPT
    assert '"sentence_refs":["P-001:S-001"]' in EVIDENCE_SYSTEM_PROMPT
    assert "visualizations` must not be empty" in EVIDENCE_SYSTEM_PROMPT
    assert "one shared raw unit suffix" in EVIDENCE_SYSTEM_PROMPT
    assert "mandatory comparison" in EVIDENCE_SYSTEM_PROMPT
    assert "emit exactly three media notes" in EVIDENCE_SYSTEM_PROMPT
    assert "Never lower the relevance threshold" in EVIDENCE_SYSTEM_PROMPT
    assert "If fewer than three images qualify" in EVIDENCE_SYSTEM_PROMPT
    assert '"selected_supporting_claim_refs"' in PLANNING_SYSTEM_PROMPT


def test_evidence_selection_schema_excludes_claim_text_and_claim_schema_binds_ids():
    source = source_packet()
    selection_schema = evidence_selection_output_schema(source)
    selection_claim = selection_schema.schema["properties"]["claims"]["items"]

    assert "text" not in selection_claim["properties"]

    selection = normalize_evidence_selection(
        evidence_selection_response(source),
        source,
    )
    assert validate_evidence_selection(selection, source) == []

    claim_schema = evidence_claim_text_output_schema(selection)
    writer_claim = claim_schema.schema["properties"]["claims"]["items"]
    assert writer_claim["properties"]["id"]["enum"] == [
        "CL-001",
        "CL-002",
        "CL-003",
    ]
    assert claim_schema.schema["properties"]["language"]["const"] == "ko"
    assert "sentence_refs" not in writer_claim["properties"]
    assert "numeric whitelist" in EVIDENCE_CLAIM_SYSTEM_PROMPT


def test_evidence_claim_prompt_projects_only_selected_quotes_and_numeric_whitelist():
    body = (
        "The counter recorded 2 units. "
        "A separate sentence recorded 8 unrelated units."
    )
    source = build_source_packet(
        title="Whitelist boundary",
        source_url="https://news.example/whitelist",
        domain="news.example",
        body_text=body,
        images=[],
        visible_image_refs=set(),
        output_language="en",
    )
    selection = normalize_evidence_selection(
        {
            "language": "en",
            "claims": [
                {
                    "id": "CL-001",
                    "kind": "number",
                    "importance": "essential",
                    "evidence": [{"sentence_refs": ["P-001:S-001"]}],
                }
            ],
            "media": [],
            "visualizations": [],
        },
        source,
    )
    assert validate_evidence_selection(selection, source) == []

    prompt = build_evidence_claim_prompt(selection, source)

    assert '"numeric_whitelist":["2"]' in prompt
    assert "The counter recorded 2 units." in prompt
    assert "A separate sentence recorded 8 unrelated units." not in prompt


def test_evidence_writer_rejects_derived_numeric_atom_after_selection():
    body = "The counter recorded 2 units."
    source = build_source_packet(
        title="Derived number",
        source_url="https://news.example/derived-number",
        domain="news.example",
        body_text=body,
        images=[],
        visible_image_refs=set(),
        output_language="en",
    )
    selection = normalize_evidence_selection(
        {
            "language": "en",
            "claims": [
                {
                    "id": "CL-001",
                    "kind": "number",
                    "importance": "essential",
                    "evidence": [{"sentence_refs": ["P-001:S-001"]}],
                }
            ],
            "media": [],
            "visualizations": [],
        },
        source,
    )

    ledger = normalize_evidence_claim_ledger(
        {
            "language": "en",
            "claims": [{"id": "CL-001", "text": "The result was 8 units."}],
        },
        selection,
        source,
    )
    errors = validate_evidence_claim_ledger(ledger, selection, source)

    assert any("numeric atoms absent from evidence: 8" in error for error in errors)
    assert any("allowed evidence numeric atoms: 2" in error for error in errors)


@pytest.mark.asyncio
async def test_evidence_writer_numeric_feedback_retries_with_the_selected_whitelist():
    body = "The counter recorded 2 units."
    source = build_source_packet(
        title="Writer correction",
        source_url="https://news.example/writer-correction",
        domain="news.example",
        body_text=body,
        images=[],
        visible_image_refs=set(),
        output_language="en",
    )
    selection = normalize_evidence_selection(
        {
            "language": "en",
            "claims": [
                {
                    "id": "CL-001",
                    "kind": "number",
                    "importance": "essential",
                    "evidence": [{"sentence_refs": ["P-001:S-001"]}],
                }
            ],
            "media": [],
            "visualizations": [],
        },
        source,
    )
    invalid = {
        "language": "en",
        "claims": [{"id": "CL-001", "text": "The result was 8 units."}],
    }
    corrected = {
        "language": "en",
        "claims": [{"id": "CL-001", "text": "The counter recorded 2 units."}],
    }
    repo = MemoryRepo()
    llm = ScriptedLLM([invalid, corrected])
    pipeline = EditorialPipeline(settings(), repo, llm)

    result = await pipeline._stage_call(
        "writer-numeric-correction",
        stage="evidence",
        input_payload=selection,
        system=EVIDENCE_CLAIM_SYSTEM_PROMPT,
        user_text=build_evidence_claim_prompt(selection, source),
        normalize=lambda raw: normalize_evidence_claim_ledger(raw, selection, source),
        validate=lambda value: validate_evidence_claim_ledger(value, selection, source),
        validation_corrections=1,
        output_schema=evidence_claim_text_output_schema(selection),
    )

    assert result["claims"][0]["text"] == "The counter recorded 2 units."
    assert len(llm.calls) == 2
    assert '"numeric_whitelist":["2"]' in llm.calls[0][1]
    assert "numeric atoms absent from evidence: 8" in llm.calls[1][1]
    assert "allowed evidence numeric atoms: 2" in llm.calls[1][1]
    artifact = repo.artifacts[("writer-numeric-correction", "evidence")]
    assert artifact["status"] == "ok"
    assert artifact["attempts"] == 2


@pytest.mark.asyncio
async def test_split_evidence_pipeline_calls_selection_then_writer_and_reuses_both_artifacts():
    source = source_packet()
    repo = MemoryRepo()
    llm = ScriptedLLM([
        evidence_selection_response(source),
        evidence_claim_text_response(),
        planning_response(),
        scene_response(repaired=True),
    ])
    pipeline = EditorialPipeline(settings(), repo, llm)

    first = await pipeline.compose(
        "split-evidence-cache",
        source=source,
        images=[],
        image_parts=[],
    )
    second = await pipeline.compose(
        "split-evidence-cache",
        source=source,
        images=[],
        image_parts=[],
    )

    assert first == second
    assert [call[3] for call in llm.calls] == [
        "article_map_evidence_refs_v1",
        "article_map_evidence_claims_v1",
        "article_map_planner_v2",
        "article_map_scene_copy_v3",
    ]
    assert {stage for _, stage in repo.artifacts} >= {"evidence_refs", "evidence"}


def test_editorial_brief_cannot_change_the_evidence_output_language():
    brief = planning_response()["brief"]
    brief["language"] = "en"
    errors = validate_editorial_brief(
        normalize_editorial_brief(brief), normalize_evidence_ledger(evidence_response())
    )
    assert "brief language must match evidence ledger language" in errors


def test_summary_claims_must_be_owned_by_the_lead_scene():
    raw = planning_response()
    raw["brief"]["summary_claim_refs"] = ["CL-002"]
    errors = validate_planning(
        normalize_planning(raw), normalize_evidence_ledger(evidence_response()), settings()
    )
    assert "summary claims must all be assigned to the lead scene" in errors


def test_planning_rejects_overstuffed_or_chart_represented_summary_claims():
    raw = planning_response()
    raw["brief"]["summary"] = "첫 문장이다. 둘째 문장이다. 셋째 문장이다."
    raw["brief"]["summary_claim_refs"] = ["CL-001", "CL-002", "CL-003", "CL-004"]
    errors = validate_planning(
        normalize_planning(raw), normalize_evidence_ledger(evidence_response()), settings()
    )
    assert "summary must contain 1..2 sentences" in errors
    assert "summary_claim_refs length must be 1..3" in errors

    chart_raw = planning_response()
    chart_raw["brief"]["summary_claim_refs"] = ["CL-002"]
    chart_raw["scenes"][0]["claim_refs"] = ["CL-001", "CL-002"]
    chart_raw["scenes"][1]["claim_refs"] = ["CL-003"]
    errors = validate_planning(
        normalize_planning(chart_raw),
        normalize_evidence_ledger(evidence_response_with_chart()),
        settings(),
    )
    assert "summary_claim_refs must not include chart-represented claims" in errors


def test_lead_reserves_a_non_summary_claim_when_partition_has_room():
    ledger, raw, corrected = planning_lead_reserve_case()

    errors = validate_planning(normalize_planning(raw), ledger, settings())

    assert "lead must reserve at least one non-summary claim for its body" in errors
    assert validate_planning(normalize_planning(corrected), ledger, settings()) == []


def test_planning_canonicalizer_rebalances_supporting_claim_into_lead():
    ledger, raw, _ = planning_lead_reserve_case()

    canonical = canonicalize_planning(
        raw,
        ledger,
        settings(),
        cover_ref=None,
    )

    lead = canonical["plan"]["scenes"][0]
    donor = canonical["plan"]["scenes"][1]
    assert lead["claim_refs"] == ["CL-001", "CL-004"]
    assert donor["claim_refs"] == ["CL-002"]
    assert validate_planning(canonical, ledger, settings()) == []


def test_lead_extra_claim_is_not_required_when_only_excess_is_chart_owned():
    evidence = evidence_response_with_chart()
    chart_supporting = deepcopy(evidence["claims"][1])
    chart_supporting["id"] = "CL-004"
    chart_supporting["importance"] = "supporting"
    chart_supporting["text"] = "같은 비교에서 8분과 13분이 기록됐다."
    evidence["claims"].append(chart_supporting)
    ledger = normalize_evidence_ledger(evidence)
    raw = planning_response()
    raw["brief"]["included_supporting_claim_refs"] = ["CL-003", "CL-004"]
    raw["brief"]["omitted_supporting_claim_refs"] = []
    raw["scenes"][0]["claim_refs"] = ["CL-001"]
    raw["scenes"][1]["claim_refs"] = ["CL-002", "CL-004"]
    raw["scenes"][1]["chart_ref"] = 0
    raw["scenes"][2]["claim_refs"] = ["CL-003"]

    errors = validate_planning(normalize_planning(raw), ledger, settings())

    assert errors == []
    canonical = canonicalize_planning(
        raw,
        ledger,
        settings(),
        cover_ref=None,
    )
    assert canonical["plan"]["scenes"][0]["claim_refs"] == ["CL-001"]
    assert validate_planning(canonical, ledger, settings()) == []


def test_planning_keeps_partial_chart_claim_in_prose_with_exact_owner_in_chart():
    source, evidence = partial_chart_case()
    evidence["claims"][1]["text"] = (
        "평균 체류 시간이 8분에서 13분으로 늘었다."
    )
    partial_claim = deepcopy(evidence["claims"][1])
    partial_claim["id"] = "CL-004"
    partial_claim["importance"] = "supporting"
    partial_claim["text"] = (
        "평균 체류 시간이 8분에서 13분으로 늘어 5분 차이가 났다."
    )
    evidence["claims"].append(partial_claim)
    ledger = normalize_evidence_ledger(evidence, source)
    raw = planning_response()
    raw["brief"]["included_supporting_claim_refs"] = ["CL-003", "CL-004"]
    raw["brief"]["omitted_supporting_claim_refs"] = []
    raw["scenes"][1]["chart_ref"] = 0

    canonical = canonicalize_planning(
        raw,
        ledger,
        settings(),
        cover_ref=None,
    )

    chart_scene = next(
        scene
        for scene in canonical["plan"]["scenes"]
        if scene["chart_ref"] == 0
    )
    prose_refs = [
        claim_ref
        for scene in canonical["plan"]["scenes"]
        if scene["chart_ref"] is None
        for claim_ref in scene["claim_refs"]
    ]
    assert chart_scene["claim_refs"] == ["CL-002"]
    assert prose_refs.count("CL-004") == 1
    assert validate_planning(canonical, ledger, settings()) == []


def test_planning_canonicalizer_owns_partition_ids_roles_and_claim_coverage():
    ledger = normalize_evidence_ledger(evidence_response())
    raw = planning_response()
    raw["brief"]["required_claim_refs"] = ["CL-003", "CL-003"]
    raw["brief"]["included_supporting_claim_refs"] = ["CL-003", "CL-999"]
    raw["brief"]["omitted_supporting_claim_refs"] = ["CL-003"]
    raw["scenes"][0]["id"] = "wrong"
    raw["scenes"][0]["role"] = "impact"
    raw["scenes"][0]["claim_refs"] = ["CL-001", "CL-001"]
    raw["scenes"][1]["claim_refs"] = []
    raw["scenes"][2]["role"] = "lead"
    raw["scenes"][2]["claim_refs"] = ["CL-001"]

    canonical = canonicalize_planning(
        raw,
        ledger,
        settings(),
        cover_ref=None,
    )

    assert canonical["brief"]["required_claim_refs"] == ["CL-001", "CL-002"]
    assert canonical["brief"]["included_supporting_claim_refs"] == ["CL-003"]
    assert canonical["brief"]["omitted_supporting_claim_refs"] == []
    assert [scene["id"] for scene in canonical["plan"]["scenes"]] == [
        "S-01",
        "S-02",
        "S-03",
    ]
    assert canonical["plan"]["scenes"][0]["role"] == "lead"
    assert canonical["plan"]["scenes"][-1]["role"] == "close"
    assert validate_planning(canonical, ledger, settings()) == []


def test_minimal_planner_proposal_compiles_to_deterministic_skeleton():
    ledger = normalize_evidence_ledger(evidence_response())
    legacy = planning_response()
    brief = legacy["brief"]
    proposal = {
        "language": brief["language"],
        "headline": brief["headline"],
        "subheadline": brief["subheadline"],
        "summary": brief["summary"],
        "keywords": brief["keywords"],
        "central_question": brief["central_question"],
        "answer": brief["answer"],
        "why_it_matters": brief["why_it_matters"],
        "selected_supporting_claim_refs": ["CL-003"],
        "summary_claim_refs": brief["summary_claim_refs"],
        "scene_proposals": [
            {
                field: scene[field]
                for field in (
                    "label",
                    "purpose",
                    "title_hint",
                    "claim_refs",
                    "target_chars",
                    "transition",
                )
            }
            for scene in legacy["scenes"]
        ],
    }

    normalized = normalize_planner_proposal(proposal)
    canonical = canonicalize_planning(
        proposal,
        ledger,
        settings(),
        cover_ref=None,
    )

    assert "id" not in proposal["scene_proposals"][0]
    assert normalized["brief"]["included_supporting_claim_refs"] == ["CL-003"]
    assert [scene["id"] for scene in canonical["plan"]["scenes"]] == [
        "S-01",
        "S-02",
        "S-03",
    ]
    assert canonical["plan"]["scenes"][0]["role"] == "lead"
    assert canonical["plan"]["scenes"][-1]["role"] == "close"
    assert validate_planning(canonical, ledger, settings()) == []


def test_scene_overlay_uses_plan_identity_and_blanks_non_owned_fields():
    plan_slice = normalize_planning(planning_response())["plan"]["scenes"]
    raw = scene_response(repaired=True)
    raw["scenes"] = list(reversed(raw["scenes"]))
    raw["scenes"][0].update(
        {
            "id": "S-99",
            "role": "lead",
            "claim_refs": ["CL-999"],
            "image_ref": 99,
            "summary": "must be removed",
        }
    )

    scenes = overlay_scene_invariants(raw, plan_slice)

    assert [scene["id"] for scene in scenes] == ["S-01", "S-02", "S-03"]
    for scene, planned in zip(scenes, plan_slice):
        for field in ("id", "role", "claim_refs", "image_ref", "chart_ref"):
            assert scene[field] == planned[field]
        if scene["role"] != "lead":
            assert scene["summary"] == ""


@pytest.mark.asyncio
async def test_planning_validation_feedback_corrects_once_and_saves_only_valid_result():
    ledger, invalid, corrected = planning_lead_reserve_case()
    repo = MemoryRepo()
    llm = ScriptedLLM([invalid, corrected])
    pipeline = EditorialPipeline(settings(), repo, llm)

    result = await pipeline._stage_call(
        "planning-correction",
        stage="planning",
        input_payload=ledger,
        system=PLANNING_SYSTEM_PROMPT,
        user_text="ORIGINAL_PLANNING_REQUEST",
        normalize=normalize_planning,
        validate=lambda value: validate_planning(value, ledger, settings()),
        validation_corrections=1,
    )

    assert result == normalize_planning(corrected)
    assert len(llm.calls) == 2
    assert llm.calls[0][1] == "ORIGINAL_PLANNING_REQUEST"
    correction_prompt = llm.calls[1][1]
    assert correction_prompt.startswith("ORIGINAL_PLANNING_REQUEST")
    assert "lead must reserve at least one non-summary claim for its body" in correction_prompt
    assert '"previous_output"' in correction_prompt
    artifact = repo.artifacts[("planning-correction", "planning")]
    assert artifact["status"] == "ok"
    assert artifact["attempts"] == 2


def test_validation_feedback_is_bounded_to_eight_errors():
    prompt = build_validation_feedback_prompt(
        "ORIGINAL",
        {"brief": {"headline": "kept"}},
        [f"validation-error-{index}" for index in range(10)],
    )

    assert "validation-error-7" in prompt
    assert "validation-error-8" not in prompt
    assert '"headline": "kept"' in prompt


@pytest.mark.asyncio
async def test_planning_second_invalid_fails_after_exactly_one_correction():
    ledger, invalid, _ = planning_lead_reserve_case()
    repo = MemoryRepo()
    llm = ScriptedLLM([invalid, invalid])
    pipeline = EditorialPipeline(settings(), repo, llm)

    with pytest.raises(PipelineStageError, match="lead must reserve"):
        await pipeline._stage_call(
            "planning-still-invalid",
            stage="planning",
            input_payload=ledger,
            system=PLANNING_SYSTEM_PROMPT,
            user_text="ORIGINAL_PLANNING_REQUEST",
            normalize=normalize_planning,
            validate=lambda value: validate_planning(value, ledger, settings()),
            validation_corrections=1,
        )

    assert len(llm.calls) == 2
    artifact = repo.artifacts[("planning-still-invalid", "planning")]
    assert artifact["status"] == "failed"
    assert artifact["attempts"] == 2


@pytest.mark.asyncio
async def test_planning_provider_error_is_not_sent_through_validation_correction():
    class ProviderFailureLLM:
        def __init__(self):
            self.calls = 0

        async def complete_json(
            self,
            system: str,
            user_text: str,
            images=None,
            *,
            output_schema=None,
        ):
            self.calls += 1
            raise OSError("transport unavailable")

    ledger, _, _ = planning_lead_reserve_case()
    repo = MemoryRepo()
    llm = ProviderFailureLLM()
    pipeline = EditorialPipeline(settings(), repo, llm)

    with pytest.raises(PipelineStageError) as captured:
        await pipeline._stage_call(
            "planning-provider-error",
            stage="planning",
            input_payload=ledger,
            system=PLANNING_SYSTEM_PROMPT,
            user_text="ORIGINAL_PLANNING_REQUEST",
            normalize=normalize_planning,
            validate=lambda value: validate_planning(value, ledger, settings()),
            validation_corrections=1,
        )

    assert llm.calls == 1
    assert isinstance(captured.value.__cause__, OSError)
    artifact = repo.artifacts[("planning-provider-error", "planning")]
    assert artifact["status"] == "failed"
    assert artifact["attempts"] == 1


def test_critic_counts_all_reader_visible_fields_without_requiring_duplication():
    assert "summary, body, secondary, and quote together" in CRITIC_SYSTEM_PROMPT
    assert "Never require the same claim or sentence" in CRITIC_SYSTEM_PROMPT
    assert "two or more of the same numeric facts with units" in CRITIC_SYSTEM_PROMPT
    assert "Divide the\nplanned claim details between lead summary and body" in SCENE_SYSTEM_PROMPT
    assert "linked visualization" in CRITIC_SYSTEM_PROMPT
    assert "Every non-chart scene must keep a non-empty body" in REPAIR_SYSTEM_PROMPT
    assert "Keep body+secondary+quote within the planned target_chars" in REPAIR_SYSTEM_PROMPT
    assert "Preserve every digit-form number" in SCENE_SYSTEM_PROMPT


def test_critic_payload_includes_visualizations_and_chart_omission_is_rejected():
    ledger = normalize_evidence_ledger(evidence_response_with_chart())
    raw_plan = planning_response()
    raw_plan["scenes"][1]["chart_ref"] = 0
    plan = normalize_planning(raw_plan)["plan"]
    prompt = build_critic_prompt(
        source_packet(), ledger, raw_plan["brief"], plan, scene_response()["scenes"]
    )
    assert '"visualizations"' in prompt
    assert "평균 체류 시간 변화" in prompt

    report = {
        "verdict": "revise",
        "issues": [{
            "scene_id": "S-02",
            "code": "omission",
            "message": "차트 수치가 prose에 없다.",
            "claim_refs": ["CL-002"],
            "repair_instruction": "body에 차트 수치를 넣는다.",
        }],
    }
    errors = validate_critic_report(
        report,
        scene_ids={"S-01", "S-02", "S-03"},
        claim_ids={"CL-001", "CL-002", "CL-003"},
        plan=plan,
    )
    assert "issues[0] chart claim omission is structurally invalid" in errors


def test_critic_issue_claims_must_belong_to_target_scene():
    plan = normalize_planning(planning_response())["plan"]
    report = {
        "verdict": "revise",
        "issues": [{
            "scene_id": "S-01",
            "code": "omission",
            "message": "다른 장면 claim을 요구한다.",
            "claim_refs": ["CL-002"],
            "repair_instruction": "CL-002를 추가한다.",
        }],
    }

    errors = validate_critic_report(
        report,
        scene_ids={"S-01", "S-02", "S-03"},
        claim_ids={"CL-001", "CL-002", "CL-003"},
        plan=plan,
    )

    assert "issues[0].claim_refs must belong to the target scene" in errors


def test_critic_claim_refs_are_projected_to_target_scene_ownership():
    plan = normalize_planning(planning_response())["plan"]
    base = {
        "verdict": "revise",
        "issues": [
            {
                "scene_id": "S-01",
                "code": "tone",
                "message": "표현을 점검한다.",
                "claim_refs": ["CL-002", "CL-001"],
                "repair_instruction": "근거 중심으로 고친다.",
            },
            {
                "scene_id": "S-02",
                "code": "tone",
                "message": "표현을 점검한다.",
                "claim_refs": ["CL-003"],
                "repair_instruction": "근거 중심으로 고친다.",
            },
            {
                "scene_id": "S-03",
                "code": "flow",
                "message": "흐름을 점검한다.",
                "claim_refs": [],
                "repair_instruction": "연결을 분명히 한다.",
            },
            {
                "scene_id": "S-99",
                "code": "tone",
                "message": "대상을 확인한다.",
                "claim_refs": ["CL-999"],
                "repair_instruction": "대상을 확인한다.",
            },
        ],
    }

    report = canonicalize_critic_report(base, plan)

    assert report["issues"][0]["claim_refs"] == ["CL-001"]
    assert report["issues"][1]["claim_refs"] == ["CL-002"]
    assert report["issues"][2]["claim_refs"] == []
    assert report["issues"][3]["claim_refs"] == ["CL-999"]


def test_close_scene_may_have_empty_transition():
    raw = planning_response()
    raw["scenes"][-1]["transition"] = ""

    errors = validate_planning(
        normalize_planning(raw), normalize_evidence_ledger(evidence_response()), settings()
    )

    assert errors == []


def test_non_close_scene_still_requires_transition():
    raw = planning_response()
    raw["scenes"][1]["transition"] = ""

    errors = validate_planning(
        normalize_planning(raw), normalize_evidence_ledger(evidence_response()), settings()
    )

    assert "scenes[1].transition is empty" in errors


def test_close_scene_must_own_a_claim_even_when_transition_is_empty():
    raw = planning_response()
    raw["scenes"][-1]["claim_refs"] = []
    raw["scenes"][-1]["transition"] = ""

    errors = validate_planning(
        normalize_planning(raw), normalize_evidence_ledger(evidence_response()), settings()
    )

    assert "scenes[2].claim_refs is empty" in errors


def test_chart_scene_claims_and_points_are_linked_by_exact_evidence():
    raw = planning_response()
    raw["scenes"][1]["chart_ref"] = 0
    raw["scenes"][1]["target_chars"] = 60

    errors = validate_planning(
        normalize_planning(raw),
        normalize_evidence_ledger(evidence_response_with_chart()),
        settings(),
    )

    assert errors == []


def test_non_chart_scene_still_rejects_target_below_80():
    raw = planning_response()
    raw["scenes"][1]["target_chars"] = 60

    errors = validate_planning(
        normalize_planning(raw),
        normalize_evidence_ledger(evidence_response()),
        settings(),
    )

    assert "scenes[1].non-chart target_chars must be 80..420" in errors


def test_chart_scene_rejects_unrelated_claim_even_with_exact_partition():
    raw = planning_response()
    raw["scenes"][1]["chart_ref"] = 0
    raw["scenes"][1]["claim_refs"] = ["CL-003"]
    raw["scenes"][2]["claim_refs"] = ["CL-002"]

    errors = validate_planning(
        normalize_planning(raw),
        normalize_evidence_ledger(evidence_response_with_chart()),
        settings(),
    )

    assert "scenes[1].claim_ref CL-003 is not represented by chart points" in errors
    assert any("chart point 0:0 is not represented by claim_refs" in error for error in errors)


def test_chart_scene_rejects_empty_claim_refs():
    raw = planning_response()
    raw["scenes"][1]["chart_ref"] = 0
    raw["scenes"][1]["claim_refs"] = []
    raw["scenes"][2]["claim_refs"] = ["CL-002", "CL-003"]

    errors = validate_planning(
        normalize_planning(raw),
        normalize_evidence_ledger(evidence_response_with_chart()),
        settings(),
    )

    assert "scenes[1].claim_refs is empty" in errors
    assert any("chart point 0:0 is not represented by claim_refs" in error for error in errors)


def test_scene_batch_rejects_omitted_claim_numeric_atoms():
    ledger = normalize_evidence_ledger(evidence_response())
    plan = normalize_planning(planning_response())["plan"]
    raw = scene_response(repaired=True)
    raw["scenes"][1]["title"] = "늘어난 평균 체류 시간"
    raw["scenes"][1]["body"] = "설치 뒤 평균 체류 시간이 늘었다."

    errors = validate_scene_batch(
        normalize_scene_batch(raw), plan["scenes"], ledger=ledger
    )

    assert "scenes[1].claim_ref CL-002 omits numeric atoms: 13, 8" in errors


def test_evidence_rejects_claim_numeric_atoms_absent_from_exact_evidence():
    source = source_packet()
    ledger = normalize_evidence_ledger(evidence_response(), source)
    ledger["claims"][1]["text"] = (
        "설치 뒤 평균 체류 시간이 8분에서 99분으로 늘었다."
    )

    errors = validate_evidence_ledger(ledger, source)

    assert any(
        "claims[1].text numeric atoms absent from evidence: 99" in error
        for error in errors
    )
    assert any(
        "allowed evidence numeric atoms: 13, 8" in error
        and "state the source comparison directly" in error
        for error in errors
    )


def test_evidence_accepts_bounded_english_number_word_digit_equivalence():
    body = (
        "The county installed sensors in twelve public schools. "
        "The test measured ventilation during the spring term."
    )
    source = build_source_packet(
        title="School sensors",
        source_url="https://news.example/sensors",
        domain="news.example",
        body_text=body,
        images=[],
        visible_image_refs=set(),
        output_language="en",
    )
    raw = {
        "language": "en",
        "claims": [
            {
                "id": "CL-001",
                "kind": "number",
                "importance": "essential",
                "text": "The county installed sensors in 12 public schools.",
                "evidence": [
                    {
                        "paragraph_id": "P-001",
                        "quote": (
                            "The county installed sensors in twelve public schools."
                        ),
                    }
                ],
            }
        ],
        "media": [],
        "visualizations": [],
    }

    ledger = normalize_evidence_ledger(raw, source)

    assert validate_evidence_ledger(ledger, source) == []


def test_evidence_accepts_titlecase_english_months_as_korean_month_numbers():
    body = (
        "The share fell from 18 percent in March to 11 percent in May. "
        "Measurements will repeat from November through January."
    )
    source = build_source_packet(
        title="Monthly ventilation result",
        source_url="https://news.example/months",
        domain="news.example",
        body_text=body,
        images=[],
        visible_image_refs=set(),
        output_language="ko",
    )
    raw = {
        "language": "ko",
        "claims": [
            {
                "id": "CL-001",
                "kind": "number",
                "importance": "essential",
                "text": "비중은 3월 18퍼센트에서 5월 11퍼센트로 줄었다.",
                "evidence": [{"paragraph_id": "P-001", "quote": body}],
            },
            {
                "id": "CL-002",
                "kind": "fact",
                "importance": "supporting",
                "text": "측정은 11월부터 1월까지 반복된다.",
                "evidence": [{"paragraph_id": "P-001", "quote": body}],
            },
        ],
        "media": [],
        "visualizations": [],
    }

    ledger = normalize_evidence_ledger(raw, source)

    assert validate_evidence_ledger(ledger, source) == []


@pytest.mark.parametrize(
    ("body", "claim_text", "unsupported"),
    [
        ("The county may repeat the test.", "카운티는 5월에 시험을 반복한다.", "5"),
        ("The result was measured in March.", "결과는 3곳에서 측정됐다.", "3"),
    ],
)
def test_evidence_month_equivalence_rejects_modal_or_non_month_context(
    body,
    claim_text,
    unsupported,
):
    source = build_source_packet(
        title="Bounded month equivalence",
        source_url="https://news.example/month-boundary",
        domain="news.example",
        body_text=body,
        images=[],
        visible_image_refs=set(),
        output_language="ko",
    )
    raw = {
        "language": "ko",
        "claims": [
            {
                "id": "CL-001",
                "kind": "fact",
                "importance": "essential",
                "text": claim_text,
                "evidence": [{"paragraph_id": "P-001", "quote": body}],
            }
        ],
        "media": [],
        "visualizations": [],
    }

    errors = validate_evidence_ledger(normalize_evidence_ledger(raw, source), source)

    assert any(
        f"numeric atoms absent from evidence: {unsupported}" in error
        for error in errors
    )


def test_evidence_does_not_treat_compound_twenty_one_as_digit_21():
    body = "The program covered twenty-one public schools."
    source = build_source_packet(
        title="School program",
        source_url="https://news.example/program",
        domain="news.example",
        body_text=body,
        images=[],
        visible_image_refs=set(),
        output_language="en",
    )
    raw = {
        "language": "en",
        "claims": [
            {
                "id": "CL-001",
                "kind": "number",
                "importance": "essential",
                "text": "The program covered 21 public schools.",
                "evidence": [
                    {
                        "paragraph_id": "P-001",
                        "quote": body,
                    }
                ],
            }
        ],
        "media": [],
        "visualizations": [],
    }

    ledger = normalize_evidence_ledger(raw, source)
    errors = validate_evidence_ledger(ledger, source)

    assert any(
        "claims[0].text numeric atoms absent from evidence: 21" in error
        for error in errors
    )


def test_auto_language_projects_ungrounded_claim_number_to_exact_evidence():
    body = (
        "The share above the alert level fell from 18 percent in March "
        "to 11 percent in May."
    )
    source = build_source_packet(
        title="Ventilation result",
        source_url="https://news.example/ventilation",
        domain="news.example",
        body_text=body,
        images=[],
        visible_image_refs=set(),
        output_language="auto",
    )
    raw = {
        "language": "en",
        "claims": [
            {
                "id": "CL-001",
                "kind": "number",
                "importance": "essential",
                "text": "The share fell by 7 percentage points.",
                "evidence": [{"paragraph_id": "P-001", "quote": body}],
            }
        ],
        "media": [],
        "visualizations": [],
    }

    projected = project_claim_numeric_grounding(
        normalize_evidence_ledger(raw, source),
        source,
    )

    assert projected["claims"][0]["text"] == body
    assert validate_evidence_ledger(projected, source) == []


def test_explicit_output_language_does_not_replace_claim_with_source_text():
    body = (
        "The share above the alert level fell from 18 percent in March "
        "to 11 percent in May."
    )
    source = build_source_packet(
        title="Ventilation result",
        source_url="https://news.example/ventilation-explicit",
        domain="news.example",
        body_text=body,
        images=[],
        visible_image_refs=set(),
        output_language="ko",
    )
    raw = {
        "language": "ko",
        "claims": [
            {
                "id": "CL-001",
                "kind": "number",
                "importance": "essential",
                "text": "기준 초과 비중이 7%포인트 줄었다.",
                "evidence": [{"paragraph_id": "P-001", "quote": body}],
            }
        ],
        "media": [],
        "visualizations": [],
    }

    projected = project_claim_numeric_grounding(
        normalize_evidence_ledger(raw, source),
        source,
    )

    assert projected["claims"][0]["text"] == "기준 초과 비중이 7%포인트 줄었다."
    errors = validate_evidence_ledger(projected, source)
    assert any("numeric atoms absent from evidence: 7" in error for error in errors)
    assert any(
        "allowed evidence numeric atoms: 11, 18" in error
        and "do not calculate a difference" in error
        and "state the source comparison directly" in error
        for error in errors
    )
    correction_prompt = build_validation_feedback_prompt(
        "ORIGINAL_EVIDENCE_REQUEST",
        projected,
        errors,
    )
    assert "allowed evidence numeric atoms: 11, 18" in correction_prompt
    assert "state the source comparison directly" in correction_prompt


def test_auto_language_numeric_projection_keeps_over_500_char_fallback_terminal():
    body = "The source reported 18 percent and 11 percent. " + ("detail " * 70)
    source = build_source_packet(
        title="Long ventilation result",
        source_url="https://news.example/ventilation-long",
        domain="news.example",
        body_text=body,
        images=[],
        visible_image_refs=set(),
        output_language="auto",
    )
    raw = {
        "language": "en",
        "claims": [
            {
                "id": "CL-001",
                "kind": "number",
                "importance": "essential",
                "text": "The difference was 7 percentage points.",
                "evidence": [{"paragraph_id": "P-001", "quote": body}],
            }
        ],
        "media": [],
        "visualizations": [],
    }

    projected = project_claim_numeric_grounding(
        normalize_evidence_ledger(raw, source),
        source,
    )

    assert len(body) > 500
    assert projected["claims"][0]["text"] == raw["claims"][0]["text"]
    assert any(
        "numeric atoms absent from evidence: 7" in error
        for error in validate_evidence_ledger(projected, source)
    )


def test_numeric_grounding_appends_canonical_claim_text_within_budget():
    ledger = normalize_evidence_ledger(evidence_response())
    plan = normalize_planning(planning_response())["plan"]
    raw = scene_response(repaired=True)["scenes"][1]
    raw["title"] = "늘어난 평균 체류 시간"
    raw["body"] = "설치 뒤 평균 체류 시간이 늘었다."

    projected = project_scene_numeric_grounding(
        [raw],
        [plan["scenes"][1]],
        ledger,
    )

    assert projected[0]["body"] == "설치 뒤 평균 체류 시간이 늘었다."
    assert "8분에서 13분" in projected[0]["secondary"]
    assert validate_scene_batch(
        projected,
        [plan["scenes"][1]],
        ledger=ledger,
    ) == []


def test_numeric_grounding_uses_assigned_claim_body_when_append_exceeds_budget():
    ledger = normalize_evidence_ledger(evidence_response())
    plan = normalize_planning(planning_response())["plan"]
    plan["scenes"][1]["target_chars"] = 80
    raw = scene_response(repaired=True)["scenes"][1]
    raw["title"] = "늘어난 평균 체류 시간"
    raw["body"] = "가" * 70

    projected = project_scene_numeric_grounding(
        [raw],
        [plan["scenes"][1]],
        ledger,
    )

    assert projected[0]["body"] == ledger["claims"][1]["text"]
    assert projected[0]["secondary"] == ""
    assert validate_scene_batch(
        projected,
        [plan["scenes"][1]],
        ledger=ledger,
    ) == []


def test_numeric_grounding_body_fallback_accepts_more_than_360_chars():
    ledger = normalize_evidence_ledger(evidence_response())
    long_claim = "8분에서 13분으로 늘었다. "
    long_claim += "가" * (361 - len(long_claim))
    ledger["claims"][1]["text"] = long_claim
    ledger["claims"][1]["evidence"] = [
        {"paragraph_id": "P-002", "quote": long_claim}
    ]
    plan = normalize_planning(planning_response())["plan"]
    plan["scenes"][1]["target_chars"] = 420
    raw = scene_response(repaired=True)["scenes"][1]
    raw["title"] = "늘어난 평균 체류 시간"
    raw["body"] = "가" * 300

    projected = project_scene_numeric_grounding(
        [raw],
        [plan["scenes"][1]],
        ledger,
    )

    assert projected[0]["body"] == long_claim
    assert len(projected[0]["body"]) == 361
    assert projected[0]["secondary"] == ""
    assert validate_scene_batch(
        projected,
        [plan["scenes"][1]],
        ledger=ledger,
    ) == []


def test_chart_visible_labels_and_raw_values_cover_claim_numeric_atoms():
    ledger = normalize_evidence_ledger(evidence_response_with_chart())
    raw_plan = planning_response()
    raw_plan["scenes"][1]["chart_ref"] = 0
    plan = normalize_planning(raw_plan)["plan"]
    raw_scenes = scene_response(repaired=True)
    raw_scenes["scenes"][1]["chart_ref"] = 0
    raw_scenes["scenes"][1]["body"] = ""

    assert validate_scene_batch(
        normalize_scene_batch(raw_scenes), plan["scenes"], ledger=ledger
    ) == []


@pytest.mark.asyncio
async def test_balanced_pipeline_compiles_v3_without_llm_critic_and_reuses_cache():
    repo = MemoryRepo()
    llm = ScriptedLLM([
        *evidence_stage_responses(),
        planning_response(),
        scene_response(repaired=True),
    ])
    pipeline = EditorialPipeline(settings(), repo, llm)
    source = source_packet()

    result = await pipeline.compose(
        "article-1", source=source, images=[], image_parts=[]
    )

    assert len(llm.calls) == 4
    assert [call[3] for call in llm.calls] == [
        "article_map_evidence_refs_v1",
        "article_map_evidence_claims_v1",
        "article_map_planner_v2",
        "article_map_scene_copy_v3",
    ]
    assert result["format_version"] == 3
    assert [scene["id"] for scene in result["scenes"]] == ["S-01", "S-02", "S-03"]
    assert result["required_claim_refs"] == ["CL-001", "CL-002"]
    assert result["selected_claim_refs"] == ["CL-001", "CL-002", "CL-003"]
    assert result["editorial"]["critic_verdict"] == "pass"
    assert repo.artifacts[("article-1", "critic")]["data"] == {
        "verdict": "pass",
        "issues": [],
        "mode": "deterministic_contract",
    }
    assert {stage for _, stage in repo.artifacts} >= {
        "source", "evidence_refs", "evidence", "planner", "skeleton", "brief", "plan",
        "scene:S-01", "scene:S-02", "scene:S-03", "critic",
    }

    again = await pipeline.compose(
        "article-1", source=source, images=[], image_parts=[]
    )
    assert again == result
    assert len(llm.calls) == 4


@pytest.mark.asyncio
async def test_english_source_compiles_korean_scenes_with_auditable_quote_pair():
    source = translated_source_packet()
    repo = MemoryRepo()
    llm = ScriptedLLM(
        [
            translated_evidence_selection_response(source),
            evidence_claim_text_response(),
            planning_response(),
            translated_scene_response(),
        ]
    )
    pipeline = EditorialPipeline(settings(), repo, llm)

    result = await pipeline.compose(
        "translated-e2e",
        source=source,
        images=[],
        image_parts=[],
    )

    assert result["language"] == "ko"
    scene_artifact = repo.artifacts[("translated-e2e", "scene:S-02")]["data"]
    assert scene_artifact["quote"] == (
        "설치 뒤 시범 구간의 평균 체류 시간은 8분에서 13분으로 늘었다."
    )
    assert scene_artifact["quote_source"] == TRANSLATED_EVIDENCE[1]
    assert scene_artifact["quote_evidence_ref"] == "CL-002/1"
    delivered_scene = next(scene for scene in result["scenes"] if scene["id"] == "S-02")
    assert delivered_scene["quote"] == scene_artifact["quote"]
    assert "quote_source" not in str(result)
    assert "quote_evidence_ref" not in str(result)
    assert TRANSLATED_EVIDENCE[1] in str(
        repo.artifacts[("translated-e2e", "evidence")]["data"]
    )
    assert "set `quote_evidence_ref` to the address" in llm.calls[3][1]
    assert "quote_evidence_ref" in str(llm.output_schemas[3].schema)
    assert "quote_source" not in str(llm.output_schemas[3].schema)
    scene_input_args = (
        planning_response()["brief"],
        normalize_evidence_ledger(evidence_response()),
        normalize_planning(planning_response())["plan"]["scenes"][0],
    )
    assert "translated" not in pipeline._scene_input(*scene_input_args)
    assert pipeline._scene_input(
        *scene_input_args,
        translated=True,
    )["translated"] is True

    again = await pipeline.compose(
        "translated-e2e",
        source=source,
        images=[],
        image_parts=[],
    )
    assert again == result
    assert len(llm.calls) == 4


@pytest.mark.asyncio
async def test_pipeline_compiles_social_cover_without_body_image_directive():
    source, images = image_source_packet()
    llm = ScriptedLLM([
        *evidence_stage_responses(),
        planning_response(),
        scene_response(repaired=True),
        {"verdict": "pass", "issues": []},
    ])
    pipeline = EditorialPipeline(settings(), MemoryRepo(), llm)

    result = await pipeline.compose(
        "article-cover", source=source, images=images, image_parts=[]
    )

    assert result["cover_image_ref"] == 0
    assert result["used_refs"] == []
    assert "<img:" not in result["body_markdown"]


@pytest.mark.asyncio
async def test_scene_batch_validation_feedback_corrects_prose_budget_once():
    invalid = scene_response_over_budget()
    repo = MemoryRepo()
    llm = ScriptedLLM([
        *evidence_stage_responses(),
        planning_response(),
        invalid,
        scene_response(repaired=True),
    ])
    pipeline = EditorialPipeline(settings(), repo, llm)

    result = await pipeline.compose(
        "scene-budget-correction",
        source=source_packet(),
        images=[],
        image_parts=[],
    )

    assert len(llm.calls) == 5
    assert result["scenes"][1]["secondary"] == ""
    correction_prompt = llm.calls[4][1]
    assert "S-02: scenes[0] prose exceeds planned target_chars" in correction_prompt
    assert '"previous_output": {"scenes":' in correction_prompt
    artifact = repo.artifacts[("scene-budget-correction", "scene:S-02")]
    assert artifact["status"] == "ok"
    assert artifact["attempts"] == 2


@pytest.mark.asyncio
async def test_scene_worker_checkpoints_valid_siblings_and_retries_only_invalid_scene():
    invalid = scene_response_over_budget()
    repaired_s02 = scene_response(repaired=True)["scenes"][1]
    repaired_copy = {
        "scenes": [
            {
                "scene_id": "S-02",
                "title": repaired_s02["title"],
                "summary": "",
                "body": repaired_s02["body"],
                "secondary": repaired_s02["secondary"],
                "quote": repaired_s02["quote"],
            }
        ]
    }
    repo = MemoryRepo()
    llm = ScriptedLLM(
        [
            *evidence_stage_responses(),
            planning_response(),
            invalid,
            repaired_copy,
        ]
    )
    pipeline = EditorialPipeline(settings(), repo, llm)

    result = await pipeline.compose(
        "scene-partial-salvage",
        source=source_packet(),
        images=[],
        image_parts=[],
    )

    assert [scene["id"] for scene in result["scenes"]] == ["S-01", "S-02", "S-03"]
    assert llm.calls[4][3] == "article_map_scene_copy_v3"
    assert '"id":"S-02"' in llm.calls[4][1]
    assert '"id":"S-01"' not in llm.calls[4][1]
    assert '"id":"S-03"' not in llm.calls[4][1]
    assert repo.artifacts[("scene-partial-salvage", "scene:S-01")]["attempts"] == 1
    assert repo.artifacts[("scene-partial-salvage", "scene:S-02")]["attempts"] == 2
    assert repo.artifacts[("scene-partial-salvage", "scene:S-03")]["attempts"] == 1


@pytest.mark.asyncio
async def test_scene_batch_third_invalid_fails_after_two_corrections():
    invalid = scene_response_over_budget()
    repo = MemoryRepo()
    llm = ScriptedLLM([
        *evidence_stage_responses(),
        planning_response(),
        invalid,
        invalid,
        invalid,
    ])
    pipeline = EditorialPipeline(settings(), repo, llm)

    with pytest.raises(PipelineStageError, match="prose exceeds planned target_chars"):
        await pipeline.compose(
            "scene-budget-still-invalid",
            source=source_packet(),
            images=[],
            image_parts=[],
        )

    assert len(llm.calls) == 6
    artifact = repo.artifacts[("scene-budget-still-invalid", "scene:S-02")]
    assert artifact["status"] == "failed"
    assert artifact["attempts"] == 3
    assert artifact["data"]["secondary"] == "가" * 200


@pytest.mark.asyncio
async def test_scene_initial_provider_error_does_not_trigger_correction():
    repo = MemoryRepo()
    llm = ScriptedLLM([
        *evidence_stage_responses(),
        planning_response(),
        OSError("scene transport unavailable"),
    ])
    pipeline = EditorialPipeline(settings(), repo, llm)

    with pytest.raises(PipelineStageError) as captured:
        await pipeline.compose(
            "scene-provider-error",
            source=source_packet(),
            images=[],
            image_parts=[],
        )

    assert len(llm.calls) == 4
    assert isinstance(captured.value.__cause__, OSError)
    artifact = repo.artifacts[("scene-provider-error", "scene:S-02")]
    assert artifact["attempts"] == 1
    assert artifact["data"] is None


@pytest.mark.asyncio
async def test_scene_correction_provider_error_preserves_previous_candidate():
    invalid = scene_response_over_budget()
    repo = MemoryRepo()
    llm = ScriptedLLM([
        *evidence_stage_responses(),
        planning_response(),
        invalid,
        OSError("correction transport unavailable"),
    ])
    pipeline = EditorialPipeline(settings(), repo, llm)

    with pytest.raises(PipelineStageError) as captured:
        await pipeline.compose(
            "scene-correction-provider-error",
            source=source_packet(),
            images=[],
            image_parts=[],
        )

    assert len(llm.calls) == 5
    assert isinstance(captured.value.__cause__, OSError)
    artifact = repo.artifacts[(
        "scene-correction-provider-error",
        "scene:S-02",
    )]
    assert artifact["attempts"] == 2
    assert artifact["data"]["secondary"] == "가" * 200


@pytest.mark.asyncio
async def test_subjective_critic_revision_is_not_called_or_used_as_a_hard_gate():
    original = scene_response(repaired=False)
    issue = {
        "scene_id": "S-01",
        "code": "duplication",
        "message": "요약과 본문이 의미상 반복된다.",
        "claim_refs": ["CL-001"],
        "repair_instruction": "도입을 다시 쓴다.",
    }
    llm = ScriptedLLM([
        *evidence_stage_responses(),
        planning_response(),
        original,
        {"verdict": "revise", "issues": [issue]},
    ])
    repo = MemoryRepo()
    pipeline = EditorialPipeline(settings(MAP_MAX_CRITIC_ROUNDS=1), repo, llm)

    result = await pipeline.compose(
        "subjective-critic-ignored",
        source=source_packet(),
        images=[],
        image_parts=[],
    )

    assert len(llm.calls) == 4
    assert [call[3] for call in llm.calls] == [
        "article_map_evidence_refs_v1",
        "article_map_evidence_claims_v1",
        "article_map_planner_v2",
        "article_map_scene_copy_v3",
    ]
    assert "놀라운 대박이다." in result["scenes"][1]["body"]
    assert repo.artifacts[("subjective-critic-ignored", "critic")]["data"][
        "mode"
    ] == "deterministic_contract"


@pytest.mark.asyncio
async def test_contract_critic_fails_closed_on_render_breaking_scene_shape():
    ledger = normalize_evidence_ledger(evidence_response())
    plan = normalize_planning(planning_response())["plan"]
    scenes = normalize_scene_batch(scene_response(repaired=True))
    scenes[1]["body"] = ""
    repo = MemoryRepo()
    llm = ScriptedLLM([])
    pipeline = EditorialPipeline(settings(), repo, llm)

    with pytest.raises(PipelineStageError) as captured:
        await pipeline._contract_critic(
            "contract-red",
            ledger=ledger,
            plan=plan,
            scenes=scenes,
        )

    assert captured.value.stage == "critic"
    assert captured.value.error_code == "critic_contract_invalid"
    assert captured.value.retryable is False
    assert any("body is required" in error for error in captured.value.errors)
    assert llm.calls == []
    artifact = repo.artifacts[("contract-red", "critic")]
    assert artifact["status"] == "failed"
    assert artifact["data"] is None


@pytest.mark.asyncio
async def test_deterministic_contract_critic_is_cached_without_llm_calls():
    ledger = normalize_evidence_ledger(evidence_response())
    plan = normalize_planning(planning_response())["plan"]
    scenes = normalize_scene_batch(scene_response(repaired=True))
    repo = MemoryRepo()
    llm = ScriptedLLM([])
    pipeline = EditorialPipeline(settings(), repo, llm)

    first = await pipeline._contract_critic(
        "contract-cache",
        ledger=ledger,
        plan=plan,
        scenes=scenes,
    )
    second = await pipeline._contract_critic(
        "contract-cache",
        ledger=ledger,
        plan=plan,
        scenes=scenes,
    )

    expected = {
        "verdict": "pass",
        "issues": [],
        "mode": "deterministic_contract",
    }
    assert first == expected
    assert second == expected
    assert llm.calls == []
    assert repo.artifacts[("contract-cache", "critic")]["attempts"] == 1


def test_repair_prompt_requires_one_scene_and_exact_outer_schema():
    planning = planning_response()
    planned = [planning["scenes"][1]]
    current = [scene_response(repaired=False)["scenes"][1]]
    issue = {
        "scene_id": "S-02",
        "code": "tone",
        "message": "과장 표현을 제거한다.",
        "claim_refs": ["CL-002"],
        "repair_instruction": "근거 수치만 남긴다.",
    }

    prompt = build_repair_prompt(
        planning["brief"],
        evidence_response(),
        planned,
        current,
        [issue],
    )

    assert "Repair exactly scene S-02" in prompt
    assert '"expected_scene_id":"S-02"' in prompt
    assert "outer object must have exactly one `scenes`" in REPAIR_SYSTEM_PROMPT
    assert "server-provided structured output schema is authoritative" in (
        REPAIR_SYSTEM_PROMPT
    )
    assert "server-provided structured output schema is authoritative" in (
        SCENE_SYSTEM_PROMPT
    )
    assert "quote_source" not in prompt
    assert "quote_evidence_ref" not in prompt
    translated_prompt = build_repair_prompt(
        planning["brief"],
        evidence_response(),
        planned,
        current,
        [issue],
        translated=True,
    )
    assert "set `quote_evidence_ref` to the address" in translated_prompt
    with pytest.raises(ValueError, match="exactly one"):
        build_repair_prompt(
            planning["brief"],
            evidence_response(),
            planning["scenes"][:2],
            scene_response()["scenes"][:2],
            [issue],
        )


@pytest.mark.asyncio
async def test_translated_repair_threads_prompt_schema_overlay_and_validation():
    raw_ledger = evidence_response()
    raw_ledger["claims"][1]["evidence"] = [
        {"paragraph_id": "P-002", "quote": TRANSLATED_EVIDENCE[1]}
    ]
    ledger = normalize_evidence_ledger(raw_ledger)
    normalized = normalize_planning(planning_response())
    plan = normalized["plan"]
    scenes = normalize_scene_batch(translated_scene_response())
    issue = {
        "scene_id": "S-02",
        "code": "tone",
        "message": "근거 표현을 정리한다.",
        "claim_refs": ["CL-002"],
        "repair_instruction": "근거에 맞춰 문장을 다시 쓴다.",
    }
    repaired_copy = {
        "scenes": [
            {
                "scene_id": "S-02",
                "title": "머무는 시간은 8분에서 13분으로",
                "summary": "",
                "body": "설치 뒤 시범 구간의 평균 체류 시간은 8분에서 13분으로 늘었다.",
                "secondary": "",
                "quote": "설치 뒤 평균 체류 시간이 8분에서 13분으로 늘었다.",
                "quote_evidence_ref": "CL-002/1",
            }
        ]
    }
    repo = MemoryRepo()
    llm = ScriptedLLM([repaired_copy])
    pipeline = EditorialPipeline(settings(), repo, llm)

    repaired = await pipeline._repair(
        "translated-repair",
        brief=normalized["brief"],
        ledger=ledger,
        plan=plan,
        scenes=scenes,
        issues=[issue],
        translated=True,
    )

    assert "set `quote_evidence_ref` to the address" in llm.calls[0][1]
    assert llm.output_schemas[0] is not None
    assert "quote_evidence_ref" in str(llm.output_schemas[0].schema)
    assert "quote_source" not in str(llm.output_schemas[0].schema)
    repaired_scene = next(scene for scene in repaired if scene["id"] == "S-02")
    assert repaired_scene["quote"] == repaired_copy["scenes"][0]["quote"]
    assert repaired_scene["quote_source"] == TRANSLATED_EVIDENCE[1]
    artifact = repo.artifacts[("translated-repair", "repair:S-02")]
    assert artifact["status"] == "ok"
    assert artifact["data"]["quote_source"] == TRANSLATED_EVIDENCE[1]


@pytest.mark.asyncio
async def test_translated_contract_critic_does_not_reuse_plain_cache_pass():
    raw_ledger = evidence_response()
    raw_ledger["claims"][1]["evidence"] = [
        {"paragraph_id": "P-002", "quote": TRANSLATED_EVIDENCE[1]}
    ]
    ledger = normalize_evidence_ledger(raw_ledger)
    plan = normalize_planning(planning_response())["plan"]
    raw_scenes = translated_scene_response()
    raw_scenes["scenes"][1]["quote"] = "평균 체류 시간은 99분이었다."
    scenes = overlay_scene_invariants(
        raw_scenes, plan["scenes"], translated=True, ledger=ledger
    )
    repo = MemoryRepo()
    pipeline = EditorialPipeline(settings(), repo, ScriptedLLM([]))

    assert (
        await pipeline._contract_critic(
            "translated-critic-cache",
            ledger=ledger,
            plan=plan,
            scenes=scenes,
            translated=False,
        )
    )["verdict"] == "pass"

    with pytest.raises(PipelineStageError, match="99"):
        await pipeline._contract_critic(
            "translated-critic-cache",
            ledger=ledger,
            plan=plan,
            scenes=scenes,
            translated=True,
        )


@pytest.mark.asyncio
async def test_multiple_subjective_critic_targets_do_not_trigger_repairs():
    original = scene_response(repaired=False)
    issues = [
        {
            "scene_id": "S-01",
            "code": "flow",
            "message": "도입을 다듬는다.",
            "claim_refs": ["CL-001"],
            "repair_instruction": "근거 중심으로 다듬는다.",
        },
        {
            "scene_id": "S-02",
            "code": "tone",
            "message": "과장을 제거한다.",
            "claim_refs": ["CL-002"],
            "repair_instruction": "과장 표현을 제거한다.",
        },
    ]
    llm = ScriptedLLM([
        *evidence_stage_responses(),
        planning_response(),
        original,
        {"verdict": "revise", "issues": issues},
    ])
    pipeline = EditorialPipeline(settings(MAP_MAX_CRITIC_ROUNDS=1), MemoryRepo(), llm)

    result = await pipeline.compose(
        "two-repairs", source=source_packet(), images=[], image_parts=[]
    )

    repair_calls = [call for call in llm.calls if call[0] == REPAIR_SYSTEM_PROMPT]
    assert repair_calls == []
    assert len(llm.calls) == 4
    assert [scene["id"] for scene in result["scenes"]] == ["S-01", "S-02", "S-03"]


def test_scene_id_mismatch_error_contains_expected_and_got_only():
    plan = planning_response()["scenes"][1:2]
    wrong = deepcopy(scene_response(repaired=True)["scenes"][1])
    wrong["id"] = "S-03"

    errors = validate_scene_batch([wrong], plan, ledger=evidence_response())

    assert errors == [
        "scene batch IDs/order must match the plan slice: "
        "expected=['S-02'] got=['S-03']"
    ]


def test_scene_batch_accepts_body_over_360_within_planned_budget():
    scene = deepcopy(scene_response(repaired=True)["scenes"][1])
    scene["body"] += "가" * (361 - len(scene["body"]))
    plan = deepcopy(planning_response()["scenes"][1:2])
    plan[0]["target_chars"] = 420

    errors = validate_scene_batch(
        [scene],
        plan,
        ledger=evidence_response(),
    )

    assert len(scene["body"]) == 361
    assert errors == []


@pytest.mark.asyncio
async def test_contract_critic_never_mutates_valid_scenes():
    ledger = normalize_evidence_ledger(evidence_response())
    plan = normalize_planning(planning_response())["plan"]
    scenes = normalize_scene_batch(scene_response(repaired=False))
    before = deepcopy(scenes)
    repo = MemoryRepo()
    llm = ScriptedLLM([])
    pipeline = EditorialPipeline(settings(), repo, llm)

    report = await pipeline._contract_critic(
        "contract-no-mutation",
        ledger=ledger,
        plan=plan,
        scenes=scenes,
    )

    assert report["mode"] == "deterministic_contract"
    assert scenes == before
    assert llm.calls == []


@pytest.mark.asyncio
async def test_thin_source_fails_before_any_llm_call():
    llm = ScriptedLLM([])
    pipeline = EditorialPipeline(settings(), MemoryRepo(), llm)
    with pytest.raises(PipelineStageError, match="source"):
        await pipeline.compose(
            "thin", source=source_packet("짧은 기사"), images=[], image_parts=[]
        )
    assert llm.calls == []


@pytest.mark.asyncio
async def test_pipeline_calls_optional_rag_after_planning_and_keeps_compiler_path():
    class PassthroughRag:
        def __init__(self):
            self.calls = []

        async def augment(self, article_id, **kwargs):
            self.calls.append((article_id, kwargs))
            return SimpleNamespace(
                ledger=kwargs["ledger"],
                planning=kwargs["planning"],
                images=kwargs["images"],
            )

    rag = PassthroughRag()
    llm = ScriptedLLM(
        [
            *evidence_stage_responses(),
            planning_response(),
            scene_response(repaired=False),
            {"verdict": "pass", "issues": []},
        ]
    )
    pipeline = EditorialPipeline(settings(), MemoryRepo(), llm, image_rag=rag)

    result = await pipeline.compose(
        "rag-hook",
        source=source_packet(),
        images=[],
        image_parts=[],
    )

    assert result["format_version"] == 3
    assert len(rag.calls) == 1
    assert rag.calls[0][0] == "rag-hook"
    assert rag.calls[0][1]["vision"] is llm


@pytest.mark.asyncio
async def test_invalid_scene_candidate_is_preserved_for_diagnosis():
    invalid = scene_response(repaired=True)
    invalid["scenes"][0]["body"] = ""
    repo = MemoryRepo()
    pipeline = EditorialPipeline(
        settings(),
        repo,
        ScriptedLLM([
            *evidence_stage_responses(),
            planning_response(),
            invalid,
            invalid,
            invalid,
        ]),
    )

    with pytest.raises(PipelineStageError, match="body is required"):
        await pipeline.compose(
            "invalid-scene-candidate",
            source=source_packet(),
            images=[],
            image_parts=[],
        )

    artifact = repo.artifacts[("invalid-scene-candidate", "scene:S-01")]
    assert artifact["status"] == "failed"
    assert artifact["data"]["id"] == "S-01"
    assert artifact["data"]["body"] == ""


@pytest.mark.asyncio
async def test_critic_round_setting_does_not_enable_subjective_llm_rewrites():
    issue = {
        "scene_id": "S-01",
        "code": "duplication",
        "message": "의미가 반복된다.",
        "claim_refs": ["CL-001"],
        "repair_instruction": "도입을 다시 쓴다.",
    }
    llm = ScriptedLLM([
        *evidence_stage_responses(),
        planning_response(),
        scene_response(repaired=False),
        {"verdict": "revise", "issues": [issue]},
    ])
    repo = MemoryRepo()
    pipeline = EditorialPipeline(settings(MAP_MAX_CRITIC_ROUNDS=0), repo, llm)

    result = await pipeline.compose(
        "article-no-subjective-repair",
        source=source_packet(),
        images=[],
        image_parts=[],
    )

    assert len(llm.calls) == 4
    assert result["editorial"]["critic_verdict"] == "pass"
    assert repo.artifacts[("article-no-subjective-repair", "critic")]["data"][
        "mode"
    ] == "deterministic_contract"


def test_schema_contains_pipeline_checkpoint_table():
    assert "CREATE TABLE IF NOT EXISTS article_pipeline_artifacts" in SCHEMA_SQL
    assert "PRIMARY KEY (article_id, stage)" in SCHEMA_SQL


@pytest.mark.parametrize(
    ("count", "expected_sizes"),
    [
        (3, [3]),
        (4, [2, 2]),
        (5, [3, 2]),
        (6, [3, 3]),
        (7, [3, 2, 2]),
        (8, [3, 3, 2]),
        (9, [3, 3, 3]),
    ],
)
def test_fresh_scene_batches_are_balanced_without_singletons(count, expected_sizes):
    scenes = [{"id": f"S-{index + 1:02d}"} for index in range(count)]
    batches = balanced_scene_batches(scenes, 3)
    assert [len(batch) for batch in batches] == expected_sizes
    assert [scene for batch in batches for scene in batch] == scenes


def test_single_missing_scene_from_checkpoint_is_allowed():
    scene = {"id": "S-04"}
    assert balanced_scene_batches([scene], 3) == [[scene]]


def _parallel_scene_plan(count: int) -> dict:
    return {
        "scenes": [
            {
                "id": f"S-{index + 1:02d}",
                "claim_refs": [],
                "image_ref": None,
                "chart_ref": None,
            }
            for index in range(count)
        ]
    }


@pytest.mark.asyncio
async def test_scene_batches_run_concurrently_without_article_local_cap(monkeypatch):
    pipeline = EditorialPipeline(settings(), MemoryRepo(), ScriptedLLM([]))
    plan = _parallel_scene_plan(7)
    expected_batches = [
        ("S-01", "S-02", "S-03"),
        ("S-04", "S-05"),
        ("S-06", "S-07"),
    ]
    started: list[tuple[str, ...]] = []
    all_batches_started = asyncio.Event()

    async def cached(*_args, **_kwargs):
        return None

    async def compose_batch(_article_id, *, initial_plan_slice, **_kwargs):
        started.append(tuple(item["id"] for item in initial_plan_slice))
        if len(started) == len(expected_batches):
            all_batches_started.set()
        await all_batches_started.wait()
        return {
            item["id"]: {"id": item["id"]}
            for item in initial_plan_slice
        }

    monkeypatch.setattr(pipeline, "_cached", cached)
    monkeypatch.setattr(pipeline, "_compose_scene_batch", compose_batch)

    scenes = await asyncio.wait_for(
        pipeline._compose_scenes(
            "article-scene-fan-out",
            brief={},
            ledger={"claims": [], "media": [], "visualizations": []},
            plan=plan,
        ),
        timeout=1,
    )

    assert started == expected_batches
    assert [scene["id"] for scene in scenes] == [
        item["id"] for item in plan["scenes"]
    ]


@pytest.mark.asyncio
async def test_scene_batch_failure_waits_for_and_preserves_successful_sibling(
    monkeypatch,
):
    repo = MemoryRepo()
    pipeline = EditorialPipeline(settings(), repo, ScriptedLLM([]))
    plan = _parallel_scene_plan(4)
    both_batches_started = asyncio.Event()
    successful_sibling_finished = asyncio.Event()
    started: list[tuple[str, ...]] = []

    async def cached(*_args, **_kwargs):
        return None

    async def compose_batch(article_id, *, initial_plan_slice, hashes, **_kwargs):
        batch_ids = tuple(item["id"] for item in initial_plan_slice)
        started.append(batch_ids)
        if len(started) == 2:
            both_batches_started.set()
        await both_batches_started.wait()
        if batch_ids == ("S-01", "S-02"):
            raise PipelineStageError("scenes", ["first batch failed"])
        completed = {
            item["id"]: {"id": item["id"]}
            for item in initial_plan_slice
        }
        for scene_id, scene in completed.items():
            await pipeline._save(
                article_id,
                stage=f"scene:{scene_id}",
                input_hash=hashes[scene_id],
                status="ok",
                data=scene,
                error=None,
            )
        successful_sibling_finished.set()
        return completed

    monkeypatch.setattr(pipeline, "_cached", cached)
    monkeypatch.setattr(pipeline, "_compose_scene_batch", compose_batch)

    with pytest.raises(PipelineStageError, match="first batch failed"):
        await pipeline._compose_scenes(
            "article-scene-sibling-preservation",
            brief={},
            ledger={"claims": [], "media": [], "visualizations": []},
            plan=plan,
        )

    assert started == [("S-01", "S-02"), ("S-03", "S-04")]
    assert successful_sibling_finished.is_set()
    assert repo.artifacts[(
        "article-scene-sibling-preservation",
        "scene:S-03",
    )]["status"] == "ok"
