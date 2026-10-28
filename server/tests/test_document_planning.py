from __future__ import annotations

from app.config import Settings
from app.editorial_pipeline import document_context_error, validate_planning
from app.editorial_prompts import (
    DOCUMENT_PLANNING_SEQUENCES,
    build_planning_prompt,
)


def _ledger() -> dict:
    return {
        "language": "ko",
        "claims": [],
        "media": [],
        "visualizations": [],
    }


def test_proposal_prompt_contains_sequence() -> None:
    prompt = build_planning_prompt(
        _ledger(),
        min_scenes=3,
        max_scenes=9,
        document_type="governance_proposal",
    )

    positions = [
        prompt.index(f'"{role}"')
        for role in DOCUMENT_PLANNING_SEQUENCES["governance_proposal"]
    ]
    assert positions == sorted(positions)
    assert '"lead_role":"proposal"' in prompt


def test_article_prompt_unchanged() -> None:
    ledger = _ledger()
    expected = (
        'Propose the editorial direction and scene intentions.\n'
        '{"claims":[],"language":"ko","media":[],"scene_count":{"max":9,"min":3},'
        '"visualizations":[]}'
    )

    assert build_planning_prompt(ledger, min_scenes=3, max_scenes=9) == expected
    assert (
        build_planning_prompt(
            ledger,
            min_scenes=3,
            max_scenes=9,
            document_type="unsupported",
        )
        == expected
    )


def test_notice_without_context_dead_letters() -> None:
    error = document_context_error(
        document_type="foundation_notice",
        publisher_context="",
    )

    assert error is not None
    assert error.stage == "planner"
    assert error.error_code == "document_context_required"
    assert error.retryable is False


def test_partial_sequence_allowed() -> None:
    ledger = {
        "language": "ko",
        "claims": [
            {
                "id": f"CL-{index:03d}",
                "kind": "fact",
                "importance": "essential",
                "text": f"claim {index}",
                "evidence": [{"sentence_refs": [f"P-001:S-{index:03d}"]}],
            }
            for index in range(1, 5)
        ],
        "media": [],
        "visualizations": [],
    }
    planning = {
        "brief": {
            "language": "ko",
            "headline": "제목",
            "subheadline": "부제",
            "summary": "요약.",
            "keywords": ["키워드", "문서", "계획"],
            "central_question": "질문",
            "answer": "답",
            "why_it_matters": "이유",
            "ordering_policy": "editorial_completeness",
            "required_claim_refs": ["CL-001", "CL-002", "CL-003", "CL-004"],
            "included_supporting_claim_refs": [],
            "omitted_supporting_claim_refs": [],
            "summary_claim_refs": ["CL-001"],
        },
        "plan": {
            "scenes": [
                {
                    "id": f"S-{index:02d}",
                    "role": role,
                    "label": role,
                    "purpose": role,
                    "title_hint": role,
                    "claim_refs": (
                        ["CL-001", "CL-002"]
                        if index == 1
                        else [f"CL-{index + 1:03d}"]
                    ),
                    "image_ref": None,
                    "chart_ref": None,
                    "target_chars": 120,
                    "transition": "next" if role != "close" else "",
                }
                for index, role in enumerate(("lead", "evidence", "close"), 1)
            ]
        },
    }

    assert validate_planning(planning, ledger, Settings()) == []


def test_types_have_distinct_sequences() -> None:
    sequences = list(DOCUMENT_PLANNING_SEQUENCES.values())
    assert len(sequences) == 5
    assert len(set(sequences)) == len(sequences)
