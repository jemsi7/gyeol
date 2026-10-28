"""Offline tests for the deterministic validation gate + map resolution."""
from app.directives import parse_directives
from app.editorial_prompts import EVIDENCE_SYSTEM_PROMPT, PLANNING_SYSTEM_PROMPT
from app.mapping_schema import MAP_FORMAT_VERSION
from app.validation import normalize_map, validate_map
from app.map_loader import resolve_map


def _good_map(n=2):
    return {
        "format_version": MAP_FORMAT_VERSION,
        "language": "en",
        "headline": "H",
        "subheadline": "",
        "summary": "A summary.",
        "keywords": ["a", "b"],
        "body_markdown": "para one\n<img:0>\nmid\n<img:1>\nend",
        "image_placements": [{"ref": 0, "caption": "lead", "reason": "top"}],
        "visualizations": [],
        "claim_ids": ["CL-001", "CL-002", "CL-003"],
        "required_claim_refs": ["CL-001", "CL-002"],
        "selected_claim_refs": ["CL-001", "CL-002", "CL-003"],
        "scenes": [
            {
                "id": "S-01", "role": "lead", "label": "lead", "title": "Lead",
                "summary": "A summary.", "body": "para one", "secondary": "", "quote": "",
                "image_ref": 0 if n else None, "chart_ref": None, "claim_refs": ["CL-001"],
            },
            {
                "id": "S-02", "role": "evidence", "label": "middle", "title": "Middle",
                "summary": "", "body": "mid", "secondary": "", "quote": "",
                "image_ref": 1 if n > 1 else None, "chart_ref": None, "claim_refs": ["CL-002"],
            },
            {
                "id": "S-03", "role": "close", "label": "close", "title": "Close",
                "summary": "", "body": "end", "secondary": "", "quote": "",
                "image_ref": None, "chart_ref": None, "claim_refs": ["CL-003"],
            },
        ],
        "editorial": {
            "mode": "editorial_completeness",
            "pipeline_version": "test",
            "source_hash": "hash",
            "critic_verdict": "pass",
        },
        "images": [
            {"ref": i, "image_id": f"id{i}", "source_url": "u", "role": "content"}
            for i in range(n)
        ],
    }


def test_gate_passes_valid_map():
    m = normalize_map(_good_map())
    m["images"] = _good_map()["images"]
    vr = validate_map(m, n_images=2)
    assert vr.ok, vr.errors
    assert vr.used_img_refs == [0, 1]


def test_gate_accepts_valid_cover_independent_from_used_refs():
    m = _good_map(n=3)
    m["cover_image_ref"] = 2
    canonical = normalize_map(m)
    canonical["images"] = m["images"]

    vr = validate_map(canonical, n_images=3)

    assert vr.ok, vr.errors
    assert vr.used_img_refs == [0, 1]


def test_gate_rejects_cover_repeated_in_scene():
    m = _good_map()
    m["cover_image_ref"] = 0
    canonical = normalize_map(m)
    canonical["images"] = m["images"]

    vr = validate_map(canonical, n_images=2)

    assert not vr.ok
    assert "cover_image_ref must not repeat a scene image ref" in vr.errors


def test_gate_rejects_invalid_cover_ref():
    for value in (2, True, "unknown"):
        m = _good_map()
        m["cover_image_ref"] = value
        canonical = normalize_map(m)
        canonical["images"] = m["images"]

        vr = validate_map(canonical, n_images=2)

        assert not vr.ok
        assert "cover_image_ref out of range" in vr.errors


def test_gate_fails_out_of_range_placeholder():
    m = _good_map()
    m["body_markdown"] = "x <img:0> y <img:5>"
    m = {**normalize_map(m), "images": m["images"]}
    vr = validate_map(m, n_images=2)
    assert not vr.ok
    assert any("out-of-range" in e for e in vr.errors)


def test_gate_fails_duplicate_placeholder():
    m = _good_map()
    m["body_markdown"] = "<img:0> ... <img:0>"
    m = {**normalize_map(m), "images": m["images"]}
    vr = validate_map(m, n_images=2)
    assert not vr.ok
    assert any("used more than once" in e for e in vr.errors)


def test_gate_fails_unknown_directive():
    m = _good_map()
    m["body_markdown"] = "text <graph:1> more"
    m = {**normalize_map(m), "images": m["images"]}
    vr = validate_map(m, n_images=2)
    assert not vr.ok
    assert any("unknown directive" in e for e in vr.errors)


