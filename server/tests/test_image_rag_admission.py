from __future__ import annotations

import hashlib
from copy import deepcopy

import pytest

from app.config import Settings
from app.editorial_pipeline import select_cover_image_ref, validate_planning, visible_image_views
from app.image_rag import (
    ImageRagService,
    RagCandidate,
    RagRetrieval,
)
from app.image_rag_prompts import (
    RAG_VERIFICATION_SYSTEM_PROMPT,
    build_rag_verification_prompt,
    normalize_rag_verdict,
    validate_rag_verdict,
)


def _settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "VECTOR_DATABASE_URL": "postgresql://vector.internal/rag",
        "OPENROUTER_API_KEY": "test-key",
    }
    values.update(overrides)
    return Settings(**values)


def _ledger_and_planning(count: int) -> tuple[dict, dict]:
    claim_ids = [f"CL-{index:03d}" for index in range(1, count + 1)]
    ledger = {
        "language": "ko",
        "claims": [
            {
                "id": claim_id,
                "kind": "fact",
                "importance": "essential",
                "text": f"exact claim {index}",
                "evidence": [],
            }
            for index, claim_id in enumerate(claim_ids, start=1)
        ],
        "media": [],
        "visualizations": [],
    }
    brief = {
        "language": "ko",
        "headline": "검증 기사",
        "subheadline": "",
        "summary": "첫 주장을 요약한다.",
        "keywords": ["검증", "이미지", "기사"],
        "central_question": "무슨 일이 있었나?",
        "answer": "검증된 일이 있었다.",
        "why_it_matters": "독자가 사실을 이해해야 한다.",
        "ordering_policy": "editorial_completeness",
        "required_claim_refs": claim_ids,
        "included_supporting_claim_refs": [],
        "omitted_supporting_claim_refs": [],
        "summary_claim_refs": [claim_ids[0]],
    }
    scenes = []
    for index, claim_id in enumerate(claim_ids, start=1):
        role = "lead" if index == 1 else ("close" if index == count else "evidence")
        scenes.append(
            {
                "id": f"S-{index:02d}",
                "role": role,
                "label": f"장면 {index}",
                "purpose": f"주장 {index} 설명",
                "title_hint": f"제목 {index}",
                "claim_refs": [claim_id],
                "image_ref": None,
                "chart_ref": None,
                "target_chars": 100,
                "transition": "다음으로" if role != "close" else "",
            }
        )
    planning = {"brief": brief, "plan": {"scenes": scenes}}
    assert validate_planning(planning, ledger, _settings()) == []
    return ledger, planning


def _candidate(index: int, data: bytes) -> RagCandidate:
    image_id = hashlib.sha256(data).hexdigest()
    return RagCandidate(
        image_id=image_id,
        scene_id=f"S-{index:02d}",
        claim_refs=(f"CL-{index:03d}",),
        similarity=0.90 - index / 100,
        origin_article_id=f"origin-{index}",
        origin_title=f"Origin {index}",
        origin_url=f"https://news.example/{index}",
        origin_domain="news.example",
        source_url=f"https://images.example/{index}.jpg",
        content_type="image/jpeg",
        byte_size=len(data),
        backend="db",
        storage_ref=None,
    )


class FakeRepo:
    def __init__(self, records):
        self.records = records

    async def get_image_record(self, image_id):
        return self.records.get(image_id)


class ScriptedVision:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, str, list, str | None]] = []

    async def complete_json(
        self,
        system,
        user_text,
        images,
        *,
        output_schema=None,
    ):
        self.calls.append(
            (
                system,
                user_text,
                list(images),
                output_schema.name if output_schema is not None else None,
            )
        )
        return self.responses.pop(0)


def _approval(candidate: RagCandidate) -> dict:
    return {
        "candidate_id": candidate.image_id,
        "scene_id": candidate.scene_id,
        "claim_refs": list(candidate.claim_refs),
        "caption": f"{candidate.scene_id} 현장",
        "reason": "이미지의 사건과 요청된 주장이 일치한다.",
    }


def test_verification_prompt_and_validator_require_exact_candidate_scene_claim():
    ledger, _ = _ledger_and_planning(3)
    candidate = _candidate(1, b"candidate")
    prompt = build_rag_verification_prompt(
        article_title="Current article",
        ledger=ledger,
        candidates=[candidate],
    )

    assert candidate.image_id in prompt
    assert candidate.origin_title in prompt
    assert "CL-001" in prompt and "exact claim 1" in prompt
    approvals = normalize_rag_verdict({"approved": [_approval(candidate)]})
    assert validate_rag_verdict(approvals, [candidate], ledger, missing_views=1) == []

    wrong = _approval(candidate)
    wrong["scene_id"] = "S-03"
    errors = validate_rag_verdict(
        normalize_rag_verdict({"approved": [wrong]}),
        [candidate],
        ledger,
        missing_views=1,
    )
    assert any("scene_id" in error for error in errors)


