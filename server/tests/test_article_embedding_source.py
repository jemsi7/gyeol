from __future__ import annotations

import hashlib

import pytest

from app.article_embedding_worker import build_article_embedding_source


def test_c10007_source_is_deterministic_bounded_and_hashes_exact_text():
    map_data = {
        "headline": "  ＡI   인프라  ",
        "subheadline": "비용\n구조",
        "summary": "요약 문장",
        "keywords": ["GPU", "  추론  ", "GPU"],
        "scenes": [
            {"title": "첫 장면", "summary": "첫 요약", "body": "포함 금지"},
            {"title": "둘째 장면", "summary": ""},
        ],
        "body_markdown": "임베딩 source에 포함하면 안 되는 본문",
        "images": [{"caption": "포함 금지"}],
    }

    first = build_article_embedding_source(map_data)
    second = build_article_embedding_source(dict(reversed(list(map_data.items()))))

    assert first == second
    assert first.text == (
        "headline: AI 인프라\n"
        "subheadline: 비용 구조\n"
        "summary: 요약 문장\n"
        "keywords: GPU | 추론 | GPU\n"
        "scene_1_title: 첫 장면\n"
        "scene_1_summary: 첫 요약\n"
        "scene_2_title: 둘째 장면"
    )
    assert "포함 금지" not in first.text
    assert first.source_hash == hashlib.sha256(first.text.encode("utf-8")).hexdigest()

    bounded = build_article_embedding_source(
        {"headline": "가" * 100, "summary": "나" * 100},
        max_chars=64,
    )
    assert len(bounded.text) == 64
    assert bounded.source_hash == hashlib.sha256(
        bounded.text.encode("utf-8")
    ).hexdigest()


def test_c10007_source_ignores_unapproved_fields_and_malformed_nodes():
    source = build_article_embedding_source(
        {
            "headline": "유효 제목",
            "keywords": "문자열은 keyword 배열이 아님",
            "scenes": [None, "bad", {"title": 3, "summary": "유효 장면"}] * 10,
        }
    )

    assert source.text == "headline: 유효 제목\nscene_3_summary: 유효 장면\nscene_6_summary: 유효 장면\nscene_9_summary: 유효 장면\nscene_12_summary: 유효 장면\nscene_15_summary: 유효 장면\nscene_18_summary: 유효 장면"

    with pytest.raises(ValueError, match="article_embedding_source_empty"):
        build_article_embedding_source({"body_markdown": "제외되는 본문"})
    with pytest.raises(ValueError, match="max_chars"):
        build_article_embedding_source({"headline": "제목"}, max_chars=0)
