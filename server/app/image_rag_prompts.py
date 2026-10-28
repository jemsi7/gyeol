"""Prompt and strict response contract for RAG candidate vision admission."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

RAG_VERIFICATION_SYSTEM_PROMPT = """You verify whether retrieved news images are factual evidence for exact article claims.
Inspect each image in the same order as the candidate list. Approve only when the visible people, place, time context, event and action are consistent with the requested scene claims and origin metadata. Visual similarity is never sufficient. Reject ambiguous, generic, contradictory, unrelated, composite, chart, logo or text-only images. Do not infer an identity, date, location or event that is not visibly supported. Return one JSON object only."""


@dataclass(frozen=True, slots=True)
class RagApproval:
    candidate_id: str
    scene_id: str
    claim_refs: tuple[str, ...]
    caption: str
    reason: str


def build_rag_verification_prompt(
    *,
    article_title: str,
    ledger: dict,
    candidates: list[Any],
) -> str:
    claim_text = {
        str(claim.get("id")): str(claim.get("text") or "").strip()
        for claim in ledger.get("claims") or []
        if isinstance(claim, dict) and claim.get("id")
    }
    items = []
    for index, candidate in enumerate(candidates):
        claim_refs = list(getattr(candidate, "claim_refs", ()) or ())
        items.append(
            {
                "image_order": index,
                "candidate_id": str(getattr(candidate, "image_id", "")),
                "requested_scene_id": str(getattr(candidate, "scene_id", "")),
                "requested_claims": [
                    {"id": ref, "text": claim_text.get(ref, "")}
                    for ref in claim_refs
                ],
                "origin_article_id": str(
                    getattr(candidate, "origin_article_id", "")
                ),
                "origin_title": str(getattr(candidate, "origin_title", "")),
                "origin_url": str(getattr(candidate, "origin_url", "")),
                "origin_domain": str(getattr(candidate, "origin_domain", "")),
                "source_image_url": str(getattr(candidate, "source_url", "")),
            }
        )
    contract = {
        "approved": [
            {
                "candidate_id": "exact candidate_id",
                "scene_id": "exact requested_scene_id",
                "claim_refs": ["one or more exact requested claim IDs"],
                "caption": "short factual Korean caption without unsupported details",
                "reason": "why visible content supports those exact claims",
            }
        ]
    }
    return (
        "Current article title:\n"
        + str(article_title).strip()
        + "\n\nCandidates (same order as attached images):\n"
        + json.dumps(items, ensure_ascii=False, separators=(",", ":"))
        + "\n\nReturn this shape; omit every rejected candidate:\n"
        + json.dumps(contract, ensure_ascii=False, separators=(",", ":"))
    )


def normalize_rag_verdict(raw: Any) -> list[RagApproval]:
    raw = raw if isinstance(raw, dict) else {}
    approved = raw.get("approved")
    if not isinstance(approved, list):
        return []
    result: list[RagApproval] = []
    for item in approved[:18]:
        if not isinstance(item, dict):
            continue
        refs = item.get("claim_refs")
        claim_refs = tuple(
            dict.fromkeys(
                str(ref).strip()
                for ref in (refs if isinstance(refs, list) else [])
                if isinstance(ref, str) and ref.strip()
            )
        )
        result.append(
            RagApproval(
                candidate_id=str(item.get("candidate_id") or "").strip(),
                scene_id=str(item.get("scene_id") or "").strip(),
                claim_refs=claim_refs,
                caption=str(item.get("caption") or "").strip(),
                reason=str(item.get("reason") or "").strip(),
            )
        )
    return result


def validate_rag_verdict(
    approvals: list[RagApproval],
    candidates: list[Any],
    ledger: dict,
    *,
    missing_views: int,
) -> list[str]:
    errors: list[str] = []
    candidate_by_id = {
        str(getattr(candidate, "image_id", "")): candidate
        for candidate in candidates
    }
    ledger_claims = {
        str(claim.get("id"))
        for claim in ledger.get("claims") or []
        if isinstance(claim, dict) and claim.get("id")
    }
    seen_candidates: set[str] = set()
    seen_scenes: set[str] = set()
    if len(approvals) > max(0, int(missing_views)):
        errors.append("approved count exceeds missing image views")
    for index, approval in enumerate(approvals):
        path = f"approved[{index}]"
        candidate = candidate_by_id.get(approval.candidate_id)
        if candidate is None:
            errors.append(f"{path}.candidate_id is not in the request")
            continue
        if approval.candidate_id in seen_candidates:
            errors.append(f"{path}.candidate_id is duplicated")
        seen_candidates.add(approval.candidate_id)
        expected_scene = str(getattr(candidate, "scene_id", ""))
        if approval.scene_id != expected_scene:
            errors.append(f"{path}.scene_id does not match the candidate")
        if approval.scene_id in seen_scenes:
            errors.append(f"{path}.scene_id is duplicated")
        seen_scenes.add(approval.scene_id)
        allowed_claims = set(getattr(candidate, "claim_refs", ()) or ())
        if (
            not approval.claim_refs
            or not set(approval.claim_refs) <= allowed_claims
            or not set(approval.claim_refs) <= ledger_claims
        ):
            errors.append(f"{path}.claim_refs must be a non-empty requested subset")
        if not approval.caption or len(approval.caption) > 300:
            errors.append(f"{path}.caption must be 1..300 characters")
        if not approval.reason or len(approval.reason) > 500:
            errors.append(f"{path}.reason must be 1..500 characters")
    return errors
