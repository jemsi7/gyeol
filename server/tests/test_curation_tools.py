from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from app.config import Settings
from app.curation_tools import CurationArticleTools


def _row(index: int, *, status: str = "ok"):
    paragraph = "구조적 변화와 장기 영향을 여러 근거로 설명하는 충분히 긴 문장입니다. " * 4
    return {
        "article_id": f"article-{index}",
        "title": f"원문 기사 {index}",
        "domain": f"source-{index % 4}.example",
        "published_at": datetime(2026, 7, 26, tzinfo=UTC),
        "status": status,
        "data": {
            "format_version": 3,
            "headline": f"구조를 설명하는 기사 {index}",
            "summary": paragraph,
            "keywords": ["구조", "근거", f"주제{index}"],
            "body_markdown": "RAW-MAP-BODY-MUST-NOT-LEAK",
            "selected_claim_refs": ["CL-001", "CL-002", "CL-003", "CL-004"],
            "required_claim_refs": ["CL-001"],
            "scenes": [
                {
                    "title": f"장면 {scene}",
                    "summary": paragraph,
                    "body": paragraph + ("X" * 500),
                    "secondary": "",
                    "quote": "",
                }
                for scene in range(4)
            ],
            "editorial": {
                "critic_verdict": "pass",
                "source_hash": f"{index:064x}",
            },
        },
    }


class FakeRepo:
    def __init__(self, search_rows):
        self.search_rows = search_rows
        self.search_calls = []
        self.detail_calls = []
        self.detail_override = None

    async def curation_search_rows(self, **kwargs):
        self.search_calls.append(kwargs)
        return self.search_rows

    async def curation_detail_rows(self, **kwargs):
        self.detail_calls.append(kwargs)
        if self.detail_override is not None:
            return self.detail_override
        by_id = {row["article_id"]: row for row in self.search_rows}
        return [
            by_id[article_id]
            for article_id in kwargs["article_ids"]
            if article_id in by_id
        ]


def _settings(**values):
    return Settings(_env_file=None, CURATION_CANDIDATE_LIMIT=8, **values)


@pytest.mark.asyncio
async def test_search_structurally_filters_and_caps_seen_registry():
    repo = FakeRepo([_row(index) for index in range(10)] + [_row(99, status="degraded")])
    tools = CurationArticleTools(_settings(), repo)

    result = await tools.search_articles(query="", offset=0, limit=20)

    assert result["ok"] is True
    assert result["article_count"] == 8
    assert result["total_seen"] == 8
    assert result["truncated"] is True
    assert tools.search_calls == 1
    assert len(tools.candidates) == 8
    assert repo.search_calls[0] == {
        "query": "",
        "offset": 0,
        "limit": 20,
        "since_days": 30,
    }
    serialized = json.dumps(result, ensure_ascii=False)
    assert "RAW-MAP-BODY-MUST-NOT-LEAK" not in serialized


@pytest.mark.asyncio
async def test_details_requires_seen_ids_and_returns_bounded_scene_previews():
    repo = FakeRepo([_row(0), _row(1)])
    tools = CurationArticleTools(_settings(), repo)

    unseen = await tools.get_article_details(article_ids=["article-0"])
    assert unseen == {"ok": False, "error": "article_not_seen"}
    assert repo.detail_calls == []

    await tools.search_articles(query="구조", limit=2)
    result = await tools.get_article_details(
        article_ids=["article-1", "article-0"]
    )

    assert result["ok"] is True
    assert [item["article_id"] for item in result["articles"]] == [
        "article-1",
        "article-0",
    ]
    assert all(len(scene["body_preview"]) <= 240 for item in result["articles"] for scene in item["scenes"])
    assert repo.detail_calls[0]["article_ids"] == ["article-1", "article-0"]


@pytest.mark.asyncio
async def test_detail_recheck_removes_article_that_left_the_pool():
    repo = FakeRepo([_row(0), _row(1)])
    tools = CurationArticleTools(_settings(), repo)
    await tools.search_articles(query="구조", limit=2)
    repo.detail_override = [_row(0)]

    result = await tools.get_article_details(
        article_ids=["article-0", "article-1"]
    )

    assert result == {"ok": False, "error": "article_unavailable"}
    assert "article-1" not in tools.seen_ids
    assert "article-0" in tools.seen_ids


@pytest.mark.asyncio
async def test_tool_argument_bounds_fail_before_repository_calls():
    repo = FakeRepo([_row(0)])
    tools = CurationArticleTools(_settings(), repo)

    assert await tools.search_articles(query="a") == {
        "ok": False,
        "error": "tool_invalid_arguments",
    }
    assert await tools.search_articles(offset=201) == {
        "ok": False,
        "error": "tool_invalid_arguments",
    }
    assert await tools.get_article_details(article_ids=[]) == {
        "ok": False,
        "error": "tool_invalid_arguments",
    }
    assert repo.search_calls == []
    assert repo.detail_calls == []
