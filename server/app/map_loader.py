"""Resolve a stored map into a loadable view.

The map's body_markdown contains `<img:N>` placeholders. N is an article-local
ref index that points (via the map's embedded `images` array, mirrored in the
article_image_links table) at a stored image. This module rewrites the
placeholders into real server image references so a client can render the
article directly.

`image_url_for(image_id)` is injected so the same resolver works whether images
are served from this app (`/images/{id}`), an S3 public bucket, or a CDN.
"""
from __future__ import annotations

import re
from typing import Callable, Optional

from .directives import CHART_RE, IMG_RE as IMG_PLACEHOLDER_RE


def resolve_map(
    map_data: dict,
    image_url_for: Callable[[str], str],
    *,
    links: Optional[list[dict]] = None,
    inline_markdown: bool = True,
) -> dict:
    """Return a resolved copy of the map.

    map_data: the stored `data` JSON (has body_markdown + images[]).
    image_url_for: image_id -> URL.
    links: optional article_image_links rows (fallback if map has no images[]).
    inline_markdown: also produce `body_resolved` with placeholders replaced by
      Markdown image tags. The raw `body_markdown` (with <img:N>) is preserved.
    """
    # ref -> {image_id, url, source_url, caption}
    ref_map: dict[int, dict] = {}
    for img in map_data.get("images") or []:
        try:
            ref = int(img.get("ref"))
        except (TypeError, ValueError):
            continue
        image_id = img.get("image_id")
        if not image_id:
            continue
        ref_map[ref] = {
            "ref": ref,
            "image_id": image_id,
            "url": image_url_for(image_id),
            "source_url": img.get("source_url"),
            "role": img.get("role"),
            "origin_article_id": img.get("origin_article_id"),
            "retrieval_score": img.get("retrieval_score"),
        }
    # fallback to DB links if the map didn't embed images
    if not ref_map and links:
        for row in links:
            ref = int(row["ref_index"])
            ref_map[ref] = {
                "ref": ref,
                "image_id": row["image_id"],
                "url": image_url_for(row["image_id"]),
                "source_url": row.get("source_url"),
                "role": row.get("role"),
            }

    # merge captions from image_placements
    for p in map_data.get("image_placements") or []:
        try:
            ref = int(p.get("ref"))
        except (TypeError, ValueError):
            continue
        if ref in ref_map:
            ref_map[ref]["caption"] = p.get("caption") or ""

    body = map_data.get("body_markdown") or ""
    used_refs: list[int] = []

    def _repl(m: re.Match) -> str:
        ref = int(m.group(1))
        info = ref_map.get(ref)
        if not info:
            return ""
        used_refs.append(ref)
        if not inline_markdown:
            return m.group(0)
        cap = info.get("caption") or ""
        return f"![{cap}]({info['url']})"

    body_resolved = IMG_PLACEHOLDER_RE.sub(_repl, body) if inline_markdown else body
    used_chart_refs = sorted({int(match.group(1)) for match in CHART_RE.finditer(body)})
    raw_cover_ref = map_data.get("cover_image_ref")
    try:
        cover_image_ref = (
            None
            if raw_cover_ref is None or isinstance(raw_cover_ref, bool)
            else int(raw_cover_ref)
        )
    except (TypeError, ValueError):
        cover_image_ref = None
    if cover_image_ref not in ref_map:
        cover_image_ref = None

    return {
        "format_version": map_data.get("format_version"),
        "language": map_data.get("language"),
        "headline": map_data.get("headline"),
        "subheadline": map_data.get("subheadline"),
        "summary": map_data.get("summary"),
        "keywords": map_data.get("keywords", []),
        "body_markdown": body,          # raw, with <img:N> placeholders
        "body_resolved": body_resolved,  # placeholders -> markdown images
        "images": [ref_map[k] for k in sorted(ref_map)],
        "cover_image_ref": cover_image_ref,
        "used_refs": sorted(set(used_refs)),
        "visualizations": map_data.get("visualizations") or [],
        "used_chart_refs": used_chart_refs,
        "claim_ids": map_data.get("claim_ids") or [],
        "required_claim_refs": map_data.get("required_claim_refs") or [],
        "selected_claim_refs": map_data.get("selected_claim_refs") or [],
        "scenes": map_data.get("scenes") or [],
        # publisher_context_basis is deliberately absent. This projection is an
        # allowlist, so the inferred/operator half of DR-13203 cannot reach a
        # reader by someone forgetting to strip it.
        "publisher_context": map_data.get("publisher_context") or "",
        "tagged_atoms": map_data.get("tagged_atoms") or [],
        "editorial": map_data.get("editorial") or {},
    }
