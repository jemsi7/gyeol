from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

from app.api import app, state
from app.config import Settings
from app.editorial_compiler import (
    MANUAL_PIPELINE_VERSION,
    compile_editorial_document,
    validate_editorial_document,
)
from app.editorial_models import EditorialDocument
from app.editorial_pipeline import compile_scene_body
from app.editorial_service import editor_token_hash


FIXTURE = Path(__file__).with_name("fixtures") / "editorial_document.json"
NOW = datetime(2026, 7, 19, 13, 0, tzinfo=UTC)
TOKEN = "c" * 43


def fixture_document() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def fixture_records() -> list[dict]:
    return [
        {"id": item["image_id"], "source_url": item["source_url"]}
        for item in fixture_document()["images"]
    ]


def chart_document(kind: str) -> dict:
    evidence = "설치 전 체류 시간은 8분, 설치 후 체류 시간은 13분이었다."
    mark = {
        "bar": "bar",
        "line": "line",
        "area": "area",
        "pie": "sector",
        "world_map": "region",
        "region_heatmap": "region",
        "matrix_heatmap": "cell",
    }.get(kind, "bar")
    series = [
        {
            "name": "평균 체류 시간",
            "mark": mark,
            "points": [
                {"label": "설치 전", "value": 8, "raw_value": "8분", "evidence": evidence},
                {"label": "설치 후", "value": 13, "raw_value": "13분", "evidence": evidence},
            ],
        }
    ]
    if kind == "mixed":
        series = [
            {"name": "설치 전", "mark": "bar", "points": [series[0]["points"][0]]},
            {"name": "설치 후", "mark": "line", "points": [series[0]["points"][1]]},
        ]
    if kind in {"world_map", "region_heatmap"}:
        for point, code in zip(series[0]["points"], ("KR", "JP"), strict=True):
            point["region_code"] = code
        geo_scope = "world" if kind == "world_map" else "asia"
    else:
        geo_scope = ""
    if kind == "matrix_heatmap":
        for point, (row, column) in zip(
            series[0]["points"], (("아시아", "현재"), ("유럽", "현재")), strict=True
        ):
            point["row"] = row
            point["column"] = column
    return {
        "format_version": 3,
        "source_name": "결 편집부",
        "language": "ko",
        "headline": "체류 시간으로 읽은 그늘",
        "subheadline": "",
        "summary": "설치 전후의 현장 기록을 비교했다.",
        "keywords": ["그늘", "체류"],
        "images": [],
        "cover_image_ref": None,
        "visualizations": [
            {
                "kind": kind,
                "title": "설치 전후 평균 체류 시간",
                "unit": "분",
                "alt_text": "설치 전 8분과 설치 후 13분을 비교한 차트",
                "geo_scope": geo_scope,
                "color_scale": (
                    {"scheme": "ocean", "mode": "continuous"}
                    if kind in {"world_map", "region_heatmap", "matrix_heatmap"}
                    else None
                ),
                "series": series,
            }
        ],
        "scenes": [
            {
                "role": "lead", "label": "시작", "title": "현장을 기록하다",
                "summary": "그늘 설치 전후의 이동과 체류를 살폈다.",
                "body": "조사는 같은 시간대와 같은 구간에서 진행됐다.",
            },
            {
                "role": "context", "label": "근거", "title": "두 시점의 체류 시간",
                "body": evidence,
            },
            {
                "role": "evidence", "label": "차트", "title": "기록을 한눈에 비교",
                "chart_ref": 0,
            },
            {
                "role": "close", "label": "다음", "title": "다음 조사로 이어지다",
                "body": "운영진은 같은 기준으로 다음 계절의 변화를 다시 확인한다.",
            },
        ],
    }


def test_golden_document_compiles_deterministically_and_passes_shared_gate():
    document = EditorialDocument.model_validate(fixture_document())
    first = validate_editorial_document(document, fixture_records())
    second = validate_editorial_document(document, reversed(fixture_records()))

    assert first.valid, first.errors
    assert first.map_data == second.map_data
    assert first.map_data["format_version"] == 3
    assert first.map_data["editorial"]["pipeline_version"] == MANUAL_PIPELINE_VERSION
    assert first.map_data["claim_ids"] == ["CL-001", "CL-002", "CL-003"]
    assert first.map_data["scenes"][1]["id"] == "S-02"
    assert first.map_data["images"][0]["role"] == "editorial"
    assert first.map_data["cover_image_ref"] == 1


