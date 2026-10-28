"""Deterministic compiler for operator-authored EditorialDocument snapshots."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .editorial_models import EditorialDocument
from .editorial_pipeline import compile_scene_body
from .mapping_schema import MAP_FORMAT_VERSION
from .validation import validate_map


MANUAL_PIPELINE_VERSION = "manual-editor-v1"
MAX_VALIDATION_ERRORS = 100


@dataclass(frozen=True, slots=True)
class EditorialValidationResult:
    valid: bool
    errors: list[str]
    map_data: dict


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _record_map(image_records: Mapping[str, Any] | Iterable[Any]) -> dict[str, dict]:
    if isinstance(image_records, Mapping):
        rows = image_records.values()
    else:
        rows = image_records
    mapped: dict[str, dict] = {}
    for raw in rows:
        row = dict(raw) if raw is not None else {}
        image_id = str(row.get("id") or row.get("image_id") or "")
        if image_id:
            mapped[image_id] = row
    return mapped


def _source_text(document: EditorialDocument) -> str:
    """Build the evidence corpus from copy a reader can actually see."""
    parts = [document.headline, document.subheadline, document.summary]
    for scene in document.scenes:
        parts.extend(
            [scene.label, scene.title, scene.summary, scene.body, scene.secondary, scene.quote]
        )
    return "\n".join(part.strip() for part in parts if part.strip())


def compile_editorial_document(
    document: EditorialDocument | dict,
    image_records: Mapping[str, Any] | Iterable[Any],
) -> dict:
    """Compile author-owned fields into a canonical Delivery Map v3."""
    if not isinstance(document, EditorialDocument):
        document = EditorialDocument.model_validate(document)
    snapshot = document.model_dump(mode="json")
    records = _record_map(image_records)

    images: list[dict] = []
    placements: list[dict] = []
    for ref, image in enumerate(document.images):
        record = records.get(image.image_id, {})
        source_url = image.source_url or str(record.get("source_url") or "")
        images.append(
            {
                "ref": ref,
                "image_id": image.image_id,
                "source_url": source_url,
                "role": "editorial",
            }
        )
        placements.append(
            {
                "ref": ref,
                "caption": image.caption,
                "reason": "operator-selected",
            }
        )

    scenes: list[dict] = []
    claim_ids: list[str] = []
    for index, scene in enumerate(document.scenes):
        scene_id = f"S-{index + 1:02d}"
        claim_id = f"CL-{index + 1:03d}"
        claim_ids.append(claim_id)
        scenes.append(
            {
                "id": scene_id,
                "role": scene.role,
                "label": scene.label,
                "title": scene.title,
                "summary": scene.summary,
                "body": scene.body,
                "secondary": scene.secondary,
                "quote": scene.quote,
                "image_ref": scene.image_ref,
                "chart_ref": scene.chart_ref,
                "claim_refs": [claim_id],
            }
        )

    visualizations = []
    for ref, chart in enumerate(document.visualizations):
        value = chart.model_dump(mode="json")
        visualizations.append({"ref": ref, **value})

    return {
        "format_version": MAP_FORMAT_VERSION,
        "language": document.language,
        "headline": document.headline,
        "subheadline": document.subheadline,
        "summary": document.summary,
        "keywords": document.keywords,
        "body_markdown": compile_scene_body(scenes),
        "image_placements": placements,
        "images": images,
        "cover_image_ref": document.cover_image_ref,
        "visualizations": visualizations,
        "claim_ids": claim_ids,
        "required_claim_refs": claim_ids,
        "selected_claim_refs": claim_ids,
        "scenes": scenes,
        "editorial": {
            "mode": "editorial_completeness",
            "pipeline_version": MANUAL_PIPELINE_VERSION,
            "source_hash": hashlib.sha256(_canonical_bytes(snapshot)).hexdigest(),
            "critic_verdict": "pass",
        },
    }


def validate_editorial_document(
    document: EditorialDocument | dict,
    image_records: Mapping[str, Any] | Iterable[Any],
) -> EditorialValidationResult:
    """Compile and apply storage-integrity plus shared Map v3 gates."""
    if not isinstance(document, EditorialDocument):
        document = EditorialDocument.model_validate(document)
    records = _record_map(image_records)
    compiled = compile_editorial_document(document, records)
    errors: list[str] = []

    seen: set[str] = set()
    for index, image in enumerate(document.images):
        if image.image_id in seen:
            errors.append(f"images[{index}].image_id duplicates an earlier image")
        seen.add(image.image_id)
        if image.image_id not in records:
            errors.append(f"images[{index}].image_id does not exist")

    result = validate_map(
        compiled,
        n_images=len(document.images),
        source_text=_source_text(document),
    )
    errors.extend(result.errors)
    errors = errors[:MAX_VALIDATION_ERRORS]
    return EditorialValidationResult(
        valid=not errors,
        errors=errors,
        map_data=compiled,
    )
