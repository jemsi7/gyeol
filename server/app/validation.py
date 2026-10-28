"""Deterministic validation layer for LLM-produced maps.

Two stages, both pure and deterministic:

  normalize_map(raw)          -> coerce types / shape into the canonical dict.
                                 Never invents or drops semantic content; only
                                 fixes representation (strip, ensure keys/lists).

  validate_map(canonical, n)  -> STRICT gate. Returns ok/errors. If the format
                                 is broken (missing field, malformed/unknown/
                                 out-of-range/duplicate directive, inconsistent
                                 images[]), ok=False and the caller must NOT save
                                 it — instead requeue the article for a retry.

This mirrors docs/MAP_SPEC.md exactly; change them together.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .directives import parse_directives
from .geo_data import country_scopes
from .mapping_schema import MAP_FORMAT_VERSION

REQUIRED_STR_FIELDS = ("language", "headline", "summary", "body_markdown")
CLAIM_ID_RE = re.compile(r"^CL-\d{3}$")
SCENE_ID_RE = re.compile(r"^S-\d{2}$")
CALENDAR_YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
LAST_DIGIT_RE = re.compile(r"\d(?!.*\d)")
SCENE_ROLES = {"lead", "context", "evidence", "impact", "close"}


@dataclass(slots=True)
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    used_img_refs: list[int] = field(default_factory=list)
    used_chart_refs: list[int] = field(default_factory=list)


def normalize_map(raw: Any) -> dict:
    """Shape the raw model output into the canonical schema (deterministic)."""
    if not isinstance(raw, dict):
        raw = {}
    canonical = {
        "format_version": raw.get("format_version") or MAP_FORMAT_VERSION,
        "language": _s(raw.get("language")),
        "headline": _s(raw.get("headline")),
        "subheadline": _s(raw.get("subheadline")),
        "summary": _s(raw.get("summary")),
        "keywords": _str_list(raw.get("keywords")),
        "body_markdown": _s(raw.get("body_markdown")),
        "image_placements": _placements(raw.get("image_placements")),
        "cover_image_ref": _optional_ref(raw.get("cover_image_ref")),
        "visualizations": normalize_visualizations(raw.get("visualizations")),
        "claim_ids": _str_list(raw.get("claim_ids"), limit=100),
        "required_claim_refs": _str_list(raw.get("required_claim_refs"), limit=100),
        "selected_claim_refs": _str_list(raw.get("selected_claim_refs"), limit=100),
        "scenes": _scenes(raw.get("scenes")),
        "editorial": raw.get("editorial") if isinstance(raw.get("editorial"), dict) else {},
        # images[] is attached by the mapper (ref -> stored image), not the LLM.
        "images": [],
    }
    # coerce format_version to int if possible
    try:
        canonical["format_version"] = int(canonical["format_version"])
    except (TypeError, ValueError):
        canonical["format_version"] = MAP_FORMAT_VERSION
    return canonical


def validate_map(
    canonical: dict,
    *,
    n_images: int,
    source_text: str | None = None,
) -> ValidationResult:
    """Strict, deterministic format gate. No repair — pass or fail."""
    errors: list[str] = []

    if canonical.get("format_version") != MAP_FORMAT_VERSION:
        errors.append(
            f"format_version must be {MAP_FORMAT_VERSION}, got {canonical.get('format_version')!r}"
        )

    for f in REQUIRED_STR_FIELDS:
        v = canonical.get(f)
        if not isinstance(v, str) or not v.strip():
            errors.append(f"required field '{f}' is missing or empty")

    if not isinstance(canonical.get("keywords"), list):
        errors.append("keywords must be a list")

    # images[] must be internally consistent: contiguous refs 0..n-1, ids set.
    images = canonical.get("images") or []
    if len(images) != n_images:
        errors.append(f"images[] length {len(images)} != expected {n_images}")
    seen_refs = set()
    for i, img in enumerate(images):
        if not isinstance(img, dict):
            errors.append(f"images[{i}] is not an object")
            continue
        ref = img.get("ref")
        if ref != i:
            errors.append(f"images[{i}].ref must be {i}, got {ref!r}")
        if not img.get("image_id"):
            errors.append(f"images[{i}].image_id missing")
        seen_refs.add(ref)

    cover_ref = canonical.get("cover_image_ref")
    if cover_ref is not None and (
        not isinstance(cover_ref, int)
        or isinstance(cover_ref, bool)
        or not 0 <= cover_ref < n_images
    ):
        errors.append("cover_image_ref out of range")

    # directive tokens in the body must all be valid and reference real images.
    used: list[int] = []
    used_charts: list[int] = []
    visualizations = canonical.get("visualizations") or []
    for d in parse_directives(canonical.get("body_markdown", "")):
        if not d.ok:
            errors.append(f"malformed directive at {d.start}: {d.error}")
            continue
        if d.type == "img":
            ref = d.value
            if not isinstance(ref, int) or ref < 0 or ref >= n_images:
                errors.append(f"<img:{d.payload_raw}> references out-of-range image (have {n_images})")
            elif ref in used:
                errors.append(f"<img:{ref}> used more than once")
            else:
                used.append(ref)
        elif d.type == "chart":
            ref = d.value
            if not isinstance(ref, int) or ref < 0 or ref >= len(visualizations):
                errors.append(
                    f"<chart:{d.payload_raw}> references out-of-range visualization "
                    f"(have {len(visualizations)})"
                )
            elif ref in used_charts:
                errors.append(f"<chart:{ref}> used more than once")
            else:
                used_charts.append(ref)
            if not _directive_is_standalone(canonical.get("body_markdown", ""), d.start, d.end):
                errors.append(f"<chart:{d.payload_raw}> must be on its own line")

    # image_placements refs must be in range (advisory metadata, still checked)
    for p in canonical.get("image_placements") or []:
        ref = p.get("ref")
        if not isinstance(ref, int) or ref < 0 or ref >= n_images:
            errors.append(f"image_placements ref {ref!r} out of range")

    _validate_visualizations(
        visualizations,
        source_text=source_text,
        used_chart_refs=used_charts,
        errors=errors,
    )
    _validate_v3_scenes(
        canonical,
        n_images=n_images,
        n_charts=len(visualizations),
        used_img_refs=used,
        used_chart_refs=used_charts,
        errors=errors,
    )

    return ValidationResult(
        ok=not errors,
        errors=errors,
        used_img_refs=sorted(used),
        used_chart_refs=sorted(used_charts),
    )


def _s(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""


def _optional_ref(v: Any) -> Any:
    if v is None or isinstance(v, bool):
        return v
    try:
        return int(v)
    except (TypeError, ValueError):
        return v


def _str_list(v: Any, *, limit: int = 12) -> list[str]:
    if isinstance(v, list):
        return [x.strip() for x in v if isinstance(x, str) and x.strip()][:limit]
    return []


def _scenes(v: Any) -> list[dict]:
    out: list[dict] = []
    if not isinstance(v, list):
        return out
    for raw in v:
        raw = raw if isinstance(raw, dict) else {}
        image_ref = raw.get("image_ref")
        chart_ref = raw.get("chart_ref")
        try:
            image_ref = None if image_ref is None or isinstance(image_ref, bool) else int(image_ref)
        except (TypeError, ValueError):
            image_ref = None
        try:
            chart_ref = None if chart_ref is None or isinstance(chart_ref, bool) else int(chart_ref)
        except (TypeError, ValueError):
            chart_ref = None
        out.append(
            {
                "id": _s(raw.get("id")),
                "role": _s(raw.get("role")),
                "label": _s(raw.get("label")),
                "title": _s(raw.get("title")),
                "summary": _s(raw.get("summary")),
                "body": _s(raw.get("body")),
                "secondary": _s(raw.get("secondary")),
                "quote": _s(raw.get("quote")),
                "image_ref": image_ref,
                "chart_ref": chart_ref,
                "claim_refs": _str_list(raw.get("claim_refs"), limit=100),
            }
        )
    return out


def _placements(v: Any) -> list[dict]:
    out: list[dict] = []
    if isinstance(v, list):
        for p in v:
            if not isinstance(p, dict):
                continue
            try:
                ref = int(p.get("ref"))
            except (TypeError, ValueError):
                continue
            out.append({"ref": ref, "caption": _s(p.get("caption")), "reason": _s(p.get("reason"))})
    return out


def normalize_visualizations(
    v: Any,
    *,
    sentence_index: dict[str, dict] | None = None,
) -> list[dict]:
    out: list[dict] = []
    if not isinstance(v, list):
        return out
    for chart in v:
        if not isinstance(chart, dict):
            out.append({})
            continue
        ref: Any = chart.get("ref")
        try:
            ref = int(ref)
        except (TypeError, ValueError):
            pass
        series_out: list[dict] = []
        series_raw = chart.get("series")
        if isinstance(series_raw, list):
            for series in series_raw:
                if not isinstance(series, dict):
                    series_out.append({})
                    continue
                points_out: list[dict] = []
                points_raw = series.get("points")
                if isinstance(points_raw, list):
                    for point in points_raw:
                        if not isinstance(point, dict):
                            if sentence_index is None:
                                points_out.append({})
                            continue
                        raw_value = _s(point.get("raw_value"))
                        evidence = _s(point.get("evidence"))
                        if sentence_index is not None:
                            sentence_ref = _s(point.get("sentence_ref"))
                            sentence = sentence_index.get(sentence_ref)
                            sentence_text = ""
                            if isinstance(sentence, dict):
                                sentence_text = _s(sentence.get("text"))
                            elif not sentence_ref and evidence:
                                sentence_text = next(
                                    (
                                        _s(candidate.get("text"))
                                        for candidate in sentence_index.values()
                                        if isinstance(candidate, dict)
                                        and evidence in _s(candidate.get("text"))
                                    ),
                                    "",
                                )
                                if sentence_text:
                                    sentence_text = evidence
                            if not sentence_text or not raw_value or raw_value not in sentence_text:
                                continue
                            evidence = sentence_text
                        points_out.append(
                            {
                                "label": _s(point.get("label")),
                                "value": point.get("value"),
                                "raw_value": raw_value,
                                "evidence": evidence,
                                "region_code": _s(point.get("region_code")),
                                "row": _s(point.get("row")),
                                "column": _s(point.get("column")),
                            }
                        )
                if sentence_index is not None and not points_out:
                    continue
                series_out.append(
                    {
                        "name": _s(series.get("name")),
                        "mark": _s(series.get("mark")),
                        "points": points_out,
                    }
                )
        if sentence_index is not None:
            point_count = sum(
                len(series.get("points") or [])
                for series in series_out
                if isinstance(series, dict)
            )
            if point_count < 2:
                continue
            ref = len(out)
        out.append(
            {
                "ref": ref,
                "kind": _s(chart.get("kind")),
                "title": _s(chart.get("title")),
                "unit": _s(chart.get("unit")),
                "alt_text": _s(chart.get("alt_text")),
                "geo_scope": _s(chart.get("geo_scope")),
                "color_scale": (
                    {
                        "scheme": _s(chart.get("color_scale", {}).get("scheme")),
                        "mode": _s(chart.get("color_scale", {}).get("mode")),
                    }
                    if isinstance(chart.get("color_scale"), dict)
                    else None
                ),
                "series": series_out,
            }
        )
    return out


def _directive_is_standalone(body: str, start: int, end: int) -> bool:
    line_start = body.rfind("\n", 0, start) + 1
    line_end = body.find("\n", end)
    if line_end < 0:
        line_end = len(body)
    return body[line_start:line_end].strip() == body[start:end]


def _validate_v3_scenes(
    canonical: dict,
    *,
    n_images: int,
    n_charts: int,
    used_img_refs: list[int],
    used_chart_refs: list[int],
    errors: list[str],
) -> None:
    scenes = canonical.get("scenes")
    if not isinstance(scenes, list) or not 3 <= len(scenes) <= 9:
        errors.append("scenes length must be 3..9")
        scenes = scenes if isinstance(scenes, list) else []

    claim_ids = canonical.get("claim_ids")
    if not isinstance(claim_ids, list) or not claim_ids:
        errors.append("claim_ids must be a non-empty list")
        claim_ids = claim_ids if isinstance(claim_ids, list) else []
    if len(claim_ids) != len(set(claim_ids)) or any(
        not isinstance(value, str) or not CLAIM_ID_RE.fullmatch(value)
        for value in claim_ids
    ):
        errors.append("claim_ids must be unique CL-nnn identifiers")
    claim_set = set(claim_ids)

    required = canonical.get("required_claim_refs")
    selected = canonical.get("selected_claim_refs")
    required = required if isinstance(required, list) else []
    selected = selected if isinstance(selected, list) else []
    if not required or len(required) != len(set(required)) or not set(required) <= claim_set:
        errors.append("required_claim_refs must be a non-empty unique subset of claim_ids")
    if not selected or len(selected) != len(set(selected)) or not set(selected) <= claim_set:
        errors.append("selected_claim_refs must be a non-empty unique subset of claim_ids")
    if not set(required) <= set(selected):
        errors.append("required_claim_refs must be included in selected_claim_refs")

    assigned_claims: list[str] = []
    scene_image_refs: list[int] = []
    scene_chart_refs: list[int] = []
    expected_scene_ids = [f"S-{index + 1:02d}" for index in range(len(scenes))]
    if [scene.get("id") for scene in scenes if isinstance(scene, dict)] != expected_scene_ids:
        errors.append("scene IDs must be contiguous S-01..")
    if scenes and isinstance(scenes[0], dict) and scenes[0].get("role") != "lead":
        errors.append("first scene role must be lead")
    if scenes and isinstance(scenes[-1], dict) and scenes[-1].get("role") != "close":
        errors.append("last scene role must be close")

    for index, scene in enumerate(scenes):
        path = f"scenes[{index}]"
        if not isinstance(scene, dict):
            errors.append(f"{path} must be an object")
            continue
        if scene.get("role") not in SCENE_ROLES:
            errors.append(f"{path}.role unsupported")
        for field in ("label", "title"):
            if not isinstance(scene.get(field), str) or not scene[field].strip():
                errors.append(f"{path}.{field} must be non-empty")
        if len(scene.get("label", "")) > 24 or len(scene.get("title", "")) > 90:
            errors.append(f"{path} label/title exceeds limit")
        if scene.get("role") == "lead" and not scene.get("summary"):
            errors.append(f"{path} lead summary is required")
        if scene.get("role") != "lead" and scene.get("summary"):
            errors.append(f"{path} non-lead summary must be empty")
        for field, limit in (("summary", 320), ("secondary", 300), ("quote", 240)):
            value = scene.get(field)
            if not isinstance(value, str):
                errors.append(f"{path}.{field} must be a string")
            elif len(value) > limit:
                errors.append(f"{path}.{field} exceeds {limit} chars")

        refs = scene.get("claim_refs")
        if not isinstance(refs, list) or not refs:
            errors.append(f"{path}.claim_refs must be non-empty")
            refs = refs if isinstance(refs, list) else []
        if len(refs) != len(set(refs)) or any(ref not in claim_set for ref in refs):
            errors.append(f"{path}.claim_refs must be unique valid claim IDs")
        assigned_claims.extend(refs)

        image_ref, chart_ref = scene.get("image_ref"), scene.get("chart_ref")
        if image_ref is not None:
            if not isinstance(image_ref, int) or isinstance(image_ref, bool) or not 0 <= image_ref < n_images:
                errors.append(f"{path}.image_ref out of range")
            else:
                scene_image_refs.append(image_ref)
        if chart_ref is not None:
            if not isinstance(chart_ref, int) or isinstance(chart_ref, bool) or not 0 <= chart_ref < n_charts:
                errors.append(f"{path}.chart_ref out of range")
            else:
                scene_chart_refs.append(chart_ref)
        if image_ref is not None and chart_ref is not None:
            errors.append(f"{path} cannot combine image_ref and chart_ref")
        if chart_ref is not None and any(scene.get(field) for field in ("body", "secondary", "quote")):
            errors.append(f"{path} chart scene must not contain prose blocks")
        if chart_ref is None and not scene.get("body"):
            errors.append(f"{path}.body required for non-chart scene")

    assigned_counts = Counter(assigned_claims)
    if any(count != 1 for count in assigned_counts.values()):
        errors.append("claim refs may appear in only one scene")
    if set(assigned_claims) != set(selected):
        errors.append("scenes must cover selected_claim_refs exactly")
    if len(scene_image_refs) != len(set(scene_image_refs)):
        errors.append("scene image refs may be used at most once")
    cover_ref = canonical.get("cover_image_ref")
    if isinstance(cover_ref, int) and not isinstance(cover_ref, bool) and cover_ref in scene_image_refs:
        errors.append("cover_image_ref must not repeat a scene image ref")
    if sorted(scene_chart_refs) != list(range(n_charts)) or len(scene_chart_refs) != len(set(scene_chart_refs)):
        errors.append("scenes must reference every chart exactly once")
    if sorted(scene_image_refs) != sorted(used_img_refs):
        errors.append("scene image refs must match body image directives")
    if sorted(scene_chart_refs) != sorted(used_chart_refs):
        errors.append("scene chart refs must match body chart directives")

    editorial = canonical.get("editorial")
    if not isinstance(editorial, dict):
        errors.append("editorial metadata must be an object")
    else:
        if editorial.get("mode") != "editorial_completeness":
            errors.append("editorial.mode must be editorial_completeness")
        if editorial.get("critic_verdict") != "pass":
            errors.append("editorial.critic_verdict must be pass")


def _validate_visualizations(
    visualizations: list[Any],
    *,
    source_text: str | None,
    used_chart_refs: list[int],
    errors: list[str],
) -> None:
    standard_kinds = {"bar", "line", "area", "pie", "mixed"}
    geo_kinds = {"world_map", "region_heatmap"}
    matrix_kinds = {"matrix_heatmap"}
    allowed_kinds = standard_kinds | geo_kinds | matrix_kinds
    allowed_marks = {"bar", "line", "area", "sector", "region", "cell"}
    regional_scopes = {
        "africa", "asia", "europe", "north_america", "south_america", "oceania"
    }
    color_schemes = {"ocean", "forest", "ember", "violet", "diverging"}
    color_modes = {"continuous", "quantized"}
    if len(visualizations) > 2:
        errors.append(f"visualizations length {len(visualizations)} exceeds 2")

    def validate_metadata_years(value: Any, path: str) -> None:
        if not isinstance(value, str) or source_text is None:
            return
        for year in CALENDAR_YEAR_RE.findall(value):
            if year not in source_text:
                errors.append(f"{path} contains calendar year {year} absent from source")

    for index, chart in enumerate(visualizations):
        path = f"visualizations[{index}]"
        if not isinstance(chart, dict):
            errors.append(f"{path} must be an object")
            continue
        if chart.get("ref") != index:
            errors.append(f"{path}.ref must be {index}, got {chart.get('ref')!r}")
        kind = chart.get("kind")
        if kind not in allowed_kinds:
            errors.append(f"{path}.kind unsupported: {kind!r}")
        for field_name in ("title", "unit", "alt_text"):
            if not isinstance(chart.get(field_name), str) or not chart[field_name].strip():
                errors.append(f"{path}.{field_name} must be non-empty")
        validate_metadata_years(chart.get("title"), f"{path}.title")
        validate_metadata_years(chart.get("alt_text"), f"{path}.alt_text")

        # Maps emitted before the additive geo contract do not carry this key.
        # Treat absence as the standard-chart empty value while still rejecting
        # an explicitly malformed null/non-string value.
        geo_scope = chart.get("geo_scope", "")
        if not isinstance(geo_scope, str):
            errors.append(f"{path}.geo_scope must be a string")
            geo_scope = ""
        if kind == "world_map" and geo_scope != "world":
            errors.append(f"{path} world_map requires geo_scope 'world'")
        elif kind == "region_heatmap" and geo_scope not in regional_scopes:
            errors.append(
                f"{path} region_heatmap requires one supported continent geo_scope"
            )
        elif kind not in geo_kinds and geo_scope:
            errors.append(f"{path} kind {kind!r} requires empty geo_scope")

        color_scale = chart.get("color_scale")
        if kind in geo_kinds | matrix_kinds and not isinstance(color_scale, dict):
            errors.append(f"{path}.color_scale is required for {kind}")
        if color_scale is not None:
            if not isinstance(color_scale, dict):
                errors.append(f"{path}.color_scale must be an object or null")
            else:
                if color_scale.get("scheme") not in color_schemes:
                    errors.append(f"{path}.color_scale.scheme unsupported")
                if color_scale.get("mode") not in color_modes:
                    errors.append(f"{path}.color_scale.mode unsupported")

        series = chart.get("series")
        if not isinstance(series, list) or not 1 <= len(series) <= 4:
            errors.append(f"{path}.series length must be 1..4")
            series = series if isinstance(series, list) else []
        if kind in {"pie", *geo_kinds, *matrix_kinds} and len(series) != 1:
            errors.append(f"{path} {kind} must contain exactly one series")

        marks: set[str] = set()
        point_count = 0
        numeric_values: list[float] = []
        raw_unit_suffixes: set[str] = set()
        region_codes: list[str] = []
        matrix_cells: list[tuple[str, str]] = []
        for series_index, one_series in enumerate(series):
            series_path = f"{path}.series[{series_index}]"
            if not isinstance(one_series, dict):
                errors.append(f"{series_path} must be an object")
                continue
            if not isinstance(one_series.get("name"), str) or not one_series["name"].strip():
                errors.append(f"{series_path}.name must be non-empty")
            validate_metadata_years(one_series.get("name"), f"{series_path}.name")
            mark = one_series.get("mark")
            if mark not in allowed_marks:
                errors.append(f"{series_path}.mark unsupported: {mark!r}")
            else:
                marks.add(mark)
            points = one_series.get("points")
            if not isinstance(points, list) or not points:
                errors.append(f"{series_path}.points must be non-empty")
                points = points if isinstance(points, list) else []
            point_count += len(points)
            for point_index, point in enumerate(points):
                point_path = f"{series_path}.points[{point_index}]"
                if not isinstance(point, dict):
                    errors.append(f"{point_path} must be an object")
                    continue
                for field_name in ("label", "raw_value", "evidence"):
                    if not isinstance(point.get(field_name), str) or not point[field_name].strip():
                        errors.append(f"{point_path}.{field_name} must be non-empty")
                validate_metadata_years(point.get("label"), f"{point_path}.label")
                value = point.get("value")
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                ):
                    errors.append(f"{point_path}.value must be a finite number")
                else:
                    numeric_values.append(float(value))
                raw_value = point.get("raw_value")
                evidence = point.get("evidence")
                if isinstance(raw_value, str):
                    last_digit = LAST_DIGIT_RE.search(raw_value)
                    if last_digit is not None:
                        suffix = re.sub(r"\s+", "", raw_value[last_digit.end():])
                        suffix = suffix.strip(".,;:!?()[]{}")
                        if suffix:
                            raw_unit_suffixes.add(suffix)
                if isinstance(raw_value, str) and isinstance(evidence, str) and raw_value not in evidence:
                    errors.append(f"{point_path}.raw_value is not contained in evidence")
                if source_text is None:
                    errors.append(f"{point_path}.evidence cannot be verified without source_text")
                elif isinstance(evidence, str) and evidence not in source_text:
                    errors.append(f"{point_path}.evidence is not an exact source substring")

                region_code = point.get("region_code", "")
                row = point.get("row", "")
                column = point.get("column", "")
                for field_name, field_value in (
                    ("region_code", region_code), ("row", row), ("column", column)
                ):
                    if not isinstance(field_value, str):
                        errors.append(f"{point_path}.{field_name} must be a string")
                if kind in geo_kinds:
                    if not isinstance(region_code, str) or not re.fullmatch(r"[A-Z]{2}", region_code):
                        errors.append(f"{point_path}.region_code must be uppercase ISO alpha-2")
                    else:
                        region_codes.append(region_code)
                    if row or column:
                        errors.append(f"{point_path} geographic points require empty row/column")
                elif kind in matrix_kinds:
                    if not isinstance(row, str) or not row.strip() or not isinstance(column, str) or not column.strip():
                        errors.append(f"{point_path} matrix points require non-empty row and column")
                    else:
                        matrix_cells.append((row, column))
                    if region_code:
                        errors.append(f"{point_path} matrix points require empty region_code")
                elif region_code or row or column:
                    errors.append(f"{point_path} standard chart points require empty geo fields")

        if kind == "world_map":
            minimum, maximum = 1, 200
        elif kind == "region_heatmap":
            minimum, maximum = 2, 200
        elif kind == "matrix_heatmap":
            minimum, maximum = 2, 100
        else:
            minimum, maximum = 2, 12
        if not minimum <= point_count <= maximum:
            errors.append(
                f"{path} total point count must be {minimum}..{maximum}, got {point_count}"
            )
        if len(raw_unit_suffixes) > 1:
            errors.append(
                f"{path} raw_value unit suffixes must match: "
                + ", ".join(sorted(raw_unit_suffixes))
            )
        if kind in {"bar", "line", "area"} and marks and marks != {kind}:
            errors.append(f"{path} kind {kind!r} requires only {kind!r} marks")
        if kind == "pie" and marks and marks != {"sector"}:
            errors.append(f"{path} pie requires only 'sector' marks")
        if kind == "pie" and numeric_values and (
            any(value < 0 for value in numeric_values) or not any(value > 0 for value in numeric_values)
        ):
            errors.append(f"{path} pie values must be non-negative with a positive total")
        if kind == "mixed" and ("sector" in marks or len(marks) < 2):
            errors.append(f"{path} mixed requires at least two of bar/line/area marks")
        if kind in geo_kinds and marks and marks != {"region"}:
            errors.append(f"{path} {kind} requires only 'region' marks")
        if kind in matrix_kinds and marks and marks != {"cell"}:
            errors.append(f"{path} matrix_heatmap requires only 'cell' marks")

        if len(region_codes) != len(set(region_codes)):
            errors.append(f"{path} region_code values must be unique")
        if len(matrix_cells) != len(set(matrix_cells)):
            errors.append(f"{path} row/column pairs must be unique")
        if kind in geo_kinds and region_codes:
            try:
                scopes = country_scopes()
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                scopes = {}
                errors.append(f"{path} geographic country registry is unavailable")
            for region_code in region_codes:
                actual_scope = scopes.get(region_code)
                if actual_scope is None:
                    errors.append(f"{path} region_code {region_code!r} is unknown")
                elif kind == "region_heatmap" and actual_scope != geo_scope:
                    errors.append(
                        f"{path} region_code {region_code!r} does not belong to scope {geo_scope!r}"
                    )
        if (
            isinstance(color_scale, dict)
            and color_scale.get("scheme") == "diverging"
            and numeric_values
            and not (min(numeric_values) < 0 < max(numeric_values))
        ):
            errors.append(
                f"{path} diverging requires negative and positive values"
            )

    expected_refs = list(range(len(visualizations)))
    if sorted(used_chart_refs) != expected_refs:
        errors.append(
            f"chart directives {sorted(used_chart_refs)} must reference every visualization "
            f"exactly once {expected_refs}"
        )


def validate_visualizations(
    visualizations: list[Any],
    *,
    source_text: str | None,
    used_chart_refs: list[int] | None = None,
) -> list[str]:
    """Validate chart data outside the final map gate.

    EvidenceLedger uses this before a ScenePlan exists, so the default treats
    each normalized visualization as intentionally selected exactly once.
    """
    errors: list[str] = []
    _validate_visualizations(
        visualizations,
        source_text=source_text,
        used_chart_refs=(
            list(range(len(visualizations)))
            if used_chart_refs is None
            else used_chart_refs
        ),
        errors=errors,
    )
    return errors