def test_body_over_360_chars_passes_shared_gate():
    document = fixture_document()
    document["scenes"][1]["body"] = "가" * 361

    result = validate_editorial_document(document, fixture_records())

    assert result.valid, result.errors
    assert len(result.map_data["scenes"][1]["body"]) == 361


def test_manual_and_automatic_paths_share_exact_body_projection():
    compiled = compile_editorial_document(fixture_document(), fixture_records())
    assert compiled["body_markdown"] == compile_scene_body(compiled["scenes"])
    assert "<img:0>" in compiled["body_markdown"]
    assert "> 머물 수 있어야 걸을 수 있습니다." in compiled["body_markdown"]


@pytest.mark.parametrize(
    "kind",
    [
        "bar", "line", "area", "pie", "mixed",
        "world_map", "region_heatmap", "matrix_heatmap",
    ],
)
def test_all_supported_chart_kinds_compile_with_exact_visible_evidence(kind):
    result = validate_editorial_document(chart_document(kind), [])
    assert result.valid, result.errors
    assert result.map_data["visualizations"][0]["kind"] == kind
    assert result.map_data["body_markdown"].count("<chart:0>") == 1


def test_negative_gates_cover_storage_scene_media_and_evidence_contracts():
    too_short = fixture_document()
    too_short["scenes"] = too_short["scenes"][:2]
    assert any("scenes length" in error for error in validate_editorial_document(too_short, fixture_records()).errors)

    duplicate = fixture_document()
    duplicate["images"][1]["image_id"] = duplicate["images"][0]["image_id"]
    duplicate_result = validate_editorial_document(duplicate, fixture_records())
    assert any("duplicates" in error for error in duplicate_result.errors)

    absent = validate_editorial_document(fixture_document(), fixture_records()[:1])
    assert any("does not exist" in error for error in absent.errors)

    prose_chart = chart_document("bar")
    prose_chart["scenes"][2]["body"] = "차트 장면에 들어가면 안 되는 본문"
    assert any("chart scene" in error for error in validate_editorial_document(prose_chart, []).errors)

    unsupported_evidence = chart_document("line")
    unsupported_evidence["visualizations"][0]["series"][0]["points"][0]["evidence"] = "8분은 원문 밖 문장이다."
    errors = validate_editorial_document(unsupported_evidence, []).errors
    assert any("exact source substring" in error for error in errors)


class CompilerRepo:
    def __init__(self, document: dict):
        self.operator_id = uuid4()
        self.draft_id = uuid4()
        self.row = {
            "id": self.draft_id,
            "status": "draft",
            "document": document,
            "version": 1,
            "created_by": self.operator_id,
            "updated_by": self.operator_id,
            "created_at": NOW,
            "updated_at": NOW,
        }

    async def authenticate_editor_token(self, token_hash):
        if token_hash != editor_token_hash(TOKEN):
            return None
        return {
            "id": self.operator_id,
            "display_name": "편집자",
            "role": "editor",
            "created_at": NOW,
            "last_used_at": NOW,
        }

    async def get_editorial_draft(self, draft_id: UUID):
        return self.row if draft_id == self.draft_id else None

    async def editorial_image_records(self, image_ids: list[str]):
        by_id = {record["id"]: record for record in fixture_records()}
        return [by_id[value] for value in image_ids if value in by_id]


@pytest.mark.asyncio
async def test_validate_endpoint_returns_resolved_preview_and_invalid_is_200():
    valid_repo = CompilerRepo(fixture_document())
    state.settings = Settings(_env_file=None, API_TOKEN="master")
    state.repo = valid_repo
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        valid = await client.post(
            f"/v1/editor/drafts/{valid_repo.draft_id}/validate",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert valid.status_code == 200
    assert valid.json()["valid"] is True
    assert valid.json()["map"]["images"][0]["url"].endswith("/images/" + "a" * 64)

    invalid_document = deepcopy(fixture_document())
    invalid_document["headline"] = ""
    invalid_repo = CompilerRepo(invalid_document)
    state.repo = invalid_repo
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        invalid = await client.post(
            f"/v1/editor/drafts/{invalid_repo.draft_id}/validate",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert invalid.status_code == 200
    assert invalid.json()["valid"] is False
    assert any("headline" in error for error in invalid.json()["errors"])