@pytest.mark.asyncio
async def test_admission_batches_four_then_one_and_merges_contiguous_rag_refs():
    ledger, planning = _ledger_and_planning(9)
    payloads = [f"candidate-{index}".encode() for index in range(1, 6)]
    candidates = [_candidate(index, data) for index, data in enumerate(payloads, start=1)]
    records = {
        candidate.image_id: {
            "id": candidate.image_id,
            "content_type": "image/jpeg",
            "byte_size": len(data),
            "backend": "db",
            "storage_ref": None,
            "data": data,
        }
        for candidate, data in zip(candidates, payloads, strict=True)
    }
    service = ImageRagService(
        _settings(), FakeRepo(records), None, vector_store=object(), embeddings=object()
    )

    async def retrieve(*_args, **_kwargs):
        return RagRetrieval(
            target_views=6,
            current_views=0,
            missing_views=6,
            eligible_scene_ids=[candidate.scene_id for candidate in candidates],
            candidates=candidates,
        )

    service.retrieve = retrieve  # type: ignore[method-assign]
    vision = ScriptedVision(
        [
            {"approved": [_approval(candidate) for candidate in candidates[:4]]},
            {"approved": [_approval(candidates[4])]},
        ]
    )

    result = await service.augment(
        "current",
        source={"title": "Current article"},
        ledger=ledger,
        planning=planning,
        images=[],
        cover_ref=None,
        vision=vision,
    )

    assert result.approved_count == 5
    assert [len(call[2]) for call in vision.calls] == [4, 1]
    assert all(call[0] == RAG_VERIFICATION_SYSTEM_PROMPT for call in vision.calls)
    assert all(call[3] == "article_map_rag_verdict_v1" for call in vision.calls)
    assert [image["ref"] for image in result.images] == [0, 1, 2, 3, 4]
    assert all(image["role"] == "rag" for image in result.images)
    assert all("origin_article_id" in image for image in result.images)
    assert visible_image_views(result.planning, None) == 5
    assert select_cover_image_ref({"images": []}, result.ledger) is None
    assert validate_planning(result.planning, result.ledger, _settings()) == []


@pytest.mark.asyncio
async def test_admission_respects_cerebras_two_image_provider_cap():
    ledger, planning = _ledger_and_planning(5)
    payloads = [f"candidate-{index}".encode() for index in range(1, 4)]
    candidates = [
        _candidate(index, data)
        for index, data in enumerate(payloads, start=1)
    ]
    records = {
        candidate.image_id: {
            "id": candidate.image_id,
            "content_type": "image/jpeg",
            "byte_size": len(data),
            "backend": "db",
            "storage_ref": None,
            "data": data,
        }
        for candidate, data in zip(candidates, payloads, strict=True)
    }
    service = ImageRagService(
        _settings(),
        FakeRepo(records),
        None,
        vector_store=object(),
        embeddings=object(),
    )

    async def retrieve(*_args, **_kwargs):
        return RagRetrieval(
            target_views=4,
            current_views=0,
            missing_views=3,
            eligible_scene_ids=[candidate.scene_id for candidate in candidates],
            candidates=candidates,
        )

    service.retrieve = retrieve  # type: ignore[method-assign]
    vision = ScriptedVision(
        [
            {"approved": [_approval(candidate) for candidate in candidates[:2]]},
            {"approved": [_approval(candidates[2])]},
        ]
    )
    vision.max_images = 2

    result = await service.augment(
        "current",
        source={"title": "Current article"},
        ledger=ledger,
        planning=planning,
        images=[],
        cover_ref=None,
        vision=vision,
    )

    assert result.approved_count == 3
    assert [len(call[2]) for call in vision.calls] == [2, 1]


@pytest.mark.asyncio
async def test_invalid_vision_verdict_discards_entire_fallback_without_mutation():
    ledger, planning = _ledger_and_planning(3)
    candidate = _candidate(1, b"candidate")
    record = {
        "id": candidate.image_id,
        "content_type": "image/jpeg",
        "byte_size": len(b"candidate"),
        "backend": "db",
        "storage_ref": None,
        "data": b"candidate",
    }
    service = ImageRagService(
        _settings(), FakeRepo({candidate.image_id: record}), None,
        vector_store=object(), embeddings=object(),
    )

    async def retrieve(*_args, **_kwargs):
        return RagRetrieval(2, 0, 2, ["S-01"], [candidate])

    service.retrieve = retrieve  # type: ignore[method-assign]
    before_ledger, before_planning = deepcopy(ledger), deepcopy(planning)
    vision = ScriptedVision(
        [{"approved": [{**_approval(candidate), "candidate_id": "unknown"}]}]
    )

    result = await service.augment(
        "current",
        source={"title": "Current article"},
        ledger=ledger,
        planning=planning,
        images=[],
        cover_ref=None,
        vision=vision,
    )

    assert result.approved_count == 0 and result.images == []
    assert result.ledger == before_ledger and result.planning == before_planning
    assert ledger == before_ledger and planning == before_planning
