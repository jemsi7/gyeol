"""Native structured-output schemas for every LLM-authored Map stage."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.map_output_schemas import (
    critic_output_schema,
    evidence_claim_text_output_schema,
    evidence_output_schema,
    evidence_selection_output_schema,
    planner_output_schema,
    planning_output_schema,
    rag_verdict_output_schema,
    scene_batch_output_schema,
    validate_supported_schema,
)
from app.openrouter import StructuredOutputSpec


def _source(*, with_image: bool = True) -> dict:
    return {
        "paragraphs": [
            {
                "id": "P-001",
                "sentences": [{"id": "P-001:S-001"}],
            },
            {
                "id": "P-002",
                "sentences": [{"id": "P-002:S-001"}],
            },
        ],
        "images": (
            [{"ref": 0, "visible_to_model": True}]
            if with_image
            else []
        ),
    }


def _ledger() -> dict:
    return {
        "language": "ko",
        "claims": [{"id": "CL-001"}, {"id": "CL-002"}],
        "media": [{"ref": 0}],
        "visualizations": [{"ref": 0}],
    }


def _evidence_selection() -> dict:
    return {
        "language": "ko",
        "claims": [{"id": "CL-001"}, {"id": "CL-002"}],
    }


def _plan_slice() -> list[dict]:
    return [
        {
            "id": "S-01",
            "role": "lead",
            "claim_refs": ["CL-001"],
            "image_ref": 0,
            "chart_ref": None,
        },
        {
            "id": "S-02",
            "role": "close",
            "claim_refs": ["CL-002"],
            "image_ref": None,
            "chart_ref": 0,
        },
    ]


def _walk(node):
    yield node
    if isinstance(node, dict):
        for value in node.values():
            if isinstance(value, (dict, list)):
                yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            if isinstance(value, (dict, list)):
                yield from _walk(value)


def _assert_provider_safe(spec: StructuredOutputSpec) -> None:
    validate_supported_schema(spec)
    for node in _walk(spec.schema):
        if not isinstance(node, dict):
            continue
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])
        if "enum" in node:
            assert node["enum"]
        if "anyOf" in node:
            assert node["anyOf"]


def test_every_stage_factory_emits_a_provider_safe_named_schema():
    candidates = [
        SimpleNamespace(
            image_id="image-a",
            scene_id="S-01",
            claim_refs=("CL-001",),
        )
    ]
    specs = [
        evidence_selection_output_schema(_source()),
        evidence_claim_text_output_schema(_evidence_selection()),
        planner_output_schema(_ledger(), min_scenes=2, max_scenes=7),
        scene_batch_output_schema(_plan_slice()),
        scene_batch_output_schema(_plan_slice()[:1], repair=True),
        critic_output_schema(
            scene_ids={"S-01", "S-02"},
            claim_ids={"CL-001", "CL-002"},
        ),
        rag_verdict_output_schema(candidates, missing_views=2),
    ]

    assert [spec.name for spec in specs] == [
        "article_map_evidence_refs_v1",
        "article_map_evidence_claims_v1",
        "article_map_planner_v2",
        "article_map_scene_copy_v3",
        "article_map_repair_v2",
        "article_map_critic_v1",
        "article_map_rag_verdict_v1",
    ]
    for spec in specs:
        _assert_provider_safe(spec)


def test_stage_schemas_bind_dynamic_source_plan_and_candidate_contracts():
    evidence = evidence_selection_output_schema(_source())
    evidence_claim = evidence.schema["properties"]["claims"]["items"]
    assert "text" not in evidence_claim["properties"]
    sentence_refs = evidence_claim["properties"]["evidence"]["items"]["properties"][
        "sentence_refs"
    ]
    # A constant-size pattern instead of the full sentence-ID enum (DR-13261):
    # the schema no longer grows with article length, and refs that do not
    # exist are rejected by the server-side validators instead.
    assert sentence_refs["items"]["pattern"] == r"^P-\d{3,}:S-\d{3,}$"
    assert "enum" not in sentence_refs["items"]
    chart_point = evidence.schema["properties"]["visualizations"]["items"][
        "properties"
    ]["series"]["items"]["properties"]["points"]["items"]
    assert chart_point["properties"]["sentence_ref"]["pattern"] == (
        r"^P-\d{3,}:S-\d{3,}$"
    )
    assert "enum" not in chart_point["properties"]["sentence_ref"]
    assert "evidence" not in chart_point["properties"]
    assert set(chart_point["properties"]) >= {
        "region_code", "row", "column", "sentence_ref"
    }
    visualization = evidence.schema["properties"]["visualizations"]["items"]
    assert set(visualization["properties"]["kind"]["enum"]) == {
        "bar", "line", "area", "pie", "mixed",
        "world_map", "region_heatmap", "matrix_heatmap",
    }
    assert set(visualization["properties"]) >= {"geo_scope", "color_scale"}
    assert evidence.schema["properties"]["media"]["items"]["properties"]["ref"][
        "enum"
    ] == [0]

    claim_writer = evidence_claim_text_output_schema(_evidence_selection())
    writer_claim = claim_writer.schema["properties"]["claims"]["items"]
    assert claim_writer.schema["properties"]["language"]["const"] == "ko"
    assert writer_claim["properties"]["id"]["enum"] == ["CL-001", "CL-002"]
    assert set(writer_claim["properties"]) == {"id", "text"}

    planning = planner_output_schema(_ledger(), min_scenes=2, max_scenes=7)
    planned_scenes = planning.schema["properties"]["scene_proposals"]
    planned_scene = planned_scenes["items"]
    assert (planned_scenes["minItems"], planned_scenes["maxItems"]) == (2, 7)
    assert planned_scene["properties"]["claim_refs"]["items"]["enum"] == [
        "CL-001",
        "CL-002",
    ]
    assert not {"id", "role", "image_ref", "chart_ref"} & set(
        planned_scene["properties"]
    )

    scenes = scene_batch_output_schema(_plan_slice())
    alternatives = scenes.schema["properties"]["scenes"]["items"]["anyOf"]
    assert [item["properties"]["scene_id"]["const"] for item in alternatives] == [
        "S-01",
        "S-02",
    ]
    for alternative in alternatives:
        assert not {
            "id", "role", "label", "claim_refs", "image_ref", "chart_ref"
        } & set(alternative["properties"])
    assert alternatives[1]["properties"]["summary"]["const"] == ""
    assert alternatives[1]["properties"]["body"]["const"] == ""

    candidate = SimpleNamespace(
        image_id="image-a",
        scene_id="S-01",
        claim_refs=("CL-001",),
    )
    rag = rag_verdict_output_schema([candidate], missing_views=2)
    approved = rag.schema["properties"]["approved"]
    approval = approved["items"]
    assert approved["maxItems"] == 2
    assert approval["properties"]["candidate_id"]["enum"] == ["image-a"]
    assert approval["properties"]["scene_id"]["enum"] == ["S-01"]


def test_scene_schema_leaves_non_chart_body_without_360_max_length():
    scenes = scene_batch_output_schema(_plan_slice())
    alternatives = scenes.schema["properties"]["scenes"]["items"]["anyOf"]

    non_chart_body = alternatives[0]["properties"]["body"]
    chart_body = alternatives[1]["properties"]["body"]

    assert non_chart_body["minLength"] == 1
    assert "maxLength" not in non_chart_body
    assert chart_body["const"] == ""


def test_evidence_without_visible_images_uses_zero_length_media_not_empty_enum():
    spec = evidence_output_schema(_source(with_image=False))

    media = spec.schema["properties"]["media"]
    assert media["maxItems"] == 0
    assert "enum" not in media["items"]["properties"]["ref"]
    _assert_provider_safe(spec)


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        (
            StructuredOutputSpec(
                name="bad_keyword",
                schema={
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                    "if": {"type": "object"},
                },
            ),
            "unsupported keywords",
        ),
        (
            StructuredOutputSpec(
                name="not_strict",
                schema={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": [],
                    "additionalProperties": False,
                },
            ),
            "object contract is not strict",
        ),
        (
            StructuredOutputSpec(
                name="empty_enum",
                schema={
                    "type": "object",
                    "properties": {"value": {"type": "string", "enum": []}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            ),
            "enum must not be empty",
        ),
    ],
)
def test_supported_schema_validator_fails_closed(spec, message):
    with pytest.raises(ValueError, match=message):
        validate_supported_schema(spec)


def test_schema_factories_reject_ambiguous_empty_dynamic_domains():
    with pytest.raises(ValueError, match="sentence IDs"):
        evidence_output_schema({"paragraphs": [], "images": []})
    with pytest.raises(ValueError, match="selection language"):
        evidence_claim_text_output_schema({"language": "", "claims": [{"id": "CL-001"}]})
    with pytest.raises(ValueError, match="selected claim IDs"):
        evidence_claim_text_output_schema({"language": "ko", "claims": []})
    with pytest.raises(ValueError, match="claim IDs"):
        planning_output_schema(
            {"language": "ko", "claims": [], "media": [], "visualizations": []},
            min_scenes=3,
            max_scenes=7,
        )
    with pytest.raises(ValueError, match="exactly one"):
        scene_batch_output_schema(_plan_slice(), repair=True)
    with pytest.raises(ValueError, match="critic scene IDs"):
        critic_output_schema(scene_ids=set(), claim_ids={"CL-001"})
    with pytest.raises(ValueError, match="candidates"):
        rag_verdict_output_schema([], missing_views=1)