def _chart(kind="bar", marks=("bar",)):
    evidence = "A reached 10% while B reached 20%."
    series = []
    for index, mark in enumerate(marks):
        series.append(
            {
                "name": f"series {index + 1}",
                "mark": mark,
                "points": [
                    {"label": "A", "value": 10, "raw_value": "10%", "evidence": evidence},
                    {"label": "B", "value": 20, "raw_value": "20%", "evidence": evidence},
                ],
            }
        )
    return {
        "ref": 0,
        "kind": kind,
        "title": "A and B",
        "unit": "%",
        "alt_text": "A is 10% and B is 20%.",
        "series": series,
    }


def _geo_chart(kind="world_map", scope="world"):
    chart = _chart(kind, ("region",))
    chart["geo_scope"] = scope
    chart["color_scale"] = {"scheme": "ocean", "mode": "quantized"}
    for point, code in zip(chart["series"][0]["points"], ("KR", "JP"), strict=True):
        point["region_code"] = code
        point["row"] = ""
        point["column"] = ""
    return chart


def _matrix_chart():
    chart = _chart("matrix_heatmap", ("cell",))
    chart["geo_scope"] = ""
    chart["color_scale"] = {"scheme": "violet", "mode": "continuous"}
    for point, (row, column) in zip(
        chart["series"][0]["points"],
        (("아시아", "현재"), ("유럽", "현재")),
        strict=True,
    ):
        point["region_code"] = ""
        point["row"] = row
        point["column"] = column
    return chart


def _map_with_chart(kind="bar", marks=("bar",)):
    m = _good_map(n=0)
    m["body_markdown"] = "A reached 10% while B reached 20%.\n\n<chart:0>"
    m["image_placements"] = []
    m["visualizations"] = [_chart(kind, marks)]
    m["claim_ids"] = ["CL-001", "CL-002", "CL-003"]
    m["required_claim_refs"] = ["CL-001", "CL-002"]
    m["selected_claim_refs"] = ["CL-001", "CL-002", "CL-003"]
    m["scenes"] = [
        {
            "id": "S-01", "role": "lead", "label": "lead", "title": "Lead",
            "summary": "A summary.", "body": "A reached 10% while B reached 20%.",
            "secondary": "", "quote": "", "image_ref": None, "chart_ref": None,
            "claim_refs": ["CL-001"],
        },
        {
            "id": "S-02", "role": "evidence", "label": "chart", "title": "Chart",
            "summary": "", "body": "", "secondary": "", "quote": "",
            "image_ref": None, "chart_ref": 0, "claim_refs": ["CL-002"],
        },
        {
            "id": "S-03", "role": "close", "label": "close", "title": "Close",
            "summary": "", "body": "Closing context.", "secondary": "", "quote": "",
            "image_ref": None, "chart_ref": None, "claim_refs": ["CL-003"],
        },
    ]
    m["editorial"] = {
        "mode": "editorial_completeness", "pipeline_version": "test",
        "source_hash": "hash", "critic_verdict": "pass",
    }
    canonical = normalize_map(m)
    canonical["images"] = []
    return canonical


def test_gate_accepts_all_supported_visualization_kinds():
    cases = [
        ("bar", ("bar",)),
        ("line", ("line",)),
        ("area", ("area",)),
        ("pie", ("sector",)),
        ("mixed", ("bar", "line")),
    ]
    source = "A reached 10% while B reached 20%."
    for kind, marks in cases:
        vr = validate_map(_map_with_chart(kind, marks), n_images=0, source_text=source)
        assert vr.ok, (kind, vr.errors)
        assert vr.used_chart_refs == [0]


def test_gate_accepts_world_region_and_matrix_heatmaps():
    source = "A reached 10% while B reached 20%."
    cases = [
        _geo_chart("world_map", "world"),
        _geo_chart("region_heatmap", "asia"),
        _matrix_chart(),
    ]
    for chart in cases:
        mapped = _map_with_chart()
        mapped["visualizations"] = [chart]
        result = validate_map(mapped, n_images=0, source_text=source)
        assert result.ok, (chart["kind"], result.errors)


