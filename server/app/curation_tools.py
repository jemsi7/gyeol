"""Least-privilege article-pool tools for the curation model."""
from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Mapping

from .config import Settings
from .curation import CurationCandidate, build_curation_candidate
from .repository import Repository


class CurationArticleTools:
    """Bounded search/detail adapter with a per-run candidate registry."""

    def __init__(self, settings: Settings, repo: Repository):
        self.settings = settings
        self.repo = repo
        self._candidates: dict[str, CurationCandidate] = {}
        self.search_calls = 0

    @property
    def candidates(self) -> tuple[CurationCandidate, ...]:
        return tuple(self._candidates.values())

    @property
    def seen_ids(self) -> frozenset[str]:
        return frozenset(self._candidates)

    async def search_articles(
        self,
        *,
        query: str = "",
        offset: int = 0,
        limit: int = 12,
    ) -> dict[str, Any]:
        if (
            not isinstance(query, str)
            or isinstance(offset, bool)
            or not isinstance(offset, int)
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or (query.strip() and not 2 <= len(" ".join(query.split())) <= 80)
            or not 0 <= offset <= 200
            or not 1 <= limit <= 20
        ):
            return {"ok": False, "error": "tool_invalid_arguments"}
        rows = await self.repo.curation_search_rows(
            query=query,
            offset=offset,
            limit=limit,
            since_days=self.settings.curation_candidate_days,
        )
        self.search_calls += 1
        results: list[dict[str, Any]] = []
        truncated = False
        for row in rows:
            candidate = build_curation_candidate(row)
            if candidate is None:
                continue
            existing = self._candidates.get(candidate.article_id)
            if existing is None:
                if len(self._candidates) >= self.settings.curation_candidate_limit:
                    truncated = True
                    continue
                self._candidates[candidate.article_id] = candidate
            else:
                candidate = existing
            results.append(candidate.prompt_dict())
        return {
            "ok": True,
            "articles": results,
            "article_count": len(results),
            "total_seen": len(self._candidates),
            "truncated": truncated,
        }

    async def get_article_details(
        self, *, article_ids: Sequence[str]
    ) -> dict[str, Any]:
        if (
            isinstance(article_ids, (str, bytes))
            or not isinstance(article_ids, Sequence)
            or not 1 <= len(article_ids) <= 8
            or any(not isinstance(article_id, str) for article_id in article_ids)
            or len(set(article_ids)) != len(article_ids)
        ):
            return {"ok": False, "error": "tool_invalid_arguments"}
        ids = list(article_ids)
        if any(article_id not in self._candidates for article_id in ids):
            return {"ok": False, "error": "article_not_seen"}
        rows = await self.repo.curation_detail_rows(
            article_ids=ids,
            since_days=self.settings.curation_candidate_days,
        )
        row_by_id = {str(row.get("article_id")): row for row in rows}
        unavailable = [article_id for article_id in ids if article_id not in row_by_id]
        if unavailable:
            for article_id in unavailable:
                self._candidates.pop(article_id, None)
            return {"ok": False, "error": "article_unavailable"}

        details: list[dict[str, Any]] = []
        for article_id in ids:
            row = row_by_id[article_id]
            candidate = build_curation_candidate(row)
            if candidate is None:
                self._candidates.pop(article_id, None)
                return {"ok": False, "error": "article_unavailable"}
            self._candidates[article_id] = candidate
            detail = candidate.prompt_dict()
            detail["scenes"] = _scene_previews(row.get("data"))
            details.append(detail)
        return {
            "ok": True,
            "articles": details,
            "article_count": len(details),
            "total_seen": len(self._candidates),
        }


def _scene_previews(raw_map: Any) -> list[dict[str, str]]:
    if isinstance(raw_map, str):
        try:
            raw_map = json.loads(raw_map)
        except (TypeError, ValueError):
            return []
    if not isinstance(raw_map, Mapping):
        return []
    scenes = raw_map.get("scenes")
    if not isinstance(scenes, list):
        return []
    previews: list[dict[str, str]] = []
    for scene in scenes[:9]:
        if not isinstance(scene, Mapping):
            continue
        previews.append(
            {
                "title": _preview(scene.get("title"), 120),
                "summary": _preview(scene.get("summary"), 240),
                "body_preview": _preview(scene.get("body"), 240),
            }
        )
    return previews


def _preview(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.replace("\x00", " ").split())[:limit]
