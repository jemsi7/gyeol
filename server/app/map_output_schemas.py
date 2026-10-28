"""Strict JSON Schemas for the LLM-authored stages of Delivery Map v3.

These schemas intentionally cover structural constraints only. Existing
deterministic validators remain authoritative for grounding, cross-stage
references, numeric preservation, claim coverage, and editorial quality.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .openrouter import StructuredOutputSpec

_SCHEMA_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SUPPORTED_TYPES = {
    "string",
    "number",
    "integer",
    "boolean",
    "null",
    "array",
    "object",
}
_SUPPORTED_KEYWORDS = {
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "anyOf",
    "enum",
    "const",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "minimum",
    "maximum",
    "pattern",
    "description",
}
# One source sentence address (DR-13261). A pattern instead of an enum of
# every sentence ID keeps the schema a constant size regardless of article
# length; the server already rejects refs that do not exist.
_SENTENCE_REF_PATTERN = r"^P-\d{3,}:S-\d{3,}$"
_CLAIM_KINDS = ["fact", "context", "quote", "number", "caveat"]
_IMPORTANCE = ["essential", "supporting"]
_CHART_KINDS = [
    "bar", "line", "area", "pie", "mixed",
    "world_map", "region_heatmap", "matrix_heatmap",
]
_CHART_MARKS = ["bar", "line", "area", "sector", "region", "cell"]
_GEO_SCOPES = [
    "", "world", "africa", "asia", "europe",
    "north_america", "south_america", "oceania",
]
_COLOR_SCHEMES = ["ocean", "forest", "ember", "violet", "diverging"]
_COLOR_MODES = ["continuous", "quantized"]
_SCENE_ROLES = ["lead", "context", "evidence", "impact", "close"]
_CRITIC_CODES = [
    "grounding",
    "distortion",
    "omission",
    "duplication",
    "flow",
    "tone",
    "media",
]


def _unique(values: list[Any]) -> list[Any]:
    result: list[Any] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _string(
    *,
    min_length: int | None = None,
    max_length: int | None = None,
    enum: list[str] | None = None,
    const: str | None = None,
    pattern: str | None = None,
) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "string"}
    if min_length is not None:
        schema["minLength"] = min_length
    if max_length is not None:
        schema["maxLength"] = max_length
    if enum is not None:
        schema["enum"] = _unique(enum)
    if const is not None:
        schema["const"] = const
    if pattern is not None:
        schema["pattern"] = pattern
    return schema


def _integer(
    *,
    minimum: int | None = None,
    maximum: int | None = None,
    enum: list[int] | None = None,
    const: int | None = None,
) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "integer"}
    if minimum is not None:
        schema["minimum"] = minimum
    if maximum is not None:
        schema["maximum"] = maximum
    if enum is not None:
        schema["enum"] = _unique(enum)
    if const is not None:
        schema["const"] = const
    return schema


def _array(
    items: dict[str, Any],
    *,
    min_items: int = 0,
    max_items: int = 256,
) -> dict[str, Any]:
    return {
        "type": "array",
        "items": items,
        "minItems": min_items,
        "maxItems": max_items,
    }


def _object(properties: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    copied = dict(properties)
    return {
        "type": "object",
        "properties": copied,
        "required": list(copied),
        "additionalProperties": False,
    }


def _nullable_integer(values: list[int]) -> dict[str, Any]:
    allowed = _unique(
        [value for value in values if isinstance(value, int) and not isinstance(value, bool)]
    )
    if not allowed:
        return {"type": "null"}
    return {
        "anyOf": [
            _integer(enum=allowed),
            {"type": "null"},
        ]
    }


def _const_nullable_integer(value: Any) -> dict[str, Any]:
    if value is None:
        return {"type": "null"}
    if isinstance(value, int) and not isinstance(value, bool):
        return _integer(const=value)
    raise ValueError("planned references must be integers or null")


def _nonempty_string_enum(values: list[str], *, label: str) -> dict[str, Any]:
    allowed = _unique([value.strip() for value in values if isinstance(value, str) and value.strip()])
    if not allowed:
        raise ValueError(f"{label} must not be empty")
    return _string(enum=allowed)


def _visualization_schema() -> dict[str, Any]:
    point = _object(
        {
            "label": _string(min_length=1),
            "value": {"type": "number"},
            "raw_value": _string(min_length=1),
            "region_code": _string(max_length=2),
            "row": _string(max_length=120),
            "column": _string(max_length=120),
            "sentence_ref": _string(pattern=_SENTENCE_REF_PATTERN),
        }
    )
    series = _object(
        {
            "name": _string(min_length=1),
            "mark": _string(enum=_CHART_MARKS),
            "points": _array(point, min_items=1, max_items=200),
        }
    )
    color_scale = _object(
        {
            "scheme": _string(enum=_COLOR_SCHEMES),
            "mode": _string(enum=_COLOR_MODES),
        }
    )
    return _object(
        {
            "ref": _integer(minimum=0, maximum=1),
            "kind": _string(enum=_CHART_KINDS),
            "title": _string(min_length=1),
            "unit": _string(min_length=1),
            "alt_text": _string(min_length=1),
            "geo_scope": _string(enum=_GEO_SCOPES),
            "color_scale": {
                "anyOf": [color_scale, {"type": "null"}],
            },
            "series": _array(series, min_items=1, max_items=4),
        }
    )


def _evidence_source_constraints(source: dict) -> tuple[list[str], list[int]]:
    sentence_ids = _unique(
        [
            str(sentence.get("id") or "").strip()
            for item in source.get("paragraphs") or []
            if isinstance(item, dict) and str(item.get("id") or "").strip()
            for sentence in item.get("sentences") or []
            if isinstance(sentence, dict)
            and str(sentence.get("id") or "").strip()
        ]
    )
    if not sentence_ids:
        raise ValueError("evidence schema requires source sentence IDs")
    visible_image_refs = _unique(
        [
            item.get("ref")
            for item in source.get("images") or []
            if isinstance(item, dict)
            and item.get("visible_to_model")
            and isinstance(item.get("ref"), int)
            and not isinstance(item.get("ref"), bool)
        ]
    )
    return sentence_ids, visible_image_refs


def evidence_selection_output_schema(source: dict) -> StructuredOutputSpec:
    """Schema for source-grounded claim/ref selection without reader prose."""
    _, visible_image_refs = _evidence_source_constraints(source)
    evidence = _object(
        {
            "sentence_refs": _array(
                _string(pattern=_SENTENCE_REF_PATTERN),
                min_items=1,
                max_items=3,
            ),
        }
    )
    claim = _object(
        {
            "id": _string(pattern=r"CL-\d{3}"),
            "kind": _string(enum=_CLAIM_KINDS),
            "importance": _string(enum=_IMPORTANCE),
            "evidence": _array(evidence, min_items=1, max_items=3),
        }
    )
    media_ref = (
        _integer(enum=visible_image_refs)
        if visible_image_refs
        else _integer(minimum=0)
    )
    media = _object(
        {
            "ref": media_ref,
            "caption": _string(min_length=1),
            "reason": _string(min_length=1),
            "claim_refs": _array(
                _string(pattern=r"CL-\d{3}"),
                min_items=1,
                max_items=30,
            ),
        }
    )
    schema = _object(
        {
            "language": _string(min_length=1),
            "claims": _array(claim, min_items=1, max_items=30),
            "media": _array(
                media,
                min_items=0,
                max_items=3 if visible_image_refs else 0,
            ),
            "visualizations": _array(
                _visualization_schema(),
                min_items=0,
                max_items=2,
            ),
        }
    )
    return _validated_spec("article_map_evidence_refs_v1", schema)


def evidence_claim_text_output_schema(selection: dict) -> StructuredOutputSpec:
    """Bind the claim writer to selected IDs and the selected language."""
    language = str(selection.get("language") or "").strip()
    claim_ids = _unique(
        [
            str(item.get("id") or "").strip()
            for item in selection.get("claims") or []
            if isinstance(item, dict) and str(item.get("id") or "").strip()
        ]
    )
    if not language:
        raise ValueError("evidence claim schema requires selection language")
    if not claim_ids:
        raise ValueError("evidence claim schema requires selected claim IDs")
    claim = _object(
        {
            "id": _nonempty_string_enum(claim_ids, label="selected claim IDs"),
            "text": _string(min_length=1, max_length=500),
        }
    )
    schema = _object(
        {
            "language": _string(const=language),
            "claims": _array(
                claim,
                min_items=len(claim_ids),
                max_items=len(claim_ids),
            ),
        }
    )
    return _validated_spec("article_map_evidence_claims_v1", schema)


def evidence_output_schema(source: dict) -> StructuredOutputSpec:
    """Backward-compatible alias for the first evidence/ref-selection stage."""
    return evidence_selection_output_schema(source)


def planning_output_schema(
    ledger: dict,
    *,
    min_scenes: int,
    max_scenes: int,
) -> StructuredOutputSpec:
    if not 1 <= min_scenes <= max_scenes <= 9:
        raise ValueError("scene bounds must satisfy 1 <= min <= max <= 9")
    language = str(ledger.get("language") or "").strip()
    if not language:
        raise ValueError("planning schema requires ledger language")
    claim_ids = [
        str(item.get("id") or "").strip()
        for item in ledger.get("claims") or []
        if isinstance(item, dict)
    ]
    claim_ref = _nonempty_string_enum(claim_ids, label="ledger claim IDs")
    media_refs = [
        item.get("ref")
        for item in ledger.get("media") or []
        if isinstance(item, dict)
    ]
    chart_refs = [
        item.get("ref")
        for item in ledger.get("visualizations") or []
        if isinstance(item, dict)
    ]
    brief = _object(
        {
            "language": _string(const=language),
            "headline": _string(min_length=1, max_length=120),
            "subheadline": _string(),
            "summary": _string(min_length=1, max_length=600),
            "keywords": _array(_string(min_length=1), min_items=3, max_items=8),
            "central_question": _string(min_length=1),
            "answer": _string(min_length=1),
            "why_it_matters": _string(min_length=1),
            "ordering_policy": _string(const="editorial_completeness"),
            "required_claim_refs": _array(claim_ref, min_items=1, max_items=30),
            "included_supporting_claim_refs": _array(claim_ref, max_items=30),
            "omitted_supporting_claim_refs": _array(claim_ref, max_items=30),
            "summary_claim_refs": _array(claim_ref, min_items=1, max_items=3),
        }
    )
    planned_scene = _object(
        {
            "id": _string(pattern=r"S-\d{2}"),
            "role": _string(enum=_SCENE_ROLES),
            "label": _string(min_length=1),
            "purpose": _string(min_length=1),
            "title_hint": _string(min_length=1),
            "claim_refs": _array(claim_ref, min_items=1, max_items=30),
            "image_ref": _nullable_integer(media_refs),
            "chart_ref": _nullable_integer(chart_refs),
            "target_chars": _integer(minimum=0, maximum=420),
            "transition": _string(),
        }
    )
    schema = _object(
        {
            "brief": brief,
            "scenes": _array(
                planned_scene,
                min_items=min_scenes,
                max_items=max_scenes,
            ),
        }
    )
    return _validated_spec("article_map_planning_v1", schema)


def planner_output_schema(
    ledger: dict,
    *,
    min_scenes: int,
    max_scenes: int,
) -> StructuredOutputSpec:
    """Schema for editorial proposals, deliberately excluding Map topology."""
    if not 1 <= min_scenes <= max_scenes <= 9:
        raise ValueError("scene bounds must satisfy 1 <= min <= max <= 9")
    language = str(ledger.get("language") or "").strip()
    if not language:
        raise ValueError("planner schema requires ledger language")
    claim_ids = [
        str(item.get("id") or "").strip()
        for item in ledger.get("claims") or []
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    ]
    supporting_ids = [
        str(item.get("id") or "").strip()
        for item in ledger.get("claims") or []
        if isinstance(item, dict)
        and item.get("importance") == "supporting"
        and str(item.get("id") or "").strip()
    ]
    claim_ref = _nonempty_string_enum(claim_ids, label="ledger claim IDs")
    supporting_ref = (
        _nonempty_string_enum(supporting_ids, label="supporting claim IDs")
        if supporting_ids
        else claim_ref
    )
    proposal = _object(
        {
            "label": _string(min_length=1, max_length=24),
            "purpose": _string(min_length=1),
            "title_hint": _string(min_length=1, max_length=90),
            "claim_refs": _array(claim_ref, min_items=1, max_items=30),
            "target_chars": _integer(minimum=0, maximum=420),
            "transition": _string(),
        }
    )
    schema = _object(
        {
            "language": _string(const=language),
            "headline": _string(min_length=1, max_length=120),
            "subheadline": _string(),
            "summary": _string(min_length=1, max_length=600),
            "keywords": _array(_string(min_length=1), min_items=3, max_items=8),
            "central_question": _string(min_length=1),
            "answer": _string(min_length=1),
            "why_it_matters": _string(min_length=1),
            "selected_supporting_claim_refs": _array(
                supporting_ref,
                max_items=30,
            ),
            "summary_claim_refs": _array(
                claim_ref,
                min_items=1,
                max_items=3,
            ),
            "scene_proposals": _array(
                proposal,
                min_items=min_scenes,
                max_items=max_scenes,
            ),
        }
    )
    return _validated_spec("article_map_planner_v2", schema)


def _quote_evidence_refs(planned: dict, ledger: dict) -> list[str]:
    """Every evidence address a scene's quote may cite, plus the empty choice.

    Addresses are "<claim id>/<1-based evidence position>" for the claims
    assigned to this scene, so the writer can only name a span the server can
    resolve; the exact source string never passes through the model (DR-13260).
    """
    claim_by_id = {
        claim.get("id"): claim
        for claim in ledger.get("claims") or []
        if isinstance(claim, dict)
    }
    refs = [""]
    for claim_ref in _unique(
        [
            str(value).strip()
            for value in planned.get("claim_refs") or []
            if isinstance(value, str) and str(value).strip()
        ]
    ):
        claim = claim_by_id.get(claim_ref)
        if not isinstance(claim, dict):
            continue
        for position in range(len(claim.get("evidence") or [])):
            refs.append(f"{claim_ref}/{position + 1}")
    return refs


def _quote_evidence_ref_property(
    planned: dict,
    ledger: dict | None,
    is_chart: bool,
) -> dict[str, Any]:
    """The claim-evidence address a translated quote renders (DR-13260)."""
    if is_chart:
        return _string(const="")
    if ledger is None:
        raise ValueError("translated scene schema requires the evidence ledger")
    return _string(enum=_quote_evidence_refs(planned, ledger))


def _written_scene_schema(
    planned: dict,
    *,
    translated: bool = False,
    ledger: dict | None = None,
) -> dict[str, Any]:
    scene_id = str(planned.get("id") or "").strip()
    role = str(planned.get("role") or "").strip()
    claim_refs = _unique(
        [
            str(value).strip()
            for value in planned.get("claim_refs") or []
            if isinstance(value, str) and str(value).strip()
        ]
    )
    if not scene_id or role not in _SCENE_ROLES or not claim_refs:
        raise ValueError("planned scene identity, role and claim refs are required")
    is_chart = planned.get("chart_ref") is not None
    return _object(
        {
            "id": _string(const=scene_id),
            "role": _string(const=role),
            "label": _string(min_length=1, max_length=24),
            "title": _string(min_length=1, max_length=90),
            "summary": (
                _string(min_length=1, max_length=320)
                if role == "lead"
                else _string(const="")
            ),
            "body": (
                _string(const="")
                if is_chart
                else _string(min_length=1)
            ),
            "secondary": (
                _string(const="")
                if is_chart
                else _string(max_length=300)
            ),
            "quote": (
                _string(const="")
                if is_chart
                else _string(max_length=240)
            ),
            **(
                {
                    "quote_evidence_ref": _quote_evidence_ref_property(
                        planned, ledger, is_chart
                    )
                }
                if translated
                else {}
            ),
            "image_ref": _const_nullable_integer(planned.get("image_ref")),
            "chart_ref": _const_nullable_integer(planned.get("chart_ref")),
            "claim_refs": _array(
                _string(enum=claim_refs),
                min_items=len(claim_refs),
                max_items=len(claim_refs),
            ),
        }
    )


def _scene_copy_schema(
    planned: dict,
    *,
    translated: bool = False,
    ledger: dict | None = None,
) -> dict[str, Any]:
    scene_id = str(planned.get("id") or "").strip()
    role = str(planned.get("role") or "").strip()
    if not scene_id or role not in _SCENE_ROLES:
        raise ValueError("planned scene identity and role are required")
    is_chart = planned.get("chart_ref") is not None
    return _object(
        {
            "scene_id": _string(const=scene_id),
            "title": _string(min_length=1, max_length=90),
            "summary": (
                _string(min_length=1, max_length=320)
                if role == "lead"
                else _string(const="")
            ),
            "body": _string(const="") if is_chart else _string(min_length=1),
            "secondary": (
                _string(const="") if is_chart else _string(max_length=300)
            ),
            "quote": _string(const="") if is_chart else _string(max_length=240),
            **(
                {
                    "quote_evidence_ref": _quote_evidence_ref_property(
                        planned, ledger, is_chart
                    )
                }
                if translated
                else {}
            ),
        }
    )


def scene_batch_output_schema(
    plan_slice: list[dict],
    *,
    repair: bool = False,
    translated: bool = False,
    ledger: dict | None = None,
) -> StructuredOutputSpec:
    if not plan_slice:
        raise ValueError("scene schema requires a non-empty plan slice")
    if repair and len(plan_slice) != 1:
        raise ValueError("repair schema requires exactly one planned scene")
    if translated and ledger is None:
        raise ValueError("translated scene schema requires the evidence ledger")
    alternatives = [
        (
            _written_scene_schema(item, translated=translated, ledger=ledger)
            if repair
            else _scene_copy_schema(item, translated=translated, ledger=ledger)
        )
        for item in plan_slice
    ]
    item_schema = (
        alternatives[0]
        if len(alternatives) == 1
        else {"anyOf": alternatives}
    )
    schema = _object(
        {
            "scenes": _array(
                item_schema,
                min_items=len(plan_slice),
                max_items=len(plan_slice),
            )
        }
    )
    name = "article_map_repair_v2" if repair else "article_map_scene_copy_v3"
    return _validated_spec(name, schema)


def critic_output_schema(
    *,
    scene_ids: set[str],
    claim_ids: set[str],
) -> StructuredOutputSpec:
    scene_ref = _nonempty_string_enum(sorted(scene_ids), label="critic scene IDs")
    claim_ref = _nonempty_string_enum(sorted(claim_ids), label="critic claim IDs")
    issue = _object(
        {
            "scene_id": scene_ref,
            "code": _string(enum=_CRITIC_CODES),
            "message": _string(min_length=1),
            "claim_refs": _array(claim_ref, max_items=30),
            "repair_instruction": _string(min_length=1),
        }
    )
    schema = _object(
        {
            "verdict": _string(enum=["pass", "revise"]),
            "issues": _array(issue, max_items=30),
        }
    )
    return _validated_spec("article_map_critic_v1", schema)


def rag_verdict_output_schema(
    candidates: list[Any],
    *,
    missing_views: int,
) -> StructuredOutputSpec:
    if not candidates or not 1 <= missing_views <= 18:
        raise ValueError("RAG schema requires candidates and 1..18 missing views")
    candidate_ids = [
        str(getattr(candidate, "image_id", "") or "").strip()
        for candidate in candidates
    ]
    scene_ids = [
        str(getattr(candidate, "scene_id", "") or "").strip()
        for candidate in candidates
    ]
    claim_ids = [
        str(ref).strip()
        for candidate in candidates
        for ref in (getattr(candidate, "claim_refs", ()) or ())
        if isinstance(ref, str)
    ]
    approval = _object(
        {
            "candidate_id": _nonempty_string_enum(
                candidate_ids,
                label="RAG candidate IDs",
            ),
            "scene_id": _nonempty_string_enum(scene_ids, label="RAG scene IDs"),
            "claim_refs": _array(
                _nonempty_string_enum(claim_ids, label="RAG claim IDs"),
                min_items=1,
                max_items=30,
            ),
            "caption": _string(min_length=1, max_length=300),
            "reason": _string(min_length=1, max_length=500),
        }
    )
    schema = _object(
        {
            "approved": _array(
                approval,
                max_items=min(18, missing_views),
            )
        }
    )
    return _validated_spec("article_map_rag_verdict_v1", schema)


def _validated_spec(name: str, schema: dict[str, Any]) -> StructuredOutputSpec:
    spec = StructuredOutputSpec(name=name, schema=schema)
    validate_supported_schema(spec)
    return spec


def validate_supported_schema(spec: StructuredOutputSpec) -> None:
    """Fail locally for schema shapes xAI rejects or only supports best-effort."""
    if not _SCHEMA_NAME_RE.fullmatch(spec.name):
        raise ValueError("invalid structured output schema name")
    if not isinstance(spec.schema, Mapping) or spec.schema.get("type") != "object":
        raise ValueError("structured output schema root must be an object")

    def walk(node: Any, path: str) -> None:
        if not isinstance(node, Mapping) or not node:
            raise ValueError(f"{path} must be a non-empty schema object")
        unknown = set(node) - _SUPPORTED_KEYWORDS
        if unknown:
            raise ValueError(f"{path} uses unsupported keywords: {sorted(unknown)!r}")
        schema_type = node.get("type")
        if schema_type is not None and schema_type not in _SUPPORTED_TYPES:
            raise ValueError(f"{path}.type is unsupported")
        if "enum" in node:
            enum = node["enum"]
            if not isinstance(enum, list) or not enum:
                raise ValueError(f"{path}.enum must not be empty")
        if "anyOf" in node:
            variants = node["anyOf"]
            if not isinstance(variants, list) or not variants:
                raise ValueError(f"{path}.anyOf must not be empty")
            for index, variant in enumerate(variants):
                walk(variant, f"{path}.anyOf[{index}]")
        if schema_type == "object":
            properties = node.get("properties")
            required = node.get("required")
            if (
                not isinstance(properties, Mapping)
                or len(properties) > 64
                or node.get("additionalProperties") is not False
                or not isinstance(required, list)
                or set(required) != set(properties)
                or len(required) != len(properties)
            ):
                raise ValueError(f"{path} object contract is not strict")
            for key, value in properties.items():
                walk(value, f"{path}.properties.{key}")
        if schema_type == "array":
            if "items" not in node:
                raise ValueError(f"{path}.items is required")
            min_items = node.get("minItems", 0)
            max_items = node.get("maxItems", 256)
            if (
                not isinstance(min_items, int)
                or not isinstance(max_items, int)
                or not 0 <= min_items <= max_items <= 256
            ):
                raise ValueError(f"{path} array bounds exceed supported limits")
            walk(node["items"], f"{path}.items")
        min_length = node.get("minLength", 0)
        max_length = node.get("maxLength", 2048)
        if (
            not isinstance(min_length, int)
            or not isinstance(max_length, int)
            or not 0 <= min_length <= max_length <= 2048
        ):
            raise ValueError(f"{path} string bounds exceed supported limits")

    walk(spec.schema, "$")
