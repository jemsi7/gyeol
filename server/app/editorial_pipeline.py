"""Grounded, checkpointed, multi-stage article-shorts generation.

The pipeline separates evidence extraction, editorial selection, scene planning,
scene writing, and criticism. Only pure Python compiles the final Delivery Map.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, Callable, Final, Sequence

import httpx

from .config import Settings
from .feed import seoul_editorial_day
from .numeric_tagging import (
    TAGGING_SYSTEM_PROMPT,
    build_tagging_prompt,
    candidate_atoms,
    normalize_tagging_response,
    numeric_tagging_output_schema,
)
from .publisher_context import (
    build_publisher_context,
    build_researched_context,
    subject_assets_from_ledger,
)
from .publisher_research import (
    PublisherInterestResearcher,
    assets_key,
    should_research,
)
from .publishers import PublisherRegistry
from .editorial_prompts import (
    CRITIC_SYSTEM_PROMPT,
    EVIDENCE_CLAIM_SYSTEM_PROMPT,
    EVIDENCE_SELECTION_SYSTEM_PROMPT,
    PLANNING_SYSTEM_PROMPT,
    PROMPT_VERSION,
    REPAIR_SYSTEM_PROMPT,
    SCENE_SYSTEM_PROMPT,
    STAGE_PROMPT_VERSIONS,
    build_critic_prompt,
    build_evidence_claim_prompt,
    build_evidence_selection_prompt,
    build_planning_prompt,
    build_repair_prompt,
    build_scene_prompt,
)
from .cerebras import CerebrasTransientError
from .mapping_schema import MAP_FORMAT_VERSION
from .map_output_schemas import (
    critic_output_schema,
    evidence_claim_text_output_schema,
    evidence_selection_output_schema,
    planner_output_schema,
    scene_batch_output_schema,
)
from .openrouter import ImagePart, OpenRouterTransientError, StructuredOutputSpec
from .validation import (
    normalize_visualizations,
    validate_map,
    validate_visualizations,
)

log = logging.getLogger("gdelt.editorial")

ARTIFACT_VERSION = 2
DETERMINISTIC_CRITIC_VERSION = "deterministic-contract-v1"
CLAIM_ID_RE = re.compile(r"^CL-(\d{3})$")
SCENE_ID_RE = re.compile(r"^S-(\d{2})$")
DIRECTIVE_LIKE_RE = re.compile(r"<[a-z][a-z0-9_]*:[^<>]+>")
NUMERIC_ATOM_RE = re.compile(
    r"(?<![\d.,])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?![\d.,])"
)
ENGLISH_NUMBER_WORD_VALUES = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
    "eleven": "11",
    "twelve": "12",
    "thirteen": "13",
    "fourteen": "14",
    "fifteen": "15",
    "sixteen": "16",
    "seventeen": "17",
    "eighteen": "18",
    "nineteen": "19",
    "twenty": "20",
}
ENGLISH_NUMBER_WORD_RE = re.compile(
    r"\b(" + "|".join(ENGLISH_NUMBER_WORD_VALUES) + r")\b",
    re.IGNORECASE,
)
ENGLISH_MONTH_VALUES = {
    "January": "1",
    "February": "2",
    "March": "3",
    "April": "4",
    "May": "5",
    "June": "6",
    "July": "7",
    "August": "8",
    "September": "9",
    "October": "10",
    "November": "11",
    "December": "12",
}
ENGLISH_MONTH_RE = re.compile(
    r"\b(" + "|".join(ENGLISH_MONTH_VALUES) + r")\b"
)
SUMMARY_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。！？])\s+")
SOURCE_SENTENCE_END_RE = re.compile(r"[.!?。！？]+[\"'”’)]*(?=\s+|$)")
SOURCE_ABBREVIATION_RE = re.compile(
    r"(?i)(?:\b(?:mr|mrs|ms|dr|prof|sr|jr|st|vs|e\.g|i\.e)|(?:[a-z]\.){2,})\.$"
)
MAX_SOURCE_SENTENCES = 512
SCENE_ROLES = {"lead", "context", "evidence", "impact", "close"}
MAX_EVIDENCE_MEDIA = 3
MAX_SCENE_MEDIA = 6
CRITIC_CODES = {
    "grounding",
    "distortion",
    "omission",
    "duplication",
    "flow",
    "tone",
    "media",
}
CLAIM_KIND_ALIASES = {
    "qualifying": "caveat",
    "qualification": "caveat",
    "limitation": "caveat",
}


class PipelineStageError(RuntimeError):
    def __init__(
        self,
        stage: str,
        errors: list[str],
        *,
        error_code: str | None = None,
        retryable: bool = False,
    ):
        self.stage = stage
        self.errors = errors
        self.error_code = error_code or f"{stage.split(':', 1)[0]}_semantic_invalid"
        self.retryable = retryable
        super().__init__(f"{stage}: {'; '.join(errors[:8])}")


def _s(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _str_list(value: Any, *, limit: int = 100) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_s(item) for item in value if isinstance(item, str) and _s(item)][:limit]


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _claim_kind(value: Any) -> str:
    kind = _s(value).lower()
    return CLAIM_KIND_ALIASES.get(kind, kind)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def artifact_hash(stage: str, model: str, payload: Any) -> str:
    envelope = {
        "artifact_version": ARTIFACT_VERSION,
        "prompt_version": prompt_version_for_stage(stage),
        "stage": stage,
        "model": model,
        "payload": payload,
    }
    return hashlib.sha256(_canonical_bytes(envelope)).hexdigest()


def prompt_version_for_stage(stage: str) -> str:
    stage_kind = stage.split(":", 1)[0]
    return STAGE_PROMPT_VERSIONS.get(stage_kind, PROMPT_VERSION)


def build_validation_feedback_prompt(
    original_user_text: str,
    previous_output: dict,
    errors: list[str],
) -> str:
    """Request one schema-preserving correction for deterministic form errors."""
    feedback = {
        "validation_errors": errors[:8],
        "previous_output": previous_output,
    }
    return (
        f"{original_user_text}\n\n"
        "The previous JSON object failed deterministic validation. Correct that "
        "object using the feedback below. Return the complete corrected object in "
        "the exact original schema and nothing else. Do not omit valid fields, add "
        "commentary, or change grounded facts merely to silence an error.\n"
        f"VALIDATION_FEEDBACK={json.dumps(feedback, ensure_ascii=False, sort_keys=True)}"
    )


def balanced_scene_batches(items: list[dict], max_size: int) -> list[list[dict]]:
    """Split fresh plans into 2–3 scene calls without a trailing singleton.

    A singleton remains valid when checkpoint reuse leaves only one missing
    scene, or when an operator explicitly configures a maximum size of two.
    """
    if not items:
        return []
    batches: list[list[dict]] = []
    cursor = 0
    while cursor < len(items):
        remaining = len(items) - cursor
        size = 2 if max_size >= 3 and remaining == 4 else min(max_size, remaining)
        batches.append(items[cursor : cursor + size])
        cursor += size
    return batches


def _sentence_spans(
    text: str,
    *,
    absolute_start: int,
    paragraph_id: str,
    max_sentences: int = MAX_SOURCE_SENTENCES,
) -> list[dict]:
    """Split conservatively while preserving exact absolute source spans."""
    if not text or max_sentences < 1:
        return []
    boundaries: list[int] = []
    for match in SOURCE_SENTENCE_END_RE.finditer(text):
        prefix = text[: match.end()]
        if match.group().startswith(".") and SOURCE_ABBREVIATION_RE.search(prefix):
            continue
        boundaries.append(match.end())
    if not boundaries or len(boundaries) + 1 > max_sentences:
        boundaries = boundaries[: max(0, max_sentences - 1)]
    spans: list[dict] = []
    cursor = 0
    for boundary in [*boundaries, len(text)]:
        raw = text[cursor:boundary]
        stripped = raw.strip()
        if stripped:
            relative = raw.find(stripped)
            start = absolute_start + cursor + max(0, relative)
            end = start + len(stripped)
            spans.append(
                {
                    "id": f"{paragraph_id}:S-{len(spans) + 1:03d}",
                    "text": stripped,
                    "start": start,
                    "end": end,
                }
            )
        cursor = boundary
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
    return spans


def source_sentence_index(source: dict) -> dict[str, dict]:
    return {
        sentence["id"]: {
            **sentence,
            "paragraph_id": paragraph["id"],
        }
        for paragraph in source.get("paragraphs") or []
        if isinstance(paragraph, dict)
        for sentence in paragraph.get("sentences") or []
        if isinstance(sentence, dict)
        and isinstance(sentence.get("id"), str)
        and isinstance(sentence.get("text"), str)
    }


def build_source_packet(
    *,
    title: str | None,
    source_url: str,
    domain: str | None,
    body_text: str,
    images: list[dict],
    visible_image_refs: set[int],
    output_language: str = "auto",
    document_type: str | None = None,
    document_source_name: str | None = None,
) -> dict:
    body = (body_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    paragraphs: list[dict] = []
    pattern = re.compile(r"(?:\A|\n\s*\n)(.*?)(?=\n\s*\n|\Z)", re.DOTALL)
    for match in pattern.finditer(body):
        raw = match.group(1)
        text = raw.strip()
        if not text:
            continue
        relative = raw.find(text)
        start = match.start(1) + max(0, relative)
        end = start + len(text)
        paragraph_id = f"P-{len(paragraphs) + 1:03d}"
        paragraphs.append(
            {
                "id": paragraph_id,
                "text": text,
                "start": start,
                "end": end,
                "sentences": _sentence_spans(
                    text,
                    absolute_start=start,
                    paragraph_id=paragraph_id,
                ),
            }
        )

    normalized_images = []
    for image in images:
        ref = _optional_int(image.get("ref")) if isinstance(image, dict) else None
        if ref is None:
            continue
        normalized_images.append(
            {
                "ref": ref,
                "source_url": _s(image.get("source_url")),
                "role": _s(image.get("role")),
                "visible_to_model": ref in visible_image_refs,
            }
        )
    source_basis = {
        "title": _s(title),
        "source_url": _s(source_url),
        "domain": _s(domain),
        "output_language": _s(output_language) or "auto",
        "body_text": body,
        "images": normalized_images,
    }
    if _s(document_type):
        source_basis["document_type"] = _s(document_type)
    if _s(document_source_name):
        source_basis["document_source_name"] = _s(document_source_name)
    return {
        **source_basis,
        "source_hash": hashlib.sha256(_canonical_bytes(source_basis)).hexdigest(),
        "paragraphs": paragraphs,
        "quality": {
            "char_count": len(re.sub(r"\s+", "", body)),
            "paragraph_count": len(paragraphs),
        },
    }


def document_context_error(
    *,
    document_type: str | None,
    publisher_context: str,
) -> PipelineStageError | None:
    if document_type == "foundation_notice" and not _s(publisher_context):
        return PipelineStageError(
            "planner",
            ["foundation notice requires publisher context"],
            error_code="document_context_required",
            retryable=False,
        )
    return None


def validate_source_packet(packet: dict, *, min_chars: int) -> list[str]:
    errors: list[str] = []
    if not packet.get("source_url"):
        errors.append("source_url is empty")
    if not packet.get("output_language"):
        errors.append("output_language is empty")
    body = packet.get("body_text")
    if not isinstance(body, str) or not body.strip():
        errors.append("body_text is empty")
    count = (packet.get("quality") or {}).get("char_count")
    if not isinstance(count, int) or count < min_chars:
        errors.append(f"source has {count or 0} non-whitespace chars; requires {min_chars}")
    paragraphs = packet.get("paragraphs")
    if not isinstance(paragraphs, list) or not paragraphs:
        errors.append("source has no paragraphs")
        return errors
    expected_ids = [f"P-{index + 1:03d}" for index in range(len(paragraphs))]
    if [item.get("id") for item in paragraphs if isinstance(item, dict)] != expected_ids:
        errors.append("paragraph IDs must be contiguous")
    for index, paragraph in enumerate(paragraphs):
        if not isinstance(paragraph, dict):
            errors.append(f"paragraphs[{index}] is not an object")
            continue
        start, end, text = paragraph.get("start"), paragraph.get("end"), paragraph.get("text")
        if not isinstance(start, int) or not isinstance(end, int) or not isinstance(text, str):
            errors.append(f"paragraphs[{index}] has invalid span")
        elif body[start:end] != text:
            errors.append(f"paragraphs[{index}] span does not reproduce text")
        sentences = paragraph.get("sentences")
        if not isinstance(sentences, list) or not sentences:
            errors.append(f"paragraphs[{index}] has no sentences")
            continue
        expected_sentence_ids = [
            f"{paragraph.get('id')}:S-{sentence_index + 1:03d}"
            for sentence_index in range(len(sentences))
        ]
        if [
            sentence.get("id")
            for sentence in sentences
            if isinstance(sentence, dict)
        ] != expected_sentence_ids:
            errors.append(f"paragraphs[{index}] sentence IDs must be contiguous")
        for sentence_index, sentence in enumerate(sentences):
            if not isinstance(sentence, dict):
                errors.append(
                    f"paragraphs[{index}].sentences[{sentence_index}] is not an object"
                )
                continue
            sentence_start = sentence.get("start")
            sentence_end = sentence.get("end")
            sentence_text = sentence.get("text")
            if (
                not isinstance(sentence_start, int)
                or not isinstance(sentence_end, int)
                or not isinstance(sentence_text, str)
                or body[sentence_start:sentence_end] != sentence_text
                or (
                    isinstance(start, int)
                    and isinstance(end, int)
                    and not start <= sentence_start < sentence_end <= end
                )
            ):
                errors.append(
                    f"paragraphs[{index}].sentences[{sentence_index}] has invalid span"
                )
    hash_keys = [
        "title",
        "source_url",
        "domain",
        "output_language",
        "body_text",
        "images",
    ]
    for optional_key in ("document_type", "document_source_name"):
        if optional_key in packet:
            hash_keys.append(optional_key)
    expected_hash = hashlib.sha256(
        _canonical_bytes({key: packet.get(key) for key in hash_keys})
    ).hexdigest()
    if packet.get("source_hash") != expected_hash:
        errors.append("source_hash mismatch")
    return errors


def _recover_unique_single_substitution(quote: str, paragraph: str) -> str:
    """Recover only an unambiguous one-code-point transcription error.

    The returned value always comes directly from ``paragraph``. Numeric atoms
    must remain byte-for-byte identical so this helper cannot hide a changed
    quantity while repairing nearby prose.
    """
    if not quote or quote in paragraph or len(quote) > len(paragraph):
        return quote
    quote_numbers = tuple(match.group() for match in NUMERIC_ATOM_RE.finditer(quote))
    candidate: str | None = None
    width = len(quote)
    for start in range(len(paragraph) - width + 1):
        window = paragraph[start : start + width]
        if sum(left != right for left, right in zip(quote, window)) != 1:
            continue
        window_numbers = tuple(
            match.group() for match in NUMERIC_ATOM_RE.finditer(window)
        )
        if window_numbers != quote_numbers:
            continue
        if candidate is not None:
            return quote
        candidate = window
    return candidate if candidate is not None else quote


def _normalize_claim_evidence_spans(
    value: Any,
    *,
    sentence_by_id: dict[str, dict],
    paragraph_by_id: dict[str, str],
) -> list[dict]:
    evidence: list[dict] = []
    for span in value if isinstance(value, list) else []:
        span = span if isinstance(span, dict) else {}
        sentence_refs = _str_list(span.get("sentence_refs"), limit=3)
        if sentence_refs:
            seen_sentence_refs: set[str] = set()
            for sentence_ref in sentence_refs:
                if sentence_ref in seen_sentence_refs:
                    continue
                seen_sentence_refs.add(sentence_ref)
                sentence = sentence_by_id.get(sentence_ref)
                if sentence is None:
                    evidence.append(
                        {
                            "paragraph_id": f"unknown:{sentence_ref}",
                            "sentence_ref": sentence_ref,
                            "quote": "",
                        }
                    )
                    continue
                evidence.append(
                    {
                        "paragraph_id": sentence["paragraph_id"],
                        "sentence_ref": sentence_ref,
                        "quote": sentence["text"],
                    }
                )
            continue
        paragraph_id = _s(span.get("paragraph_id"))
        quote = _s(span.get("quote"))
        paragraph = paragraph_by_id.get(paragraph_id)
        if paragraph is not None:
            quote = _recover_unique_single_substitution(quote, paragraph)
        evidence.append({"paragraph_id": paragraph_id, "quote": quote})
    return evidence


def _selection_numeric_whitelist(evidence: list[dict]) -> list[str]:
    return sorted(
        {
            atom
            for span in evidence
            for atom in _numeric_grounding_atoms(str(span.get("quote") or ""))
        }
    )


def _selection_translated_month_whitelist(evidence: list[dict]) -> list[str]:
    return sorted(
        {
            ENGLISH_MONTH_VALUES[match.group(0)]
            for span in evidence
            for match in ENGLISH_MONTH_RE.finditer(str(span.get("quote") or ""))
        }
    )


def normalize_evidence_selection(raw: Any, source: dict) -> dict:
    """Project selected source refs before the writer receives claim prose work."""
    raw = raw if isinstance(raw, dict) else {}
    sentence_by_id = source_sentence_index(source)
    paragraph_by_id = {
        item.get("id"): item.get("text")
        for item in source.get("paragraphs", [])
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and isinstance(item.get("text"), str)
    }
    claims: list[dict] = []
    for item in raw.get("claims") if isinstance(raw.get("claims"), list) else []:
        item = item if isinstance(item, dict) else {}
        evidence = _normalize_claim_evidence_spans(
            item.get("evidence"),
            sentence_by_id=sentence_by_id,
            paragraph_by_id=paragraph_by_id,
        )
        claims.append(
            {
                "id": _s(item.get("id")),
                "kind": _claim_kind(item.get("kind")),
                "importance": _s(item.get("importance")),
                "evidence": evidence,
                "numeric_whitelist": _selection_numeric_whitelist(evidence),
                "translated_month_whitelist": (
                    _selection_translated_month_whitelist(evidence)
                ),
            }
        )
    media: list[dict] = []
    for item in raw.get("media") if isinstance(raw.get("media"), list) else []:
        item = item if isinstance(item, dict) else {}
        media.append(
            {
                "ref": _optional_int(item.get("ref")),
                "caption": _s(item.get("caption")),
                "reason": _s(item.get("reason")),
                "claim_refs": _str_list(item.get("claim_refs")),
            }
        )
    return {
        "language": _s(raw.get("language")),
        "claims": claims,
        "media": media,
        "visualizations": normalize_visualizations(
            raw.get("visualizations"),
            sentence_index=sentence_by_id,
        ),
    }


def validate_evidence_selection(selection: dict, source: dict) -> list[str]:
    """Reuse final-ledger contracts while selection owns no reader claim text."""
    candidate = deepcopy(selection)
    candidate["claims"] = [
        {**claim, "text": "selection metadata"}
        for claim in selection.get("claims") or []
        if isinstance(claim, dict)
    ]
    return validate_evidence_ledger(candidate, source)


def normalize_evidence_claim_ledger(
    raw: Any,
    selection: dict,
    source: dict,
) -> dict:
    """Merge writer-only text with server-owned selection metadata and quotes."""
    raw = raw if isinstance(raw, dict) else {}
    selected_claims = [
        claim for claim in selection.get("claims") or [] if isinstance(claim, dict)
    ]
    written_claims = raw.get("claims") if isinstance(raw.get("claims"), list) else []
    claims: list[dict] = []
    for index, item in enumerate(written_claims):
        item = item if isinstance(item, dict) else {}
        selected = selected_claims[index] if index < len(selected_claims) else {}
        claims.append(
            {
                "id": _s(item.get("id")),
                "kind": _s(selected.get("kind")),
                "importance": _s(selected.get("importance")),
                "text": _s(item.get("text")),
                "evidence": deepcopy(selected.get("evidence") or []),
            }
        )
    ledger = normalize_evidence_ledger(
        {
            "language": _s(raw.get("language")),
            "claims": claims,
            "media": deepcopy(selection.get("media") or []),
            "visualizations": deepcopy(selection.get("visualizations") or []),
        },
        source,
    )
    return project_claim_numeric_grounding(ledger, source)


def validate_evidence_claim_ledger(
    ledger: dict,
    selection: dict,
    source: dict,
) -> list[str]:
    errors = validate_evidence_ledger(ledger, source)
    expected_ids = [
        _s(claim.get("id"))
        for claim in selection.get("claims") or []
        if isinstance(claim, dict)
    ]
    actual_ids = [
        _s(claim.get("id"))
        for claim in ledger.get("claims") or []
        if isinstance(claim, dict)
    ]
    if actual_ids != expected_ids:
        errors.append("claim writer IDs must match selected claims in order")
    if _s(ledger.get("language")) != _s(selection.get("language")):
        errors.append("evidence claim language must match selected evidence language")
    return errors


def normalize_evidence_ledger(raw: Any, source: dict | None = None) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    sentence_by_id = source_sentence_index(source or {})
    paragraph_by_id = {
        item.get("id"): item.get("text")
        for item in (source or {}).get("paragraphs", [])
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and isinstance(item.get("text"), str)
    }
    claims: list[dict] = []
    for item in raw.get("claims") if isinstance(raw.get("claims"), list) else []:
        item = item if isinstance(item, dict) else {}
        evidence = []
        for span in item.get("evidence") if isinstance(item.get("evidence"), list) else []:
            span = span if isinstance(span, dict) else {}
            sentence_refs = _str_list(span.get("sentence_refs"), limit=3)
            if sentence_refs:
                seen_sentence_refs: set[str] = set()
                for sentence_ref in sentence_refs:
                    if sentence_ref in seen_sentence_refs:
                        continue
                    seen_sentence_refs.add(sentence_ref)
                    sentence = sentence_by_id.get(sentence_ref)
                    if sentence is None:
                        evidence.append(
                            {
                                "paragraph_id": f"unknown:{sentence_ref}",
                                "quote": "",
                            }
                        )
                        continue
                    evidence.append(
                        {
                            "paragraph_id": sentence["paragraph_id"],
                            "quote": sentence["text"],
                        }
                    )
                continue
            paragraph_id = _s(span.get("paragraph_id"))
            quote = _s(span.get("quote"))
            paragraph = paragraph_by_id.get(paragraph_id)
            if paragraph is not None:
                quote = _recover_unique_single_substitution(quote, paragraph)
            evidence.append(
                {"paragraph_id": paragraph_id, "quote": quote}
            )
        claims.append(
            {
                "id": _s(item.get("id")),
                "kind": _claim_kind(item.get("kind")),
                "importance": _s(item.get("importance")),
                "text": _s(item.get("text")),
                "evidence": evidence,
            }
        )
    media: list[dict] = []
    for item in raw.get("media") if isinstance(raw.get("media"), list) else []:
        item = item if isinstance(item, dict) else {}
        media.append(
            {
                "ref": _optional_int(item.get("ref")),
                "caption": _s(item.get("caption")),
                "reason": _s(item.get("reason")),
                "claim_refs": _str_list(item.get("claim_refs")),
            }
        )
    visualizations = normalize_visualizations(
        raw.get("visualizations"),
        sentence_index=sentence_by_id if source is not None else None,
    )
    if source is not None:
        claim_quotes = [
            span.get("quote") or ""
            for claim in claims
            for span in claim.get("evidence") or []
        ]
        grounded_visualizations: list[dict] = []
        for chart in visualizations:
            grounded_series: list[dict] = []
            for series in chart.get("series") or []:
                points = [
                    point
                    for point in series.get("points") or []
                    if any(
                        _evidence_spans_match(
                            quote,
                            point.get("evidence") or "",
                        )
                        for quote in claim_quotes
                    )
                ]
                if points:
                    grounded_series.append({**series, "points": points})
            if sum(len(series["points"]) for series in grounded_series) < 2:
                continue
            candidate = {
                **chart,
                "ref": len(grounded_visualizations),
                "series": grounded_series,
            }
            if not _chart_claim_refs(candidate, {"claims": claims}):
                continue
            grounded_visualizations.append(candidate)
        visualizations = grounded_visualizations
    return {
        "language": _s(raw.get("language")),
        "claims": claims,
        "media": media,
        "visualizations": visualizations,
    }


def project_claim_numeric_grounding(ledger: dict, source: dict) -> dict:
    """Replace auto-language numeric derivations with their exact evidence."""
    projected = deepcopy(ledger)
    if _s(source.get("output_language")).casefold() != "auto":
        return projected
    for claim in projected.get("claims") or []:
        if not isinstance(claim, dict):
            continue
        claim_atoms = _numeric_atoms(str(claim.get("text") or ""))
        evidence_atoms = {
            atom
            for span in claim.get("evidence") or []
            for atom in _numeric_grounding_atoms(str(span.get("quote") or ""))
        }
        if not claim_atoms - evidence_atoms:
            continue
        quotes = list(
            dict.fromkeys(
                str(span.get("quote") or "").strip()
                for span in claim.get("evidence") or []
                if str(span.get("quote") or "").strip()
            )
        )
        fallback = " ".join(quotes)
        if 1 <= len(fallback) <= 500:
            claim["text"] = fallback
    return projected


def validate_evidence_ledger(ledger: dict, source: dict) -> list[str]:
    errors: list[str] = []
    if not ledger.get("language"):
        errors.append("language is empty")
    requested_language = _s(source.get("output_language"))
    if (
        requested_language.casefold() != "auto"
        and re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]+)*", requested_language)
        and _s(ledger.get("language")).casefold() != requested_language.casefold()
    ):
        errors.append("evidence language must match requested output language")
    claims = ledger.get("claims") or []
    if not 1 <= len(claims) <= 30:
        errors.append(f"claims length must be 1..30, got {len(claims)}")
    paragraph_by_id = {item["id"]: item["text"] for item in source.get("paragraphs", [])}
    expected_ids = [f"CL-{index + 1:03d}" for index in range(len(claims))]
    if [claim.get("id") for claim in claims] != expected_ids:
        errors.append("claim IDs must be contiguous CL-001..")
    claim_ids = set(expected_ids)
    essential = 0
    for index, claim in enumerate(claims):
        path = f"claims[{index}]"
        if claim.get("kind") not in {"fact", "context", "quote", "number", "caveat"}:
            errors.append(f"{path}.kind unsupported")
        if claim.get("importance") not in {"essential", "supporting"}:
            errors.append(f"{path}.importance unsupported")
        elif claim["importance"] == "essential":
            essential += 1
        if claim.get("kind") == "caveat" and claim.get("importance") != "essential":
            errors.append(f"{path} caveat must be essential")
        if not claim.get("text") or len(claim["text"]) > 500:
            errors.append(f"{path}.text must be 1..500 chars")
        evidence = claim.get("evidence") or []
        if not 1 <= len(evidence) <= 3:
            errors.append(f"{path}.evidence length must be 1..3")
        for span_index, span in enumerate(evidence):
            paragraph = paragraph_by_id.get(span.get("paragraph_id"))
            quote = span.get("quote")
            if paragraph is None:
                errors.append(f"{path}.evidence[{span_index}] paragraph is unknown")
            elif not quote or quote not in paragraph:
                errors.append(f"{path}.evidence[{span_index}] quote is not an exact paragraph substring")
        claim_atoms = _numeric_atoms(str(claim.get("text") or ""))
        evidence_atoms = {
            atom
            for span in evidence
            for atom in _numeric_grounding_atoms(str(span.get("quote") or ""))
        }
        evidence_text = " ".join(
            str(span.get("quote") or "")
            for span in evidence
        )
        evidence_atoms.update(
            _translated_korean_month_atoms(
                str(claim.get("text") or ""),
                evidence_text,
            )
        )
        unsupported_atoms = sorted(claim_atoms - evidence_atoms)
        if unsupported_atoms:
            allowed_atoms = ", ".join(sorted(evidence_atoms)) or "none"
            errors.append(
                f"{path}.text numeric atoms absent from evidence: "
                + ", ".join(unsupported_atoms)
                + f"; allowed evidence numeric atoms: {allowed_atoms}; "
                "do not calculate a difference, rate, total, conversion, or "
                "rounded value; state the source comparison directly"
            )
    if essential == 0:
        errors.append("at least one essential claim is required")

    visible_refs = {
        image["ref"] for image in source.get("images", []) if image.get("visible_to_model")
    }
    seen_media: set[int] = set()
    if len(ledger.get("media") or []) > MAX_EVIDENCE_MEDIA:
        errors.append(f"media length exceeds {MAX_EVIDENCE_MEDIA}")
    for index, media in enumerate(ledger.get("media") or []):
        ref = media.get("ref")
        if ref not in visible_refs:
            errors.append(f"media[{index}].ref is not a model-visible image")
        if ref in seen_media:
            errors.append(f"media ref {ref} duplicated")
        if isinstance(ref, int):
            seen_media.add(ref)
        if not media.get("caption") or not media.get("reason"):
            errors.append(f"media[{index}] caption/reason must be non-empty")
        refs = media.get("claim_refs") or []
        if not refs or any(ref_id not in claim_ids for ref_id in refs):
            errors.append(f"media[{index}].claim_refs must be non-empty and valid")

    errors.extend(
        validate_visualizations(
            ledger.get("visualizations") or [], source_text=source.get("body_text")
        )
    )
    return errors


def normalize_editorial_brief(raw: Any) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    return {
        "language": _s(raw.get("language")),
        "headline": _s(raw.get("headline")),
        "subheadline": _s(raw.get("subheadline")),
        "summary": _s(raw.get("summary")),
        "keywords": _str_list(raw.get("keywords"), limit=12),
        "central_question": _s(raw.get("central_question")),
        "answer": _s(raw.get("answer")),
        "why_it_matters": _s(raw.get("why_it_matters")),
        "ordering_policy": _s(raw.get("ordering_policy")),
        "required_claim_refs": _str_list(raw.get("required_claim_refs")),
        "included_supporting_claim_refs": _str_list(raw.get("included_supporting_claim_refs")),
        "omitted_supporting_claim_refs": _str_list(raw.get("omitted_supporting_claim_refs")),
        "summary_claim_refs": _str_list(raw.get("summary_claim_refs")),
    }


def validate_editorial_brief(brief: dict, ledger: dict) -> list[str]:
    errors: list[str] = []
    for field in ("language", "headline", "summary", "central_question", "answer", "why_it_matters"):
        if not brief.get(field):
            errors.append(f"{field} is empty")
    if len(brief.get("headline", "")) > 120:
        errors.append("headline exceeds 120 chars")
    if len(brief.get("summary", "")) > 600:
        errors.append("summary exceeds 600 chars")
    summary_sentences = [
        part.strip()
        for part in SUMMARY_SENTENCE_SPLIT_RE.split(brief.get("summary", ""))
        if part.strip()
    ]
    if not 1 <= len(summary_sentences) <= 2:
        errors.append("summary must contain 1..2 sentences")
    if brief.get("language") != ledger.get("language"):
        errors.append("brief language must match evidence ledger language")
    if not 3 <= len(brief.get("keywords") or []) <= 8:
        errors.append("keywords length must be 3..8")
    if brief.get("ordering_policy") != "editorial_completeness":
        errors.append("ordering_policy must be editorial_completeness")

    essential = {claim["id"] for claim in ledger["claims"] if claim["importance"] == "essential"}
    supporting = {claim["id"] for claim in ledger["claims"] if claim["importance"] == "supporting"}
    required = brief.get("required_claim_refs") or []
    included = brief.get("included_supporting_claim_refs") or []
    omitted = brief.get("omitted_supporting_claim_refs") or []
    if len(required) != len(set(required)) or set(required) != essential:
        errors.append("required_claim_refs must equal all essential claims exactly once")
    if len(included) != len(set(included)) or len(omitted) != len(set(omitted)):
        errors.append("supporting claim refs must not contain duplicates")
    if set(included) & set(omitted) or set(included) | set(omitted) != supporting:
        errors.append("supporting claims must be partitioned into included or omitted")
    selected = essential | set(included)
    summary_refs = brief.get("summary_claim_refs") or []
    if not summary_refs or len(summary_refs) != len(set(summary_refs)) or not set(summary_refs) <= selected:
        errors.append("summary_claim_refs must be a non-empty unique subset of selected claims")
    if len(summary_refs) > 3:
        errors.append("summary_claim_refs length must be 1..3")
    return errors


def selected_claim_refs(brief: dict) -> list[str]:
    return brief["required_claim_refs"] + brief["included_supporting_claim_refs"]


def _evidence_spans_match(left: str, right: str) -> bool:
    return bool(left and right and (left in right or right in left))


def _visualization_reader_text(chart: dict) -> str:
    values: list[str] = [
        str(chart.get(field) or "")
        for field in ("title", "alt_text", "unit")
    ]
    for series in chart.get("series") or []:
        values.append(str(series.get("name") or ""))
        for point in series.get("points") or []:
            values.extend(
                str(point.get(field) or "") for field in ("label", "raw_value")
            )
    return " ".join(values)


def _claim_fully_represented_by_chart(claim: dict, chart: dict) -> bool:
    claim_atoms = _numeric_atoms(str(claim.get("text") or ""))
    if not claim_atoms:
        return False
    chart_atoms = _numeric_atoms(_visualization_reader_text(chart))
    if not claim_atoms <= chart_atoms:
        return False
    point_evidence = [
        str(point.get("evidence") or "")
        for series in chart.get("series") or []
        for point in series.get("points") or []
    ]
    return any(
        _evidence_spans_match(str(span.get("quote") or ""), evidence)
        for span in claim.get("evidence") or []
        for evidence in point_evidence
    )


def _chart_represented_claim_refs(ledger: dict) -> set[str]:
    return {
        claim_ref
        for chart in ledger.get("visualizations") or []
        for claim_ref in _chart_claim_refs(chart, ledger)
    }


def normalize_scene_plan(raw: Any) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    scenes: list[dict] = []
    for item in raw.get("scenes") if isinstance(raw.get("scenes"), list) else []:
        item = item if isinstance(item, dict) else {}
        target = _optional_int(item.get("target_chars"))
        scenes.append(
            {
                "id": _s(item.get("id")),
                "role": _s(item.get("role")),
                "label": _s(item.get("label")),
                "purpose": _s(item.get("purpose")),
                "title_hint": _s(item.get("title_hint")),
                "claim_refs": _str_list(item.get("claim_refs")),
                "image_ref": _optional_int(item.get("image_ref")),
                "chart_ref": _optional_int(item.get("chart_ref")),
                "target_chars": target,
                "transition": _s(item.get("transition")),
            }
        )
    return {"scenes": scenes}


def validate_scene_plan(
    plan: dict,
    brief: dict,
    ledger: dict,
    settings: Settings,
) -> list[str]:
    errors: list[str] = []
    scenes = plan.get("scenes") or []
    if settings.map_min_scenes > settings.map_max_scenes:
        errors.append("MAP_MIN_SCENES exceeds MAP_MAX_SCENES")
    if not settings.map_min_scenes <= len(scenes) <= settings.map_max_scenes:
        errors.append(
            f"scenes length must be {settings.map_min_scenes}..{settings.map_max_scenes}"
        )
    expected_ids = [f"S-{index + 1:02d}" for index in range(len(scenes))]
    if [scene.get("id") for scene in scenes] != expected_ids:
        errors.append("scene IDs must be contiguous S-01..")
    if scenes and scenes[0].get("role") != "lead":
        errors.append("first scene role must be lead")
    if scenes and scenes[-1].get("role") != "close":
        errors.append("last scene role must be close")
    if scenes and not set(brief.get("summary_claim_refs") or []) <= set(
        scenes[0].get("claim_refs") or []
    ):
        errors.append("summary claims must all be assigned to the lead scene")
    charted_summary_refs = set(
        brief.get("summary_claim_refs") or []
    ) & _chart_represented_claim_refs(ledger)
    if charted_summary_refs:
        errors.append("summary_claim_refs must not include chart-represented claims")

    selected = selected_claim_refs(brief)
    chart_owned_refs = {
        claim_ref
        for scene in scenes
        if scene.get("chart_ref") is not None
        for claim_ref in scene.get("claim_refs") or []
    }
    prose_claim_count = len(set(selected) - chart_owned_refs)
    prose_scene_count = sum(
        1 for scene in scenes if scene.get("chart_ref") is None
    )
    if (
        scenes
        and scenes[0].get("chart_ref") is None
        and prose_claim_count > prose_scene_count
    ):
        lead_refs = set(scenes[0].get("claim_refs") or [])
        summary_refs = set(brief.get("summary_claim_refs") or [])
        if not lead_refs - summary_refs:
            errors.append(
                "lead must reserve at least one non-summary claim for its body"
            )
    assigned: list[str] = []
    media_by_ref = {item["ref"]: item for item in ledger.get("media") or []}
    media_refs = set(media_by_ref)
    visualizations = ledger.get("visualizations") or []
    chart_refs = set(range(len(visualizations)))
    chart_by_ref = {chart["ref"]: chart for chart in visualizations}
    claim_by_id = {claim["id"]: claim for claim in ledger.get("claims") or []}
    used_images: list[int] = []
    used_charts: list[int] = []
    for index, scene in enumerate(scenes):
        path = f"scenes[{index}]"
        if scene.get("role") not in SCENE_ROLES:
            errors.append(f"{path}.role unsupported")
        for field in ("label", "purpose", "title_hint"):
            if not scene.get(field):
                errors.append(f"{path}.{field} is empty")
        if scene.get("role") != "close" and not scene.get("transition"):
            errors.append(f"{path}.transition is empty")
        refs = scene.get("claim_refs") or []
        if not refs:
            errors.append(f"{path}.claim_refs is empty")
        assigned.extend(refs)
        image_ref, chart_ref = scene.get("image_ref"), scene.get("chart_ref")
        if image_ref is not None and image_ref not in media_refs:
            errors.append(f"{path}.image_ref is not approved media")
        elif image_ref is not None and not (
            set(refs) & set(media_by_ref[image_ref].get("claim_refs") or [])
        ):
            errors.append(f"{path}.image_ref has no claim overlap")
        if chart_ref is not None and chart_ref not in chart_refs:
            errors.append(f"{path}.chart_ref is invalid")
        if image_ref is not None and chart_ref is not None:
            errors.append(f"{path} cannot contain image_ref and chart_ref")
        if image_ref is not None:
            used_images.append(image_ref)
        if chart_ref is not None:
            used_charts.append(chart_ref)
            chart = chart_by_ref.get(chart_ref)
            if chart is not None:
                covered_claim_refs = set(_chart_claim_refs(chart, ledger))
                point_evidence = [
                    (
                        series_index,
                        point_index,
                        point.get("evidence") or "",
                    )
                    for series_index, series in enumerate(chart.get("series") or [])
                    for point_index, point in enumerate(series.get("points") or [])
                ]
                claim_quotes = {
                    claim_ref: [
                        span.get("quote") or ""
                        for span in (claim_by_id.get(claim_ref) or {}).get("evidence") or []
                    ]
                    for claim_ref in refs
                }
                for claim_ref, quotes in claim_quotes.items():
                    if claim_ref not in covered_claim_refs:
                        errors.append(
                            f"{path}.claim_ref {claim_ref} is not represented by chart points"
                        )
                all_claim_quotes = [quote for quotes in claim_quotes.values() for quote in quotes]
                for series_index, point_index, evidence in point_evidence:
                    if not any(
                        _evidence_spans_match(quote, evidence) for quote in all_claim_quotes
                    ):
                        errors.append(
                            f"{path}.chart point {series_index}:{point_index} "
                            "is not represented by claim_refs"
                        )
        budget = scene.get("target_chars")
        minimum_budget = 0 if chart_ref is not None else 80
        if not isinstance(budget, int) or not minimum_budget <= budget <= 420:
            scene_kind = "chart" if chart_ref is not None else "non-chart"
            errors.append(
                f"{path}.{scene_kind} target_chars must be {minimum_budget}..420"
            )
    if Counter(assigned) != Counter(selected) or any(count != 1 for count in Counter(assigned).values()):
        errors.append("selected claims must be assigned exactly once")
    if len(used_images) != len(set(used_images)):
        errors.append("image refs may be assigned at most once")
    if sorted(used_charts) != sorted(chart_refs) or len(used_charts) != len(set(used_charts)):
        errors.append("every chart must be assigned exactly once")
    return errors


def normalize_planning(raw: Any) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    return {
        "brief": normalize_editorial_brief(raw.get("brief")),
        "plan": normalize_scene_plan({"scenes": raw.get("scenes")}),
    }


def normalize_planner_proposal(raw: Any) -> dict:
    """Project the minimal planner wire onto the server-owned skeleton input.

    Legacy combined planning objects remain readable so in-flight tests and
    diagnostics can be replayed, but provider-facing v2 schemas no longer let
    the model author IDs, roles, or media/chart topology.
    """
    raw = raw if isinstance(raw, dict) else {}
    if isinstance(raw.get("brief"), dict):
        return normalize_planning(raw)
    scene_proposals = (
        raw.get("scene_proposals")
        if isinstance(raw.get("scene_proposals"), list)
        else []
    )
    return {
        "brief": normalize_editorial_brief(
            {
                "language": raw.get("language"),
                "headline": raw.get("headline"),
                "subheadline": raw.get("subheadline"),
                "summary": raw.get("summary"),
                "keywords": raw.get("keywords"),
                "central_question": raw.get("central_question"),
                "answer": raw.get("answer"),
                "why_it_matters": raw.get("why_it_matters"),
                "ordering_policy": "editorial_completeness",
                "required_claim_refs": [],
                "included_supporting_claim_refs": raw.get(
                    "selected_supporting_claim_refs"
                ),
                "omitted_supporting_claim_refs": [],
                "summary_claim_refs": raw.get("summary_claim_refs"),
            }
        ),
        "plan": normalize_scene_plan({"scenes": scene_proposals}),
    }


def _unique_allowed(values: Any, allowed: set[str]) -> list[str]:
    result: list[str] = []
    for value in _str_list(values):
        if value in allowed and value not in result:
            result.append(value)
    return result


def _chart_claim_refs(chart: dict, ledger: dict) -> list[str]:
    return [
        claim.get("id")
        for claim in ledger.get("claims") or []
        if _claim_fully_represented_by_chart(claim, chart)
    ]


def _canonical_claim_selection(
    brief: dict,
    ledger: dict,
    *,
    min_selected: int,
) -> tuple[list[str], list[str], list[str]]:
    essential = [
        claim["id"]
        for claim in ledger.get("claims") or []
        if claim.get("importance") == "essential"
    ]
    supporting = [
        claim["id"]
        for claim in ledger.get("claims") or []
        if claim.get("importance") == "supporting"
    ]
    supporting_set = set(supporting)
    included = _unique_allowed(
        brief.get("included_supporting_claim_refs"),
        supporting_set,
    )
    chart_refs = {
        claim_ref
        for chart in ledger.get("visualizations") or []
        for claim_ref in _chart_claim_refs(chart, ledger)
    }
    for claim_ref in supporting:
        if claim_ref in chart_refs and claim_ref not in included:
            included.append(claim_ref)
    for claim_ref in supporting:
        if len(essential) + len(included) >= max(1, min_selected):
            break
        if claim_ref not in included:
            included.append(claim_ref)
    omitted = [claim_ref for claim_ref in supporting if claim_ref not in included]
    return essential, included, omitted


def _canonical_scene_plan(
    plan: dict,
    brief: dict,
    ledger: dict,
    settings: Settings,
) -> dict:
    selected = selected_claim_refs(brief)
    selected_set = set(selected)
    proposed = [
        scene
        for scene in (plan.get("scenes") or [])
        if isinstance(scene, dict)
    ]
    chart_count = len(ledger.get("visualizations") or [])
    desired = max(
        settings.map_min_scenes,
        min(len(proposed) or settings.map_min_scenes, settings.map_max_scenes),
        chart_count,
    )
    scene_count = min(desired, settings.map_max_scenes, len(selected))
    if scene_count < 1:
        return {"scenes": []}

    scenes: list[dict] = []
    for index in range(scene_count):
        original = proposed[index] if index < len(proposed) else {}
        if index == 0:
            role = "lead"
        elif index == scene_count - 1:
            role = "close"
        else:
            proposed_role = _s(original.get("role"))
            role = (
                proposed_role
                if proposed_role in {"context", "evidence", "impact"}
                else "evidence"
            )
        scenes.append(
            {
                "id": f"S-{index + 1:02d}",
                "role": role,
                "label": _s(original.get("label")) or role,
                "purpose": _s(original.get("purpose")) or "Present assigned evidence",
                "title_hint": _s(original.get("title_hint")) or "Assigned evidence",
                "claim_refs": [],
                "image_ref": _optional_int(original.get("image_ref")),
                "chart_ref": None,
                "target_chars": _optional_int(original.get("target_chars")),
                "transition": (
                    ""
                    if index == scene_count - 1
                    else _s(original.get("transition")) or "Continue to the next scene."
                ),
            }
        )

    chart_by_ref = {
        chart["ref"]: chart
        for chart in ledger.get("visualizations") or []
        if isinstance(chart.get("ref"), int)
    }
    assigned_chart_refs: set[int] = set()
    for index, original in enumerate(proposed[:scene_count]):
        chart_ref = _optional_int(original.get("chart_ref"))
        if chart_ref in chart_by_ref and chart_ref not in assigned_chart_refs:
            scenes[index]["chart_ref"] = chart_ref
            scenes[index]["image_ref"] = None
            assigned_chart_refs.add(chart_ref)
    chart_destinations = [
        *range(1, max(1, scene_count - 1)),
        scene_count - 1,
        0,
    ]
    for chart_ref in chart_by_ref:
        if chart_ref in assigned_chart_refs:
            continue
        destination = next(
            (
                index
                for index in chart_destinations
                if 0 <= index < scene_count and scenes[index]["chart_ref"] is None
            ),
            None,
        )
        if destination is None:
            break
        scenes[destination]["chart_ref"] = chart_ref
        scenes[destination]["image_ref"] = None
        assigned_chart_refs.add(chart_ref)

    assigned: set[str] = set()
    claim_by_id = {
        claim.get("id"): claim
        for claim in ledger.get("claims") or []
        if isinstance(claim, dict)
    }
    for scene in scenes:
        chart_ref = scene.get("chart_ref")
        if chart_ref is None:
            continue
        refs = [
            ref
            for ref in _chart_claim_refs(chart_by_ref[chart_ref], ledger)
            if ref in selected_set and ref not in assigned
        ]
        scene["claim_refs"].extend(refs)
        assigned.update(refs)

    for claim_ref in brief.get("summary_claim_refs") or []:
        if claim_ref in selected_set and claim_ref not in assigned:
            scenes[0]["claim_refs"].append(claim_ref)
            assigned.add(claim_ref)

    for index, original in enumerate(proposed[:scene_count]):
        for claim_ref in _unique_allowed(
            original.get("claim_refs"),
            selected_set,
        ):
            if claim_ref not in assigned and scenes[index].get("chart_ref") is None:
                scenes[index]["claim_refs"].append(claim_ref)
                assigned.add(claim_ref)

    prose_indices = [
        index for index, scene in enumerate(scenes) if scene.get("chart_ref") is None
    ]
    destinations = prose_indices or list(range(scene_count))
    for claim_ref in selected:
        if claim_ref in assigned:
            continue
        destination = min(
            destinations,
            key=lambda index: (len(scenes[index]["claim_refs"]), index),
        )
        scenes[destination]["claim_refs"].append(claim_ref)
        assigned.add(claim_ref)

    for index in prose_indices:
        if scenes[index]["claim_refs"]:
            continue
        donor = next(
            (
                donor_index
                for donor_index in sorted(
                    prose_indices,
                    key=lambda item: (-len(scenes[item]["claim_refs"]), item),
                )
                if len(scenes[donor_index]["claim_refs"]) > 1
                and not (
                    donor_index == 0
                    and scenes[donor_index]["claim_refs"][-1]
                    in set(brief.get("summary_claim_refs") or [])
                )
            ),
            None,
        )
        if donor is not None:
            scenes[index]["claim_refs"].append(scenes[donor]["claim_refs"].pop())

    summary_refs = set(brief.get("summary_claim_refs") or [])
    prose_claim_refs = {
        claim_ref
        for index in prose_indices
        for claim_ref in scenes[index].get("claim_refs") or []
    }
    if (
        0 in prose_indices
        and len(prose_claim_refs) > len(prose_indices)
        and not (set(scenes[0]["claim_refs"]) - summary_refs)
    ):
        donors = [
            index
            for index in prose_indices
            if index != 0 and len(scenes[index]["claim_refs"]) > 1
        ]
        if donors:
            donor = min(
                donors,
                key=lambda index: (
                    scenes[index].get("role") == "close",
                    -len(scenes[index]["claim_refs"]),
                    index,
                ),
            )
            candidate_index = min(
                range(len(scenes[donor]["claim_refs"])),
                key=lambda index: (
                    (claim_by_id.get(scenes[donor]["claim_refs"][index]) or {}).get(
                        "importance"
                    )
                    != "supporting",
                    (claim_by_id.get(scenes[donor]["claim_refs"][index]) or {}).get(
                        "kind"
                    )
                    == "caveat",
                    index,
                ),
            )
            scenes[0]["claim_refs"].append(
                scenes[donor]["claim_refs"].pop(candidate_index)
            )

    for scene in scenes:
        if scene.get("chart_ref") is not None:
            scene["image_ref"] = None
            scene["target_chars"] = max(
                0,
                min(scene.get("target_chars") or 0, 420),
            )
        else:
            target = scene.get("target_chars")
            normalized_target = (
                max(80, min(target, 420))
                if isinstance(target, int)
                else 240
            )
            assigned_text = " ".join(
                str(claim_by_id.get(ref, {}).get("text") or "").strip()
                for ref in scene.get("claim_refs") or []
                if str(claim_by_id.get(ref, {}).get("text") or "").strip()
            )
            scene["target_chars"] = max(
                normalized_target,
                min(420, len(assigned_text)),
            )
    return {"scenes": scenes}


def canonicalize_planning(
    raw: Any,
    ledger: dict,
    settings: Settings,
    *,
    cover_ref: int | None,
) -> dict:
    normalized = normalize_planner_proposal(raw)
    brief = normalized["brief"]
    required, included, omitted = _canonical_claim_selection(
        brief,
        ledger,
        min_selected=settings.map_min_scenes,
    )
    brief["language"] = ledger.get("language") or brief.get("language")
    brief["ordering_policy"] = "editorial_completeness"
    brief["required_claim_refs"] = required
    brief["included_supporting_claim_refs"] = included
    brief["omitted_supporting_claim_refs"] = omitted
    selected = set(required + included)
    charted = _chart_represented_claim_refs(ledger)
    summary_refs = [
        ref
        for ref in _unique_allowed(brief.get("summary_claim_refs"), selected)
        if ref not in charted
    ][:3]
    if not summary_refs:
        summary_refs = [ref for ref in required + included if ref not in charted][:1]
    brief["summary_claim_refs"] = summary_refs
    normalized["brief"] = brief
    normalized["plan"] = _canonical_scene_plan(
        normalized["plan"],
        brief,
        ledger,
        settings,
    )
    return enrich_planning_media(
        normalized,
        ledger,
        cover_ref=cover_ref,
    )


def _best_media_assignments(
    media: list[dict],
    scenes: list[dict],
    *,
    scene_indices: list[int],
    limit: int,
) -> list[tuple[int, int]]:
    """Return a bounded, deterministic maximum-cardinality media matching."""
    bounded_limit = max(0, min(int(limit), MAX_SCENE_MEDIA))
    if not media or not scene_indices or bounded_limit == 0:
        return []

    best_score: tuple | None = None
    best: list[tuple[int, int, int]] = []

    def consider(assignments: list[tuple[int, int, int]]) -> None:
        nonlocal best_score, best
        ordered = sorted(assignments)
        score = (
            len(ordered),
            sum(overlap for _, _, overlap in ordered),
            tuple(-media_index for media_index, _, _ in ordered),
            tuple(-scene_index for _, scene_index, _ in ordered),
        )
        if best_score is None or score > best_score:
            best_score = score
            best = ordered

    def search(
        media_index: int,
        used_scenes: set[int],
        assignments: list[tuple[int, int, int]],
    ) -> None:
        if media_index >= len(media) or len(assignments) >= bounded_limit:
            consider(assignments)
            return

        search(media_index + 1, used_scenes, assignments)
        media_claims = set(media[media_index].get("claim_refs") or [])
        for scene_index in scene_indices:
            if scene_index in used_scenes:
                continue
            overlap = len(
                media_claims & set(scenes[scene_index].get("claim_refs") or [])
            )
            if overlap == 0:
                continue
            used_scenes.add(scene_index)
            assignments.append((media_index, scene_index, overlap))
            search(media_index + 1, used_scenes, assignments)
            assignments.pop()
            used_scenes.remove(scene_index)

    search(0, set(), [])
    return [(media_index, scene_index) for media_index, scene_index, _ in best]


def enrich_planning_media(
    value: dict,
    ledger: dict,
    *,
    limit: int = MAX_SCENE_MEDIA,
    excluded_refs: set[int] | None = None,
    cover_ref: int | None = None,
    density_min_scenes: int = 3,
    density_target_views: int | None = None,
) -> dict:
    """Place approved media toward the literal 3/5 visible-image target.

    The evidence ledger remains the relevance boundary. Every valid 3..9 scene
    plan is deterministically rematched; an actual lead cover counts toward the
    target and constrained relevant media are not lost to a flexible choice.
    """
    enriched = deepcopy(value)
    scenes = (enriched.get("plan") or {}).get("scenes") or []
    bounded_limit = max(0, min(int(limit), MAX_SCENE_MEDIA))
    threshold = max(1, int(density_min_scenes))
    if density_target_views is None:
        target_views = (
            image_density_target(len(scenes))
            if 3 <= len(scenes) <= 9
            else min(len(scenes), MAX_SCENE_MEDIA)
        )
    else:
        target_views = max(0, min(int(density_target_views), MAX_SCENE_MEDIA))
    has_cover = isinstance(cover_ref, int) and not isinstance(cover_ref, bool)
    excluded = set(excluded_refs or set())
    if has_cover:
        excluded.add(cover_ref)
    for scene in scenes:
        if scene.get("image_ref") in excluded:
            scene["image_ref"] = None

    approved: list[dict] = []
    approved_refs: set[int] = set()
    for item in ledger.get("media") or []:
        ref = item.get("ref") if isinstance(item, dict) else None
        if (
            not isinstance(ref, int)
            or isinstance(ref, bool)
            or ref in excluded
            or ref in approved_refs
        ):
            continue
        approved.append(item)
        approved_refs.add(ref)

    if len(scenes) < threshold:
        approved = approved[:bounded_limit]
        approved_refs = {item["ref"] for item in approved}
        assigned: set[int] = set()
        for scene in scenes:
            ref = scene.get("image_ref")
            if (
                ref in approved_refs
                and scene.get("chart_ref") is None
                and ref not in assigned
            ):
                assigned.add(ref)

        for media in approved:
            ref = media["ref"]
            if ref in assigned or len(assigned) >= bounded_limit:
                continue
            media_claims = set(media.get("claim_refs") or [])
            candidates: list[tuple[int, int, dict]] = []
            for index, scene in enumerate(scenes):
                if (
                    scene.get("image_ref") is not None
                    or scene.get("chart_ref") is not None
                ):
                    continue
                overlap = len(media_claims & set(scene.get("claim_refs") or []))
                if overlap:
                    candidates.append((overlap, index, scene))
            if not candidates:
                continue
            _, _, destination = max(
                candidates, key=lambda item: (item[0], -item[1])
            )
            destination["image_ref"] = ref
            assigned.add(ref)
        return enriched

    # Planner media choices are hints. Recompute approved choices for every
    # valid plan so the visible cover is reserved and relevant assignment count
    # is maximal. Unknown refs remain untouched for strict validation to reject.
    for scene in scenes:
        if scene.get("image_ref") in approved_refs:
            scene["image_ref"] = None

    scene_media_limit = max(
        0,
        min(
            bounded_limit,
            target_views - (1 if has_cover else 0),
        ),
    )
    scene_indices = [
        index
        for index, scene in enumerate(scenes)
        if not (has_cover and index == 0)
        and scene.get("image_ref") is None
        and scene.get("chart_ref") is None
    ]
    for media_index, scene_index in _best_media_assignments(
        approved,
        scenes,
        scene_indices=scene_indices,
        limit=scene_media_limit,
    ):
        scenes[scene_index]["image_ref"] = approved[media_index]["ref"]
    return enriched


def image_density_target(scene_count: int) -> int:
    if isinstance(scene_count, bool) or not isinstance(scene_count, int):
        raise ValueError("scene_count must be an integer from 3 through 9")
    if not 3 <= scene_count <= 9:
        raise ValueError("scene_count must be from 3 through 9")
    return min((scene_count * 4 + 4) // 5, MAX_SCENE_MEDIA)


def visible_image_views(planning: dict, cover_ref: int | None) -> int:
    refs: set[int] = set()
    if isinstance(cover_ref, int) and not isinstance(cover_ref, bool):
        refs.add(cover_ref)
    plan = planning.get("plan") if isinstance(planning.get("plan"), dict) else planning
    for scene in plan.get("scenes") or []:
        ref = scene.get("image_ref") if isinstance(scene, dict) else None
        if isinstance(ref, int) and not isinstance(ref, bool):
            refs.add(ref)
    return len(refs)


def eligible_rag_scenes(planning: dict, cover_ref: int | None) -> list[dict]:
    has_cover = isinstance(cover_ref, int) and not isinstance(cover_ref, bool)
    plan = planning.get("plan") if isinstance(planning.get("plan"), dict) else planning
    eligible: list[dict] = []
    for index, scene in enumerate(plan.get("scenes") or []):
        if not isinstance(scene, dict):
            continue
        if has_cover and index == 0:
            continue
        if scene.get("chart_ref") is not None or scene.get("image_ref") is not None:
            continue
        if not scene.get("id") or not scene.get("claim_refs"):
            continue
        eligible.append(scene)
    return eligible


def build_rag_query(*, article_title: str, scene: dict, ledger: dict) -> str:
    claims = {
        claim.get("id"): str(claim.get("text") or "").strip()
        for claim in ledger.get("claims") or []
        if isinstance(claim, dict)
    }
    lines = [
        f"task: search result | query: {str(article_title).strip()}",
        "scene: "
        + " | ".join(
            value
            for value in (
                str(scene.get("id") or "").strip(),
                str(scene.get("role") or "").strip(),
                str(scene.get("title") or "").strip(),
            )
            if value
        ),
        "claims:",
    ]
    for claim_ref in scene.get("claim_refs") or []:
        text = claims.get(claim_ref)
        if not text:
            continue
        line = f"{claim_ref}: {text}"
        if len("\n".join([*lines, line])) > 6000:
            break
        lines.append(line)
    return "\n".join(lines).strip()


def validate_planning(value: dict, ledger: dict, settings: Settings) -> list[str]:
    brief = value.get("brief") or {}
    plan = value.get("plan") or {}
    errors = validate_editorial_brief(brief, ledger)
    if not errors:
        errors.extend(validate_scene_plan(plan, brief, ledger, settings))
    return errors


def normalize_scene_batch(raw: Any) -> list[dict]:
    raw = raw if isinstance(raw, dict) else {}
    scenes: list[dict] = []
    for item in raw.get("scenes") if isinstance(raw.get("scenes"), list) else []:
        item = item if isinstance(item, dict) else {}
        scenes.append(
            {
                "id": _s(item.get("scene_id")) or _s(item.get("id")),
                "role": _s(item.get("role")),
                "label": _s(item.get("label")),
                "title": _s(item.get("title")),
                "summary": _s(item.get("summary")),
                "body": _s(item.get("body")),
                "secondary": _s(item.get("secondary")),
                "quote": _s(item.get("quote")),
                "quote_source": _s(item.get("quote_source")),
                "quote_evidence_ref": _s(item.get("quote_evidence_ref")),
                "image_ref": _optional_int(item.get("image_ref")),
                "chart_ref": _optional_int(item.get("chart_ref")),
                "claim_refs": _str_list(item.get("claim_refs")),
            }
        )
    return scenes


def _resolve_quote_evidence_ref(
    ref: str,
    planned: dict,
    ledger: dict,
) -> str:
    """Resolve a "<claim id>/<position>" address to its exact evidence quote.

    Only claims assigned to the planned scene are addressable, so a quote can
    never borrow evidence the scene does not own. Anything unresolvable maps
    to "" and the scene validator then rejects the orphan quote (DR-13260).
    """
    ref = _s(ref)
    if not ref:
        return ""
    claim_id, separator, ordinal = ref.partition("/")
    if not separator or claim_id not in set(planned.get("claim_refs") or []):
        return ""
    claim = next(
        (
            item
            for item in ledger.get("claims") or []
            if isinstance(item, dict) and item.get("id") == claim_id
        ),
        None,
    )
    if claim is None:
        return ""
    try:
        position = int(ordinal)
    except ValueError:
        return ""
    evidence = claim.get("evidence") or []
    if not 1 <= position <= len(evidence):
        return ""
    span = evidence[position - 1]
    return _s(span.get("quote")) if isinstance(span, dict) else ""


def compile_scene_copies(
    raw: Any,
    plan_slice: list[dict],
    *,
    translated: bool = False,
    ledger: dict | None = None,
) -> list[dict]:
    """Compile prose-only worker output onto immutable scene blueprints.

    When the output language matches the source, ``quote_source`` mirrors
    ``quote``: the existing domains then satisfy the source-span invariant by
    construction and their stored Maps are unchanged. In translated mode the
    writer only names a claim-evidence address and the server fills
    ``quote_source`` with that exact span (DR-13260).
    """
    if translated and ledger is None:
        raise ValueError("translated scene compilation requires the evidence ledger")
    candidates = normalize_scene_batch(raw)
    used: set[int] = set()
    overlaid: list[dict] = []
    for planned in plan_slice:
        candidate_index = next(
            (
                index
                for index, candidate in enumerate(candidates)
                if index not in used and candidate.get("id") == planned.get("id")
            ),
            None,
        )
        if candidate_index is None:
            candidate_index = next(
                (index for index in range(len(candidates)) if index not in used),
                None,
            )
        if candidate_index is None:
            candidate: dict = {
                "label": "",
                "title": "",
                "summary": "",
                "body": "",
                "secondary": "",
                "quote": "",
                "quote_source": "",
            }
        else:
            used.add(candidate_index)
            candidate = dict(candidates[candidate_index])
        candidate.update(
            {
                "id": planned.get("id"),
                "role": planned.get("role"),
                "label": planned.get("label"),
                "image_ref": planned.get("image_ref"),
                "chart_ref": planned.get("chart_ref"),
                "claim_refs": list(planned.get("claim_refs") or []),
            }
        )
        if planned.get("role") != "lead":
            candidate["summary"] = ""
        if planned.get("chart_ref") is not None:
            candidate["body"] = ""
            candidate["secondary"] = ""
            candidate["quote"] = ""
            candidate["quote_source"] = ""
            candidate["quote_evidence_ref"] = ""
            candidate["image_ref"] = None
        elif translated:
            candidate["quote_source"] = _resolve_quote_evidence_ref(
                candidate.get("quote_evidence_ref", ""),
                planned,
                ledger or {},
            )
        else:
            # The untranslated lanes keep their pre-DR-13260 artifact shape.
            candidate.pop("quote_evidence_ref", None)
            candidate["quote_source"] = candidate.get("quote", "")
        overlaid.append(candidate)
    return overlaid


def overlay_scene_invariants(
    raw: Any,
    plan_slice: list[dict],
    *,
    translated: bool = False,
    ledger: dict | None = None,
) -> list[dict]:
    """Compatibility alias for callers migrating to prose-only SceneCopy."""
    return compile_scene_copies(
        raw, plan_slice, translated=translated, ledger=ledger
    )


def _claim_evidence_texts(ledger: dict, refs: list[str]) -> list[str]:
    wanted = set(refs)
    return [
        span["quote"]
        for claim in ledger["claims"]
        if claim["id"] in wanted
        for span in claim["evidence"]
    ]


def _numeric_atoms(text: str) -> set[str]:
    return {
        match.group().replace(",", "")
        for match in NUMERIC_ATOM_RE.finditer(text)
    }


def _numeric_grounding_atoms(text: str) -> set[str]:
    atoms = _numeric_atoms(text)
    atoms.update(
        ENGLISH_NUMBER_WORD_VALUES[match.group(0).casefold()]
        for match in ENGLISH_NUMBER_WORD_RE.finditer(text)
    )
    return atoms


def _translated_korean_month_atoms(claim_text: str, evidence_text: str) -> set[str]:
    """Allow only source Titlecase month names rendered as Korean ``n월``."""
    source_months = {
        ENGLISH_MONTH_VALUES[match.group(0)]
        for match in ENGLISH_MONTH_RE.finditer(evidence_text)
    }
    return {
        month
        for month in source_months
        if re.search(rf"(?<!\d){re.escape(month)}\s*월", claim_text)
    }


def project_scene_numeric_grounding(
    scenes: list[dict],
    plan_slice: list[dict],
    ledger: dict,
) -> list[dict]:
    """Project evidence-grounded claim prose when a selected digit is missing."""
    projected = deepcopy(scenes)
    planned_by_id = {
        scene.get("id"): scene
        for scene in plan_slice
        if isinstance(scene, dict)
    }
    claim_by_id = {
        claim.get("id"): claim
        for claim in ledger.get("claims") or []
        if isinstance(claim, dict)
    }
    for scene in projected:
        planned = planned_by_id.get(scene.get("id")) or {}
        if planned.get("chart_ref") is not None:
            continue
        reader_text = " ".join(
            str(scene.get(field) or "")
            for field in ("title", "summary", "body", "secondary", "quote")
        )
        reader_atoms = _numeric_atoms(reader_text)
        missing_claims: list[dict] = []
        for claim_ref in planned.get("claim_refs") or []:
            claim = claim_by_id.get(claim_ref) or {}
            claim_atoms = _numeric_atoms(str(claim.get("text") or ""))
            evidence_atoms = {
                atom
                for span in claim.get("evidence") or []
                for atom in _numeric_grounding_atoms(
                    str(span.get("quote") or "")
                )
            }
            if (
                claim_atoms
                and claim_atoms <= evidence_atoms
                and claim_atoms - reader_atoms
            ):
                missing_claims.append(claim)
        fallback_parts = list(
            dict.fromkeys(
                str(claim.get("text") or "").strip()
                for claim in missing_claims
                if str(claim.get("text") or "").strip()
            )
        )
        if not fallback_parts:
            continue
        addition = " ".join(fallback_parts)
        current_secondary = str(scene.get("secondary") or "").strip()
        candidate_secondary = " ".join(
            part for part in (current_secondary, addition) if part
        )
        body = str(scene.get("body") or "")
        quote = str(scene.get("quote") or "")
        budget = planned.get("target_chars")
        budget = budget if isinstance(budget, int) else 420
        combined = len(body) + len(candidate_secondary) + len(quote)
        if len(candidate_secondary) <= 300 and combined <= min(budget, 520):
            scene["secondary"] = candidate_secondary
            continue
        assigned_parts = list(
            dict.fromkeys(
                str((claim_by_id.get(ref) or {}).get("text") or "").strip()
                for ref in planned.get("claim_refs") or []
                if str((claim_by_id.get(ref) or {}).get("text") or "").strip()
            )
        )
        grounded_body = " ".join(assigned_parts)
        if grounded_body and len(grounded_body) <= budget:
            scene["body"] = grounded_body
            scene["secondary"] = ""
            scene["quote"] = ""
    return projected


def _chart_reader_text(ledger: dict, chart_ref: int) -> str:
    chart = next(
        (
            item
            for item in ledger.get("visualizations") or []
            if item.get("ref") == chart_ref
        ),
        None,
    )
    if chart is None:
        return ""
    return _visualization_reader_text(chart)


# A translation may render prose freely but must not invent or drop the parts
# of a quote that carry its meaning. These three checks are deterministic on
# purpose: a model grading its own translation would turn the copyright
# control into a declaration.
ACRONYM_RE: Final = re.compile(r"\b[A-Z]{2,6}\b")
QUOTE_TRANSLATION_LENGTH_SLACK: Final[int] = 20
QUOTE_TRANSLATION_LENGTH_RATIO: Final[float] = 1.6


def scene_output_is_translated(source: dict) -> bool:
    """True when the scenes are written in a language the source is not in.

    Keyed on the requested output language rather than the coverage domain, so
    any future lane that pins a language gets the same treatment without a
    second rule to keep in sync.
    """
    return _s(source.get("output_language")).strip().casefold() not in {"", "auto"}


# English scale words and the Korean myriad groupings a faithful rendering uses
# instead. "$48 billion" becomes "480억 달러", so the digits legitimately change
# and a naive atom comparison would reject every correct translation.
ENGLISH_SCALE_VALUES: Final[dict[str, int]] = {
    "thousand": 10**3,
    "million": 10**6,
    "billion": 10**9,
    "trillion": 10**12,
}
KOREAN_MYRIAD_UNITS: Final[tuple[int, ...]] = (10**4, 10**8, 10**12)
SCALED_NUMBER_RE: Final = re.compile(
    r"(\d+(?:\.\d+)?)\s*(" + "|".join(ENGLISH_SCALE_VALUES) + r")\b",
    re.IGNORECASE,
)


def _translated_scale_atoms(source_text: str) -> set[str]:
    """Korean myriad renderings of the source's scaled numbers.

    Only exact regroupings are allowed: 48 billion may appear as 480억 because
    that is the same quantity, but no rounding or unit invention is admitted.
    """
    atoms: set[str] = set()
    for match in SCALED_NUMBER_RE.finditer(source_text):
        value = float(match.group(1)) * ENGLISH_SCALE_VALUES[match.group(2).casefold()]
        for unit in KOREAN_MYRIAD_UNITS:
            scaled = value / unit
            if scaled >= 1 and scaled == int(scaled):
                atoms.add(str(int(scaled)))
    return atoms


def quote_translation_errors(quote: str, quote_source: str) -> list[str]:
    """Deterministic gates a translated quote must clear against its source."""
    errors: list[str] = []
    allowed = _numeric_grounding_atoms(quote_source)
    allowed |= _translated_scale_atoms(quote_source)
    invented = _numeric_atoms(quote) - allowed
    if invented:
        errors.append(
            "quote introduces numeric atoms absent from quote_source: "
            + ", ".join(sorted(invented))
        )
    # Only all-caps acronyms, not every capitalised token: sentence-initial
    # words like "The" are not names, and requiring them would fail every
    # honest translation.
    dropped = sorted(
        {
            token
            for token in ACRONYM_RE.findall(quote_source)
            if token not in quote
        }
    )
    if dropped:
        errors.append(
            "quote drops source acronyms: " + ", ".join(dropped)
        )
    if len(quote) > len(quote_source) * QUOTE_TRANSLATION_LENGTH_RATIO + (
        QUOTE_TRANSLATION_LENGTH_SLACK
    ):
        errors.append("quote is too long to be a translation of quote_source")
    return errors


def validate_scene_batch(
    scenes: list[dict],
    plan_slice: list[dict],
    *,
    ledger: dict,
    translated: bool = False,
) -> list[str]:
    errors: list[str] = []
    claim_by_id = {
        claim.get("id"): claim for claim in ledger.get("claims") or []
    }
    returned_ids = [scene.get("id") for scene in scenes]
    expected_ids = [scene.get("id") for scene in plan_slice]
    if returned_ids != expected_ids:
        errors.append(
            "scene batch IDs/order must match the plan slice: "
            f"expected={expected_ids!r} got={returned_ids!r}"
        )
        return errors
    for index, (scene, planned) in enumerate(zip(scenes, plan_slice)):
        path = f"scenes[{index}]"
        for field in ("id", "role", "image_ref", "chart_ref", "claim_refs"):
            if scene.get(field) != planned.get(field):
                errors.append(f"{path}.{field} must match plan")
        if not scene.get("label") or not scene.get("title"):
            errors.append(f"{path} label/title must be non-empty")
        if len(scene.get("label", "")) > 24 or len(scene.get("title", "")) > 90:
            errors.append(f"{path} label/title exceeds limit")
        if scene.get("role") == "lead" and not scene.get("summary"):
            errors.append(f"{path} lead summary is required")
        if scene.get("role") != "lead" and scene.get("summary"):
            errors.append(f"{path} non-lead summary must be empty")
        for field, limit in (("summary", 320), ("secondary", 300), ("quote", 240)):
            if len(scene.get(field, "")) > limit:
                errors.append(f"{path}.{field} exceeds {limit} chars")
        text_fields = [scene.get(field, "") for field in ("title", "summary", "body", "secondary", "quote")]
        if any(DIRECTIVE_LIKE_RE.search(text) for text in text_fields):
            errors.append(f"{path} contains directive-like text")
        chart_ref = scene.get("chart_ref")
        if chart_ref is not None:
            if any(scene.get(field) for field in ("body", "secondary", "quote")):
                errors.append(f"{path} chart scene must not contain prose blocks")
            if scene.get("image_ref") is not None:
                errors.append(f"{path} chart scene must not contain an image")
        elif not scene.get("body"):
            errors.append(f"{path}.body is required for non-chart scene")
        reader_text = " ".join(
            str(scene.get(field) or "")
            for field in ("title", "summary", "body", "secondary", "quote")
        )
        if chart_ref is not None:
            reader_text += " " + _chart_reader_text(ledger, chart_ref)
            chart = next(
                (
                    item
                    for item in ledger.get("visualizations") or []
                    if item.get("ref") == chart_ref
                ),
                None,
            )
            covered_claim_refs = (
                set(_chart_claim_refs(chart, ledger))
                if chart is not None
                else set()
            )
            for claim_ref in scene.get("claim_refs") or []:
                if claim_ref not in covered_claim_refs:
                    errors.append(
                        f"{path}.claim_ref {claim_ref} is not fully represented by chart"
                    )
        reader_atoms = _numeric_atoms(reader_text)
        for claim_ref in scene.get("claim_refs") or []:
            claim_atoms = _numeric_atoms(
                str((claim_by_id.get(claim_ref) or {}).get("text") or "")
            )
            missing_atoms = sorted(claim_atoms - reader_atoms)
            if missing_atoms:
                errors.append(
                    f"{path}.claim_ref {claim_ref} omits numeric atoms: "
                    + ", ".join(missing_atoms)
                )
        combined = sum(len(scene.get(field, "")) for field in ("body", "secondary", "quote"))
        budget = planned.get("target_chars")
        if isinstance(budget, int) and combined > budget:
            errors.append(f"{path} prose exceeds planned target_chars")
        if scene.get("image_ref") is not None and combined > 300:
            errors.append(f"{path} image scene prose exceeds 300 chars")
        if scene.get("image_ref") is None and combined > 520:
            errors.append(f"{path} prose exceeds 520 chars")
        quote = scene.get("quote") or ""
        quote_source = scene.get("quote_source") or ""
        if quote and not quote_source:
            if translated:
                errors.append(
                    f"{path}.quote requires quote_evidence_ref naming one "
                    "assigned claim evidence span"
                )
            else:
                errors.append(
                    f"{path}.quote has no quote_source to verify against"
                )
        if quote_source and not any(
            quote_source in evidence
            for evidence in _claim_evidence_texts(ledger, scene["claim_refs"])
        ):
            errors.append(
                f"{path}.quote_source is not an exact claim evidence substring"
            )
        if translated and quote and quote_source:
            errors.extend(
                f"{path}.{message}"
                for message in quote_translation_errors(quote, quote_source)
            )
    return errors


def normalize_critic_report(raw: Any) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    issues: list[dict] = []
    for item in raw.get("issues") if isinstance(raw.get("issues"), list) else []:
        item = item if isinstance(item, dict) else {}
        issues.append(
            {
                "scene_id": _s(item.get("scene_id")),
                "code": _s(item.get("code")),
                "message": _s(item.get("message")),
                "claim_refs": _str_list(item.get("claim_refs")),
                "repair_instruction": _s(item.get("repair_instruction")),
            }
        )
    return {"verdict": _s(raw.get("verdict")), "issues": issues}


def canonicalize_critic_report(raw: Any, plan: dict) -> dict:
    """Project issue claim refs onto the known target scene ownership."""
    report = normalize_critic_report(raw)
    plan_by_id = {
        scene.get("id"): scene
        for scene in plan.get("scenes") or []
        if isinstance(scene, dict)
    }
    for issue in report["issues"]:
        planned = plan_by_id.get(issue.get("scene_id"))
        proposed = issue.get("claim_refs") or []
        if planned is None or not proposed:
            continue
        owned = list(
            dict.fromkeys(
                ref
                for ref in planned.get("claim_refs") or []
                if isinstance(ref, str) and ref
            )
        )
        proposed_set = set(proposed)
        intersection = [ref for ref in owned if ref in proposed_set]
        issue["claim_refs"] = intersection or owned
    return report


def validate_critic_report(
    report: dict,
    *,
    scene_ids: set[str],
    claim_ids: set[str],
    plan: dict | None = None,
) -> list[str]:
    errors: list[str] = []
    verdict = report.get("verdict")
    issues = report.get("issues") or []
    if verdict not in {"pass", "revise"}:
        errors.append("critic verdict must be pass or revise")
    if verdict == "pass" and issues:
        errors.append("pass critic must have no issues")
    if verdict == "revise" and not issues:
        errors.append("revise critic must have issues")
    plan_by_id = {
        scene.get("id"): scene for scene in (plan or {}).get("scenes") or []
    }
    for index, issue in enumerate(issues):
        scene_id = issue.get("scene_id")
        if scene_id not in scene_ids:
            errors.append(f"issues[{index}].scene_id is unknown")
        if issue.get("code") not in CRITIC_CODES:
            errors.append(f"issues[{index}].code unsupported")
        if not issue.get("message") or not issue.get("repair_instruction"):
            errors.append(f"issues[{index}] message/repair_instruction required")
        refs = issue.get("claim_refs") or []
        if any(ref not in claim_ids for ref in refs):
            errors.append(f"issues[{index}].claim_refs contains unknown claim")
        planned = plan_by_id.get(scene_id)
        if planned is not None:
            owned_refs = set(planned.get("claim_refs") or [])
            if any(ref not in owned_refs for ref in refs):
                errors.append(
                    f"issues[{index}].claim_refs must belong to the target scene"
                )
            if planned.get("chart_ref") is not None and issue.get("code") == "omission":
                errors.append(
                    f"issues[{index}] chart claim omission is structurally invalid"
                )
    return errors


def compile_scene_body(scenes: list[dict]) -> str:
    """Project canonical scenes into the legacy Markdown/directive stream.

    Both the automatic pipeline and the operator-authored path call this one
    function so the feed never has two subtly different rendering grammars.
    """
    blocks: list[str] = []
    for scene in scenes:
        if scene.get("image_ref") is not None:
            blocks.append(f"<img:{scene['image_ref']}>")
        for field in ("body", "secondary"):
            if scene.get(field):
                blocks.append(scene[field])
        if scene.get("quote"):
            blocks.append(f"> {scene['quote']}")
        if scene.get("chart_ref") is not None:
            blocks.append(f"<chart:{scene['chart_ref']}>")
    return "\n\n".join(blocks)


# Kept for older offline imports while new code uses the public name above.
_legacy_body = compile_scene_body


def select_cover_image_ref(source: dict, ledger: dict) -> int | None:
    """Choose an editorial cover independently from claim media placement."""
    for image in source.get("images") or []:
        ref = image.get("ref") if isinstance(image, dict) else None
        if (
            isinstance(ref, int)
            and not isinstance(ref, bool)
            and image.get("visible_to_model") is True
            and str(image.get("role") or "").strip().casefold() == "social"
        ):
            return ref
    for media in ledger.get("media") or []:
        if not isinstance(media, dict):
            continue
        if str(media.get("role") or "").strip().casefold() == "rag":
            continue
        ref = media.get("ref")
        if isinstance(ref, int) and not isinstance(ref, bool):
            return ref
    return None


def compile_delivery_map(
    *,
    source: dict,
    ledger: dict,
    brief: dict,
    scenes: list[dict],
    images: list[dict],
    critic: dict,
    publisher_context: str = "",
    publisher_context_basis: Sequence[str] = (),
    tagged_atoms: Sequence[dict] = (),
) -> dict:
    used_images = {scene["image_ref"] for scene in scenes if scene.get("image_ref") is not None}
    media_by_ref = {item["ref"]: item for item in ledger.get("media") or []}
    placements = [
        {
            "ref": ref,
            "caption": media_by_ref[ref]["caption"],
            "reason": media_by_ref[ref]["reason"],
        }
        for ref in sorted(used_images)
        if ref in media_by_ref
    ]
    return {
        "format_version": MAP_FORMAT_VERSION,
        "language": brief["language"],
        "headline": brief["headline"],
        "subheadline": brief["subheadline"],
        "summary": brief["summary"],
        "keywords": brief["keywords"],
        "body_markdown": compile_scene_body(scenes),
        "image_placements": placements,
        "images": images,
        "cover_image_ref": select_cover_image_ref(source, ledger),
        "visualizations": ledger["visualizations"],
        "claim_ids": [claim["id"] for claim in ledger["claims"]],
        "required_claim_refs": brief["required_claim_refs"],
        "selected_claim_refs": selected_claim_refs(brief),
        # quote_source is a verification input, not reader copy. It has already
        # done its work by this point, and the scene-stage pipeline artifact
        # keeps it for audit, so the delivered Map -- and therefore the app --
        # never carries the untranslated source span.
        "scenes": [
            {
                key: value
                for key, value in scene.items()
                if key not in ("quote_source", "quote_evidence_ref")
            }
            for scene in scenes
        ],
        # A factual line about who issued this article, or "" (DR-13203). The
        # basis codes travel with the Map for audit but are stripped before the
        # app sees it -- see app_service -- because they are operator data.
        "publisher_context": publisher_context,
        "publisher_context_basis": list(publisher_context_basis),
        # Numbers whose subject the evidence settles, for the read-time
        # staleness comparison (C-13203). Empty is the normal outcome.
        "tagged_atoms": list(tagged_atoms),
        "editorial": {
            "mode": "editorial_completeness",
            "pipeline_version": PROMPT_VERSION,
            "source_hash": source["source_hash"],
            "critic_verdict": critic["verdict"],
        },
    }


class EditorialPipeline:
    def __init__(
        self,
        settings: Settings,
        repo: Any,
        llm: Any,
        image_rag: Any | None = None,
        model_identity: str | None = None,
    ):
        self.s = settings
        self.repo = repo
        self.llm = llm
        self.image_rag = image_rag
        self.model_identity = (
            model_identity
            or getattr(llm, "model_identity", "")
            or f"openrouter:{settings.openrouter_model}"
        )
        # Loaded once per pipeline: the registry is a small static file and a
        # per-article read would turn a config lookup into IO in the hot path.
        self.publisher_registry = PublisherRegistry.load(settings.publishers_file)
        # Built only where research can actually run, so an off deployment
        # carries no researcher at all (C-13212, DR-13248).
        self.publisher_researcher = (
            PublisherInterestResearcher(
                llm,
                fetch_timeout_s=settings.publisher_research_fetch_timeout_s,
                max_search_results=settings.publisher_research_max_search_results,
                max_search_uses=settings.publisher_research_max_search_uses,
            )
            if settings.publisher_research_mode != "off"
            else None
        )
        self.structured_outputs_enabled = bool(
            getattr(
                llm,
                "structured_outputs_enabled",
                settings.openrouter_structured_outputs_enabled,
            )
        )

    async def _event(
        self,
        article_id: str,
        *,
        stage: str,
        outcome: str,
        error_code: str | None = None,
        error_detail: str | None = None,
        retryable: bool = False,
        correction_index: int | None = None,
        metadata: dict | None = None,
    ) -> None:
        append = getattr(self.repo, "append_map_attempt_event", None)
        if append is None:
            return
        try:
            await append(
                article_id,
                stage=stage,
                outcome=outcome,
                error_code=error_code,
                error_detail=error_detail,
                retryable=retryable,
                correction_index=correction_index,
                model=self.model_identity,
                structured_output=self.structured_outputs_enabled,
                metadata=metadata,
            )
        except Exception:  # noqa: BLE001 - observability must not break mapping
            log.warning("Map attempt event insert failed code=map_event_write_failed")

    async def _save(
        self,
        article_id: str,
        *,
        stage: str,
        input_hash: str,
        status: str,
        data: dict | None,
        error: str | None,
        model: str | None = None,
    ) -> None:
        await self.repo.upsert_pipeline_artifact(
            article_id,
            stage=stage,
            input_hash=input_hash,
            artifact_version=ARTIFACT_VERSION,
            model=model or self.model_identity,
            status=status,
            data=data,
            error=error,
        )

    async def _cached(self, article_id: str, stage: str, input_hash: str) -> dict | None:
        artifact = await self.repo.get_pipeline_artifact(article_id, stage, input_hash)
        if artifact and artifact.get("status") == "ok" and isinstance(artifact.get("data"), dict):
            return artifact["data"]
        return None

    async def _contract_critic(
        self,
        article_id: str,
        *,
        ledger: dict,
        plan: dict,
        scenes: list[dict],
        translated: bool = False,
    ) -> dict:
        """Fail closed on render-breaking contracts without judging prose."""
        input_payload = {
            "ledger": ledger,
            "plan": plan,
            "scenes": scenes,
        }
        if translated:
            input_payload["translated"] = True
        input_hash = artifact_hash(
            "critic_contract",
            DETERMINISTIC_CRITIC_VERSION,
            input_payload,
        )
        cached = await self._cached(article_id, "critic", input_hash)
        if (
            cached is not None
            and cached.get("verdict") == "pass"
            and cached.get("mode") == "deterministic_contract"
        ):
            await self._event(
                article_id,
                stage="critic",
                outcome="cached",
                metadata={"mode": "deterministic_contract"},
            )
            return cached

        await self._event(
            article_id,
            stage="critic",
            outcome="started",
            metadata={"mode": "deterministic_contract"},
        )
        errors = validate_scene_batch(
            scenes, plan["scenes"], ledger=ledger, translated=translated
        )
        if errors:
            await self._save(
                article_id,
                stage="critic",
                input_hash=input_hash,
                status="failed",
                data=None,
                error="; ".join(errors[:8]),
                model=DETERMINISTIC_CRITIC_VERSION,
            )
            await self._event(
                article_id,
                stage="critic",
                outcome="failed",
                error_code="critic_contract_invalid",
                error_detail="; ".join(errors[:8]),
                retryable=False,
                metadata={"mode": "deterministic_contract"},
            )
            raise PipelineStageError(
                "critic",
                errors,
                error_code="critic_contract_invalid",
                retryable=False,
            )

        report = {
            "verdict": "pass",
            "issues": [],
            "mode": "deterministic_contract",
        }
        await self._save(
            article_id,
            stage="critic",
            input_hash=input_hash,
            status="ok",
            data=report,
            error=None,
            model=DETERMINISTIC_CRITIC_VERSION,
        )
        await self._event(
            article_id,
            stage="critic",
            outcome="succeeded",
            metadata={"mode": "deterministic_contract"},
        )
        return report

    async def _stage_call(
        self,
        article_id: str,
        *,
        stage: str,
        input_payload: Any,
        system: str,
        user_text: str,
        normalize: Callable[[Any], dict],
        validate: Callable[[dict], list[str]],
        images: list[ImagePart] | None = None,
        validation_corrections: int = 0,
        output_schema: StructuredOutputSpec | None = None,
    ) -> dict:
        if validation_corrections not in {0, 1, 2}:
            raise ValueError("validation_corrections must be 0, 1, or 2")
        input_hash = artifact_hash(stage, self.model_identity, input_payload)
        cached = await self._cached(article_id, stage, input_hash)
        if cached is not None:
            await self._event(
                article_id,
                stage=stage,
                outcome="cached",
            )
            return cached
        current_user_text = user_text
        for correction_index in range(validation_corrections + 1):
            await self._event(
                article_id,
                stage=stage,
                outcome="started",
                correction_index=correction_index,
                metadata={
                    "schema": output_schema.name if output_schema is not None else None,
                },
            )
            try:
                raw = await self.llm.complete_json(
                    system,
                    current_user_text,
                    images,
                    output_schema=output_schema,
                )
            except Exception as exc:  # noqa: BLE001
                message = f"LLM error: {exc}"
                retryable = isinstance(
                    exc,
                    (
                        httpx.TransportError,
                        OpenRouterTransientError,
                        CerebrasTransientError,
                    ),
                )
                error_code = (
                    "provider_transient"
                    if retryable
                    else "provider_request_failed"
                )
                await self._save(
                    article_id,
                    stage=stage,
                    input_hash=input_hash,
                    status="failed",
                    data=None,
                    error=message,
                )
                await self._event(
                    article_id,
                    stage=stage,
                    outcome="failed",
                    error_code=error_code,
                    error_detail=message,
                    retryable=retryable,
                    correction_index=correction_index,
                )
                raise PipelineStageError(
                    stage,
                    [message],
                    error_code=error_code,
                    retryable=retryable,
                ) from exc
            canonical = normalize(raw)
            errors = validate(canonical)
            if not errors:
                await self._save(
                    article_id,
                    stage=stage,
                    input_hash=input_hash,
                    status="ok",
                    data=canonical,
                    error=None,
                )
                await self._event(
                    article_id,
                    stage=stage,
                    outcome="succeeded",
                    correction_index=correction_index,
                )
                return canonical
            await self._save(
                article_id,
                stage=stage,
                input_hash=input_hash,
                status="failed",
                data=canonical,
                error="; ".join(errors[:8]),
            )
            error_code = f"{stage.split(':', 1)[0]}_semantic_invalid"
            await self._event(
                article_id,
                stage=stage,
                outcome="failed",
                error_code=error_code,
                error_detail="; ".join(errors[:8]),
                retryable=False,
                correction_index=correction_index,
            )
            if correction_index >= validation_corrections:
                raise PipelineStageError(
                    stage,
                    errors,
                    error_code=error_code,
                    retryable=False,
                )
            current_user_text = build_validation_feedback_prompt(
                user_text,
                canonical,
                errors,
            )
        raise AssertionError("unreachable validation correction state")

    def _scene_input(
        self,
        brief: dict,
        ledger: dict,
        planned: dict,
        *,
        translated: bool = False,
    ) -> dict:
        refs = set(planned["claim_refs"])
        payload = {
            "brief": brief,
            "plan": planned,
            "claims": [claim for claim in ledger["claims"] if claim["id"] in refs],
            "media": [item for item in ledger["media"] if item["ref"] == planned.get("image_ref")],
            "visualizations": [
                item for item in ledger["visualizations"] if item["ref"] == planned.get("chart_ref")
            ],
        }
        if translated:
            payload["translated"] = True
        return payload

    async def _research_publisher_context(
        self,
        article_id: str,
        *,
        domain: str,
        title: str,
        subject_assets: frozenset[str],
    ) -> Any | None:
        """A possibility sentence for an unregistered publisher, or nothing.

        Fails open in every direction, and fails *closed* on cost: a spend
        ledger we cannot read means we do not spend (DR-13247). ``None`` is by
        far the most common outcome and is not an error -- the gate alone rules
        out ordinary reporting, and the researcher rules out everything it
        could not check.
        """
        researcher = self.publisher_researcher
        if researcher is None or not domain:
            return None
        required = (
            "read_triage_signals",
            "read_publisher_interest",
            "write_publisher_interest",
            "count_publisher_research_runs",
        )
        if any(getattr(self.repo, name, None) is None for name in required):
            return None
        try:
            signals = await self.repo.read_triage_signals(article_id)
        except Exception:  # noqa: BLE001 - enrichment must not fail a Map
            return None
        record = self.publisher_registry.lookup(domain)
        if not should_research(
            has_factual_sentence=False,
            registered_type=record.publisher_type if record is not None else None,
            subject_assets=subject_assets,
            triage_signals=signals,
        ):
            return None

        key = assets_key(subject_assets)
        try:
            cached = await self.repo.read_publisher_interest(
                domain, key, max_age_days=self.s.publisher_research_cache_days
            )
        except Exception:  # noqa: BLE001
            cached = None
        if cached is not None:
            await self._event(
                article_id, stage="publisher_research", outcome="cached"
            )
            return self._displayable_context(
                relation=str(cached.get("relation") or ""),
                owner=str(cached.get("owner") or ""),
                asset=str(cached.get("asset") or ""),
                confidence=str(cached.get("confidence") or ""),
                evidence_count=len(cached.get("evidence") or ()),
            )

        try:
            spent = await self.repo.count_publisher_research_runs(
                seoul_editorial_day()
            )
        except Exception:  # noqa: BLE001 - unknown spend is treated as spent
            return None
        if spent >= self.s.publisher_research_daily_limit:
            return None

        await self._event(article_id, stage="publisher_research", outcome="started")
        try:
            async with asyncio.timeout(self.s.publisher_research_timeout_s):
                finding = await researcher.research(
                    domain=domain, title=title, subject_assets=subject_assets
                )
        except Exception as exc:  # noqa: BLE001 - includes TimeoutError
            await self._event(
                article_id,
                stage="publisher_research",
                outcome="failed",
                error_code="research_unavailable",
                error_detail=type(exc).__name__,
                retryable=True,
            )
            return None
        if finding is None:
            await self._event(
                article_id,
                stage="publisher_research",
                outcome="failed",
                error_code="research_inconclusive",
                retryable=True,
            )
            return None

        context = self._displayable_context(
            relation=finding.relation,
            owner=finding.owner,
            asset=finding.asset,
            confidence=finding.confidence,
            evidence_count=len(finding.evidence),
        )
        try:
            await self.repo.write_publisher_interest(
                {
                    "domain": domain,
                    "assets_key": key,
                    "relation": finding.relation,
                    "owner": finding.owner,
                    "asset": finding.asset,
                    "confidence": finding.confidence,
                    # Stored whatever the mode: shadow keeps the receipt, and
                    # an operator reviewing the promotion queue reads the exact
                    # sentence a reader would have seen.
                    "sentence": (
                        build_researched_context(
                            relation=finding.relation,
                            owner=finding.owner,
                            asset=finding.asset,
                            confidence=finding.confidence,
                            evidence_count=len(finding.evidence),
                        ).sentence
                    ),
                    "evidence": [item.to_dict() for item in finding.evidence],
                    "article_id": article_id,
                    "editorial_day": seoul_editorial_day(),
                }
            )
        except Exception:  # noqa: BLE001 - a lost receipt is not a lost Map
            log.warning("publisher interest write failed code=interest_write_failed")
        await self._event(
            article_id,
            stage="publisher_research",
            outcome="succeeded",
            metadata={
                "relation": finding.relation,
                "confidence": finding.confidence,
                "evidence": len(finding.evidence),
                "displayed": context is not None,
            },
        )
        return context

    def _displayable_context(
        self,
        *,
        relation: str,
        owner: str,
        asset: str,
        confidence: str,
        evidence_count: int,
    ) -> Any | None:
        """The sentence, but only in ``on`` mode. Shadow stores and shows nothing."""
        if self.s.publisher_research_mode != "on":
            return None
        context = build_researched_context(
            relation=relation,
            owner=owner,
            asset=asset,
            confidence=confidence,
            evidence_count=evidence_count,
        )
        return None if context.is_empty else context

    async def _tag_numeric_atoms(
        self,
        article_id: str,
        *,
        ledger: dict,
        as_of: datetime | None,
    ) -> list[dict]:
        """Label the numbers whose subject the evidence settles.

        Fails open in every direction. The tags are an optional enrichment on
        top of a Map that is already complete and valid, so a provider error,
        a malformed reply or an unconvinced model all end the same way: no
        tags, and an article that still publishes.
        """
        candidates = candidate_atoms(ledger)
        if not candidates:
            return []
        stamp = as_of or datetime.now(UTC)
        try:
            raw = await self.llm.complete_json(
                TAGGING_SYSTEM_PROMPT,
                build_tagging_prompt(candidates),
                output_schema=numeric_tagging_output_schema(candidates),
            )
        except Exception as exc:  # noqa: BLE001 - enrichment must not fail a Map
            await self._event(
                article_id,
                stage="numeric_tags",
                outcome="failed",
                error_code="tagging_unavailable",
                error_detail=type(exc).__name__,
                retryable=True,
            )
            return []
        tags = normalize_tagging_response(
            raw,
            candidates,
            as_of=stamp,
            min_confidence=self.s.numeric_tag_min_confidence,
        )
        await self._event(
            article_id,
            stage="numeric_tags",
            outcome="succeeded",
            metadata={
                "candidates": len(candidates),
                "tagged": len(tags),
            },
        )
        return [item.to_dict() for item in tags]

    async def _compose_scenes(
        self,
        article_id: str,
        *,
        brief: dict,
        ledger: dict,
        plan: dict,
        translated: bool = False,
    ) -> list[dict]:
        planned_scenes = plan["scenes"]
        by_id: dict[str, dict] = {}
        hashes: dict[str, str] = {}
        missing: list[dict] = []
        for planned in planned_scenes:
            stage = f"scene:{planned['id']}"
            input_payload = self._scene_input(
                brief,
                ledger,
                planned,
                translated=translated,
            )
            input_hash = artifact_hash(stage, self.model_identity, input_payload)
            hashes[planned["id"]] = input_hash
            cached = await self._cached(article_id, stage, input_hash)
            if cached is None:
                missing.append(planned)
            else:
                by_id[planned["id"]] = cached
                await self._event(
                    article_id,
                    stage=stage,
                    outcome="cached",
                )

        batch_tasks = [
            asyncio.create_task(
                self._compose_scene_batch(
                    article_id,
                    brief=brief,
                    ledger=ledger,
                    initial_plan_slice=initial_plan_slice,
                    hashes=hashes,
                    translated=translated,
                ),
                name=f"scene-batch-{index + 1}",
            )
            for index, initial_plan_slice in enumerate(
                balanced_scene_batches(missing, self.s.map_scene_batch_size)
            )
        ]
        if batch_tasks:
            batch_results = await asyncio.gather(
                *batch_tasks,
                return_exceptions=True,
            )
            for batch_result in batch_results:
                if isinstance(batch_result, BaseException):
                    raise batch_result
                by_id.update(batch_result)
        return [by_id[item["id"]] for item in planned_scenes]

    async def _compose_scene_batch(
        self,
        article_id: str,
        *,
        brief: dict,
        ledger: dict,
        initial_plan_slice: list[dict],
        hashes: dict[str, str],
        translated: bool = False,
    ) -> dict[str, dict]:
        """Write one independent scene transport batch and return valid copies.

        `_compose_scenes` launches every cache-miss batch at once. This helper
        intentionally owns only its slice so a failed sibling cannot discard
        already checkpointed work from another batch.
        """
        completed: dict[str, dict] = {}
        pending = list(initial_plan_slice)
        previous_candidates: dict[str, dict] = {}
        for correction_index in range(3):
            base_user_text = build_scene_prompt(
                brief, ledger, pending, translated=translated
            )
            if correction_index == 0:
                current_user_text = base_user_text
            else:
                current_user_text = build_validation_feedback_prompt(
                    base_user_text,
                    {
                        "scenes": [
                            previous_candidates[item["id"]]
                            for item in pending
                            if item["id"] in previous_candidates
                        ]
                    },
                    pending_errors,
                )
            output_schema = scene_batch_output_schema(
                pending, translated=translated, ledger=ledger
            )
            await self._event(
                article_id,
                stage="scenes",
                outcome="started",
                correction_index=correction_index,
                metadata={
                    "batch_size": len(pending),
                    "schema": output_schema.name,
                },
            )
            try:
                raw = await self.llm.complete_json(
                    SCENE_SYSTEM_PROMPT,
                    current_user_text,
                    output_schema=output_schema,
                )
            except Exception as exc:  # noqa: BLE001
                errors = [f"LLM error: {exc}"]
                retryable = isinstance(
                    exc,
                    (
                        httpx.TransportError,
                        OpenRouterTransientError,
                        CerebrasTransientError,
                    ),
                )
                for planned in pending:
                    await self._save(
                        article_id,
                        stage=f"scene:{planned['id']}",
                        input_hash=hashes[planned["id"]],
                        status="failed",
                        data=previous_candidates.get(planned["id"]),
                        error=errors[0],
                    )
                await self._event(
                    article_id,
                    stage="scenes",
                    outcome="failed",
                    error_code=(
                        "provider_transient"
                        if retryable
                        else "provider_request_failed"
                    ),
                    error_detail=errors[0],
                    retryable=retryable,
                    correction_index=correction_index,
                )
                raise PipelineStageError(
                    "scenes",
                    errors,
                    error_code=(
                        "provider_transient"
                        if retryable
                        else "provider_request_failed"
                    ),
                    retryable=retryable,
                ) from exc
            scenes = project_scene_numeric_grounding(
                compile_scene_copies(
                    raw, pending, translated=translated, ledger=ledger
                ),
                pending,
                ledger,
            )
            candidates = {
                scene["id"]: scene
                for scene in scenes
                if isinstance(scene, dict) and scene.get("id")
            }
            next_pending: list[dict] = []
            pending_errors: list[str] = []
            for planned, scene in zip(pending, scenes):
                scene_errors = validate_scene_batch(
                    [scene],
                    [planned],
                    ledger=ledger,
                    translated=translated,
                )
                if scene_errors:
                    next_pending.append(planned)
                    pending_errors.extend(
                        f"{planned['id']}: {error}" for error in scene_errors
                    )
                    await self._save(
                        article_id,
                        stage=f"scene:{planned['id']}",
                        input_hash=hashes[planned["id"]],
                        status="failed",
                        data=scene,
                        error="; ".join(scene_errors[:8]),
                    )
                    continue
                completed[scene["id"]] = scene
                await self._save(
                    article_id,
                    stage=f"scene:{scene['id']}",
                    input_hash=hashes[scene["id"]],
                    status="ok",
                    data=scene,
                    error=None,
                )
                await self._event(
                    article_id,
                    stage=f"scene:{scene['id']}",
                    outcome="succeeded",
                    correction_index=correction_index,
                )
            if not next_pending:
                return completed
            await self._event(
                article_id,
                stage="scenes",
                outcome="failed",
                error_code="scenes_semantic_invalid",
                error_detail="; ".join(pending_errors[:8]),
                correction_index=correction_index,
            )
            if correction_index == 2:
                raise PipelineStageError("scenes", pending_errors)
            previous_candidates = candidates
            pending = next_pending
        raise AssertionError("unreachable scene correction state")

    async def _repair(
        self,
        article_id: str,
        *,
        brief: dict,
        ledger: dict,
        plan: dict,
        scenes: list[dict],
        issues: list[dict],
        translated: bool = False,
    ) -> list[dict]:
        by_id = {scene["id"]: scene for scene in scenes}
        plan_by_id = {scene["id"]: scene for scene in plan["scenes"]}
        affected_ids = list(dict.fromkeys(issue["scene_id"] for issue in issues))
        for scene_id in affected_ids:
            ids = [scene_id]
            plan_slice = [plan_by_id[scene_id] for scene_id in ids]
            current = [by_id[scene_id] for scene_id in ids]
            issue_slice = [issue for issue in issues if issue["scene_id"] in set(ids)]
            repair_hashes: dict[str, str] = {}
            for repair_scene_id in ids:
                repair_payload = {
                    "brief": brief,
                    "plan": plan_by_id[repair_scene_id],
                    "current": by_id[repair_scene_id],
                    "issues": [
                        issue
                        for issue in issue_slice
                        if issue["scene_id"] == repair_scene_id
                    ],
                }
                if translated:
                    repair_payload["translated"] = True
                repair_hashes[repair_scene_id] = artifact_hash(
                    f"repair:{repair_scene_id}",
                    self.model_identity,
                    repair_payload,
                )
            await self._event(
                article_id,
                stage=f"repair:{scene_id}",
                outcome="started",
                metadata={"scene_count": len(ids)},
            )
            try:
                raw = await self.llm.complete_json(
                    REPAIR_SYSTEM_PROMPT,
                    build_repair_prompt(
                        brief,
                        ledger,
                        plan_slice,
                        current,
                        issue_slice,
                        translated=translated,
                    ),
                    output_schema=scene_batch_output_schema(
                        plan_slice,
                        repair=True,
                        translated=translated,
                        ledger=ledger,
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                retryable = isinstance(
                    exc,
                    (
                        httpx.TransportError,
                        OpenRouterTransientError,
                        CerebrasTransientError,
                    ),
                )
                for scene_id in ids:
                    await self._save(
                        article_id,
                        stage=f"repair:{scene_id}",
                        input_hash=repair_hashes[scene_id],
                        status="failed",
                        data=None,
                        error=f"LLM error: {exc}",
                    )
                await self._event(
                    article_id,
                    stage=f"repair:{scene_id}",
                    outcome="failed",
                    error_code=(
                        "provider_transient"
                        if retryable
                        else "provider_request_failed"
                    ),
                    error_detail=f"LLM error: {exc}",
                    retryable=retryable,
                )
                raise PipelineStageError(
                    "repair",
                    [f"LLM error: {exc}"],
                    error_code=(
                        "provider_transient"
                        if retryable
                        else "provider_request_failed"
                    ),
                    retryable=retryable,
                ) from exc
            repaired = project_scene_numeric_grounding(
                overlay_scene_invariants(
                    raw,
                    plan_slice,
                    translated=translated,
                    ledger=ledger,
                ),
                plan_slice,
                ledger,
            )
            errors = validate_scene_batch(
                repaired,
                plan_slice,
                ledger=ledger,
                translated=translated,
            )
            if errors:
                candidates = {
                    scene["id"]: scene
                    for scene in repaired
                    if isinstance(scene, dict) and scene.get("id")
                }
                for scene_id in ids:
                    await self._save(
                        article_id,
                        stage=f"repair:{scene_id}",
                        input_hash=repair_hashes[scene_id],
                        status="failed",
                        data=candidates.get(scene_id),
                        error="; ".join(errors[:8]),
                    )
                await self._event(
                    article_id,
                    stage=f"repair:{scene_id}",
                    outcome="failed",
                    error_code="repair_semantic_invalid",
                    error_detail="; ".join(errors[:8]),
                )
                raise PipelineStageError("repair", errors)
            for scene in repaired:
                planned = plan_by_id[scene["id"]]
                stage = f"scene:{scene['id']}"
                input_hash = artifact_hash(
                    stage,
                    self.model_identity,
                    self._scene_input(
                        brief,
                        ledger,
                        planned,
                        translated=translated,
                    ),
                )
                by_id[scene["id"]] = scene
                await self._save(
                    article_id,
                    stage=f"repair:{scene['id']}",
                    input_hash=repair_hashes[scene["id"]],
                    status="ok",
                    data=scene,
                    error=None,
                )
                await self._event(
                    article_id,
                    stage=f"repair:{scene['id']}",
                    outcome="succeeded",
                )
                await self._save(
                    article_id,
                    stage=stage,
                    input_hash=input_hash,
                    status="ok",
                    data=scene,
                    error=None,
                )
        return [by_id[item["id"]] for item in plan["scenes"]]

    async def compose(
        self,
        article_id: str,
        *,
        source: dict,
        images: list[dict],
        image_parts: list[ImagePart],
        as_of: datetime | None = None,
    ) -> dict:
        source_errors = validate_source_packet(source, min_chars=self.s.map_min_source_chars)
        source_input_hash = artifact_hash("source", "deterministic", source)
        if source_errors:
            await self._save(
                article_id,
                stage="source",
                input_hash=source_input_hash,
                status="failed",
                data=source,
                error="; ".join(source_errors[:8]),
                model="deterministic",
            )
            await self._event(
                article_id,
                stage="source",
                outcome="terminal",
                error_code="input_ineligible",
                error_detail="; ".join(source_errors[:8]),
            )
            raise PipelineStageError(
                "source",
                source_errors,
                error_code="input_ineligible",
                retryable=False,
            )
        await self._save(
            article_id,
            stage="source",
            input_hash=source_input_hash,
            status="ok",
            data=source,
            error=None,
            model="deterministic",
        )
        await self._event(
            article_id,
            stage="source",
            outcome="succeeded",
        )

        evidence_selection = await self._stage_call(
            article_id,
            stage="evidence_refs",
            input_payload=source,
            system=EVIDENCE_SELECTION_SYSTEM_PROMPT,
            user_text=build_evidence_selection_prompt(source),
            normalize=lambda raw: normalize_evidence_selection(raw, source),
            validate=lambda value: validate_evidence_selection(value, source),
            images=image_parts,
            validation_corrections=2,
            output_schema=evidence_selection_output_schema(source),
        )
        ledger = await self._stage_call(
            article_id,
            stage="evidence",
            input_payload=evidence_selection,
            system=EVIDENCE_CLAIM_SYSTEM_PROMPT,
            user_text=build_evidence_claim_prompt(evidence_selection, source),
            normalize=lambda raw: normalize_evidence_claim_ledger(
                raw,
                evidence_selection,
                source,
            ),
            validate=lambda value: validate_evidence_claim_ledger(
                value,
                evidence_selection,
                source,
            ),
            validation_corrections=2,
            output_schema=evidence_claim_text_output_schema(evidence_selection),
        )
        subject_assets = subject_assets_from_ledger(ledger.get("claims") or [])
        publisher_context = build_publisher_context(
            url=str(source.get("source_url") or ""),
            domain=str(source.get("domain") or ""),
            subject_assets=subject_assets,
            registry=self.publisher_registry,
        )
        context_error = document_context_error(
            document_type=str(source.get("document_type") or "") or None,
            publisher_context=publisher_context.sentence,
        )
        if context_error is not None:
            await self._event(
                article_id,
                stage="planner",
                outcome="terminal",
                error_code=context_error.error_code,
                error_detail="; ".join(context_error.errors),
                retryable=False,
            )
            raise context_error
        if publisher_context.is_empty:
            # The registry knows nothing about this publisher. DR-13245 lets
            # the web answer instead, as a possibility rather than a fact.
            researched = await self._research_publisher_context(
                article_id,
                domain=str(source.get("domain") or ""),
                title=str(source.get("title") or ""),
                subject_assets=subject_assets,
            )
            if researched is not None:
                publisher_context = researched
        cover_image_ref = select_cover_image_ref(source, ledger)
        planning = await self._stage_call(
            article_id,
            stage="planner",
            input_payload=ledger,
            system=PLANNING_SYSTEM_PROMPT,
            user_text=build_planning_prompt(
                ledger,
                min_scenes=self.s.map_min_scenes,
                max_scenes=self.s.map_max_scenes,
                document_type=str(source.get("document_type") or "") or None,
            ),
            normalize=lambda raw: canonicalize_planning(
                raw,
                ledger,
                self.s,
                cover_ref=cover_image_ref,
            ),
            validate=lambda value: validate_planning(value, ledger, self.s),
            validation_corrections=2,
            output_schema=planner_output_schema(
                ledger,
                min_scenes=self.s.map_min_scenes,
                max_scenes=self.s.map_max_scenes,
            ),
        )
        if self.image_rag is not None:
            try:
                augmentation = await self.image_rag.augment(
                    article_id,
                    source=source,
                    ledger=ledger,
                    planning=planning,
                    images=images,
                    cover_ref=cover_image_ref,
                    vision=self.llm,
                )
                if (
                    isinstance(getattr(augmentation, "ledger", None), dict)
                    and isinstance(getattr(augmentation, "planning", None), dict)
                    and isinstance(getattr(augmentation, "images", None), list)
                ):
                    ledger = augmentation.ledger
                    planning = augmentation.planning
                    images = augmentation.images
            except Exception:  # noqa: BLE001 - RAG is optional enrichment
                log.warning("image RAG augmentation skipped code=pipeline_rag_failed")
        skeleton_errors = validate_planning(planning, ledger, self.s)
        if skeleton_errors:
            raise PipelineStageError(
                "skeleton",
                skeleton_errors,
                error_code="skeleton_contract_invalid",
                retryable=False,
            )
        brief = planning["brief"]
        plan = planning["plan"]
        await self._save(
            article_id,
            stage="skeleton",
            input_hash=artifact_hash(
                "skeleton",
                "deterministic",
                {"ledger": ledger, "planning": planning},
            ),
            status="ok",
            data=planning,
            error=None,
            model="deterministic-skeleton-v1",
        )
        await self._save(
            article_id,
            stage="brief",
            input_hash=artifact_hash("brief", self.model_identity, ledger),
            status="ok",
            data=brief,
            error=None,
        )
        await self._save(
            article_id,
            stage="plan",
            input_hash=artifact_hash(
                "plan", self.model_identity, {"ledger": ledger, "brief": brief}
            ),
            status="ok",
            data=plan,
            error=None,
        )
        scenes = await self._compose_scenes(
            article_id,
            brief=brief,
            ledger=ledger,
            plan=plan,
            translated=scene_output_is_translated(source),
        )

        report = await self._contract_critic(
            article_id,
            ledger=ledger,
            plan=plan,
            scenes=scenes,
            translated=scene_output_is_translated(source),
        )

        tagged_atoms = await self._tag_numeric_atoms(
            article_id, ledger=ledger, as_of=as_of
        )
        delivery = compile_delivery_map(
            source=source,
            ledger=ledger,
            brief=brief,
            scenes=scenes,
            images=images,
            critic=report,
            publisher_context=publisher_context.sentence,
            publisher_context_basis=publisher_context.basis,
            tagged_atoms=tagged_atoms,
        )
        result = validate_map(
            delivery,
            n_images=len(images),
            source_text=source["body_text"],
        )
        if not result.ok:
            raise PipelineStageError(
                "delivery_map",
                result.errors,
                error_code="delivery_map_semantic_invalid",
                retryable=False,
            )
        delivery["used_refs"] = result.used_img_refs
        delivery["used_chart_refs"] = result.used_chart_refs
        return delivery