def test_gate_rejects_invalid_geo_matrix_and_color_contracts():
    source = "A reached 10% while B reached 20%."

    duplicate_region = _map_with_chart()
    duplicate_region["visualizations"] = [_geo_chart()]
    duplicate_region["visualizations"][0]["series"][0]["points"][1]["region_code"] = "KR"
    assert any(
        "region_code values must be unique" in error
        for error in validate_map(duplicate_region, n_images=0, source_text=source).errors
    )

    wrong_scope = _map_with_chart()
    wrong_scope["visualizations"] = [_geo_chart("region_heatmap", "europe")]
    assert any(
        "does not belong to scope" in error
        for error in validate_map(wrong_scope, n_images=0, source_text=source).errors
    )

    duplicate_cell = _map_with_chart()
    duplicate_cell["visualizations"] = [_matrix_chart()]
    points = duplicate_cell["visualizations"][0]["series"][0]["points"]
    points[1]["row"] = points[0]["row"]
    points[1]["column"] = points[0]["column"]
    assert any(
        "row/column pairs must be unique" in error
        for error in validate_map(duplicate_cell, n_images=0, source_text=source).errors
    )

    no_scale = _map_with_chart()
    no_scale["visualizations"] = [_matrix_chart()]
    no_scale["visualizations"][0]["color_scale"] = None
    assert any(
        "color_scale is required" in error
        for error in validate_map(no_scale, n_images=0, source_text=source).errors
    )

    invalid_diverging = _map_with_chart()
    invalid_diverging["visualizations"] = [_geo_chart()]
    invalid_diverging["visualizations"][0]["color_scale"] = {
        "scheme": "diverging",
        "mode": "continuous",
    }
    assert any(
        "diverging requires negative and positive values" in error
        for error in validate_map(invalid_diverging, n_images=0, source_text=source).errors
    )


def test_gate_rejects_chart_evidence_not_in_source():
    m = _map_with_chart()
    m["visualizations"][0]["series"][0]["points"][0]["evidence"] = "Invented 10% sentence."
    vr = validate_map(m, n_images=0, source_text="A reached 10% while B reached 20%.")
    assert not vr.ok
    assert any("exact source substring" in error for error in vr.errors)


def test_gate_rejects_chart_when_source_text_is_unavailable():
    vr = validate_map(_map_with_chart(), n_images=0, source_text=None)
    assert not vr.ok
    assert any("cannot be verified without source_text" in error for error in vr.errors)


def test_gate_rejects_raw_value_not_in_evidence():
    m = _map_with_chart()
    m["visualizations"][0]["series"][0]["points"][0]["raw_value"] = "99%"
    vr = validate_map(m, n_images=0, source_text="A reached 10% while B reached 20%.")
    assert not vr.ok
    assert any("not contained in evidence" in error for error in vr.errors)


def test_gate_rejects_mixed_raw_value_units_in_one_chart():
    m = _map_with_chart()
    point = m["visualizations"][0]["series"][0]["points"][1]
    point["raw_value"] = "20시간"
    point["evidence"] = "B took 20시간."

    vr = validate_map(
        m,
        n_images=0,
        source_text="A reached 10% while B took 20시간.",
    )

    assert not vr.ok
    assert "visualizations[0] raw_value unit suffixes must match: %, 시간" in vr.errors


def test_gate_rejects_chart_metadata_year_absent_from_source():
    m = _map_with_chart()
    m["visualizations"][0]["title"] = "2023 A and B"

    vr = validate_map(
        m,
        n_images=0,
        source_text="A reached 10% while B reached 20%.",
    )

    assert not vr.ok
    assert "visualizations[0].title contains calendar year 2023 absent from source" in vr.errors


def test_gate_accepts_chart_metadata_year_present_in_source():
    m = _map_with_chart()
    m["visualizations"][0]["title"] = "2023 A and B"

    vr = validate_map(
        m,
        n_images=0,
        source_text="In 2023, A reached 10% while B reached 20%.",
    )

    assert vr.ok, vr.errors


def test_gate_rejects_inline_duplicate_and_unreferenced_charts():
    source = "A reached 10% while B reached 20%."
    inline = _map_with_chart()
    inline["body_markdown"] = "text <chart:0>"
    assert any(
        "own line" in error
        for error in validate_map(inline, n_images=0, source_text=source).errors
    )

    duplicate = _map_with_chart()
    duplicate["body_markdown"] += "\n<chart:0>"
    assert any(
        "more than once" in error
        for error in validate_map(duplicate, n_images=0, source_text=source).errors
    )

    missing = _map_with_chart()
    missing["body_markdown"] = "no chart directive"
    assert any(
        "must reference every visualization" in error
        for error in validate_map(missing, n_images=0, source_text=source).errors
    )


def test_gate_rejects_wrong_mark_point_limit_and_nonfinite_value():
    source = "A reached 10% while B reached 20%."
    wrong_mark = _map_with_chart("pie", ("bar",))
    assert any(
        "sector" in error
        for error in validate_map(wrong_mark, n_images=0, source_text=source).errors
    )

    too_many = _map_with_chart()
    point = too_many["visualizations"][0]["series"][0]["points"][0]
    too_many["visualizations"][0]["series"][0]["points"] = [dict(point) for _ in range(13)]
    assert any(
        "2..12" in error
        for error in validate_map(too_many, n_images=0, source_text=source).errors
    )

    nonfinite = _map_with_chart()
    nonfinite["visualizations"][0]["series"][0]["points"][0]["value"] = float("nan")
    assert any(
        "finite number" in error
        for error in validate_map(nonfinite, n_images=0, source_text=source).errors
    )

    negative_pie = _map_with_chart("pie", ("sector",))
    negative_pie["visualizations"][0]["series"][0]["points"][0]["value"] = -10
    assert any(
        "non-negative" in error
        for error in validate_map(negative_pie, n_images=0, source_text=source).errors
    )


def test_gate_rejects_more_than_two_visualizations():
    m = _map_with_chart()
    charts = []
    directives = []
    for ref in range(3):
        chart = _chart()
        chart["ref"] = ref
        charts.append(chart)
        directives.append(f"<chart:{ref}>")
    m["visualizations"] = charts
    m["body_markdown"] = "A reached 10% while B reached 20%.\n" + "\n".join(directives)
    vr = validate_map(m, n_images=0, source_text="A reached 10% while B reached 20%.")
    assert not vr.ok
    assert any("exceeds 2" in error for error in vr.errors)


def test_prompt_requires_source_grounded_comparable_values():
    assert "evidence" in EVIDENCE_SYSTEM_PROMPT
    assert "copied exactly" in EVIDENCE_SYSTEM_PROMPT
    assert "essential" in EVIDENCE_SYSTEM_PROMPT
    assert "editorial direction" in PLANNING_SYSTEM_PROMPT
    assert "server, not you, owns scene IDs" in PLANNING_SYSTEM_PROMPT


def test_gate_fails_missing_required_field():
    m = _good_map()
    m["headline"] = ""
    m = {**normalize_map(m), "images": m["images"]}
    vr = validate_map(m, n_images=2)
    assert not vr.ok
    assert any("headline" in e for e in vr.errors)


def test_gate_fails_inconsistent_images():
    m = _good_map(n=2)
    canonical = normalize_map(m)
    canonical["images"] = [{"ref": 0, "image_id": "id0"}]  # length mismatch
    vr = validate_map(canonical, n_images=2)
    assert not vr.ok
    assert any("images[] length" in e for e in vr.errors)


def test_malformed_img_payload_detected():
    ds = parse_directives("a <img:abc> b")
    assert ds and ds[0].ok is False


def test_resolve_map_rewrites_placeholders():
    m = _good_map(n=3)
    m["cover_image_ref"] = 2
    view = resolve_map(m, lambda iid: f"/images/{iid}")
    assert "![lead](/images/id0)" in view["body_resolved"]
    assert "![](/images/id1)" in view["body_resolved"]
    assert "<img:0>" in view["body_markdown"]  # raw preserved
    assert view["used_refs"] == [0, 1]
    assert view["cover_image_ref"] == 2


def test_resolve_map_preserves_rag_origin_and_similarity_metadata():
    m = _good_map(n=2)
    m["images"][1].update(
        {
            "role": "rag",
            "origin_article_id": "origin-article",
            "retrieval_score": 0.812345,
        }
    )

    view = resolve_map(m, lambda iid: f"/images/{iid}")

    assert view["images"][1]["role"] == "rag"
    assert view["images"][1]["origin_article_id"] == "origin-article"
    assert view["images"][1]["retrieval_score"] == 0.812345


def test_resolve_map_drops_cover_missing_from_image_table():
    m = _good_map(n=2)
    m["cover_image_ref"] = 5
    view = resolve_map(m, lambda iid: f"/images/{iid}")
    assert view["cover_image_ref"] is None


def test_resolve_map_preserves_visualizations_and_chart_refs():
    m = _map_with_chart()
    view = resolve_map(m, lambda iid: f"/images/{iid}")
    assert view["visualizations"][0]["kind"] == "bar"
    assert view["used_chart_refs"] == [0]
    assert "<chart:0>" in view["body_resolved"]
