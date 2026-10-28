from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.trending_search import (
    TrendingArticle,
    TrendingMetric,
    TrendingSearchSnapshot,
    build_keyword_pool,
    normalize_keyword,
    rank_category_keywords,
    sample_keyword_pool,
    trending_bucket_started_at,
    view_growth_score,
)


BUCKET = datetime(2026, 8, 1, 3, 10, tzinfo=UTC)


def _article(
    metric: TrendingMetric,
    rank: int,
    *keywords: str,
    article_number: int | None = None,
) -> TrendingArticle:
    return TrendingArticle(
        metric=metric,
        article_id=f"{article_number if article_number is not None else rank:064x}",
        rank=rank,
        metric_value=float(11 - rank),
        recent_count=5,
        previous_count=1,
        keywords=tuple(keywords),
    )


def test_keyword_weighting_normalization_ties_and_display():
    articles = [
        _article(
            TrendingMetric.VIEW_TOTAL,
            1,
            " ＡＩ ",
            "베타",
            "알파",
            "AI",
        ),
        _article(TrendingMetric.VIEW_TOTAL, 2, "ai", "Cloud"),
        _article(TrendingMetric.VIEW_TOTAL, 3, "cloud", "한 글자 아님"),
    ]

    ranked = rank_category_keywords(articles)

    assert ranked[:5] == ("AI", "Cloud", "베타", "알파", "한 글자 아님")
    assert ranked.count("AI") == 1
    assert normalize_keyword("  ＡＩ   인프라 ") == ("ai 인프라", "AI 인프라")
    assert normalize_keyword("A") is None
    assert normalize_keyword("AI\n인프라") is None
    assert normalize_keyword("가" * 61) is None
    assert normalize_keyword(42) is None


def test_keyword_pool_cross_category_dedupes_and_backfills_to_nine():
    rankings = {
        TrendingMetric.VIEW_TOTAL: [
            _article(TrendingMetric.VIEW_TOTAL, 1, "공통", article_number=1),
            _article(TrendingMetric.VIEW_TOTAL, 2, "조회 A", article_number=2),
            _article(TrendingMetric.VIEW_TOTAL, 3, "조회 B", article_number=3),
            _article(TrendingMetric.VIEW_TOTAL, 4, "조회 C", article_number=4),
        ],
        TrendingMetric.VIEW_GROWTH: [
            _article(TrendingMetric.VIEW_GROWTH, 1, "공통", article_number=11),
            _article(TrendingMetric.VIEW_GROWTH, 2, "상승 D", article_number=12),
            _article(TrendingMetric.VIEW_GROWTH, 3, "상승 E", article_number=13),
            _article(TrendingMetric.VIEW_GROWTH, 4, "상승 F", article_number=14),
        ],
        TrendingMetric.BOOKMARK_TOTAL: [
            _article(TrendingMetric.BOOKMARK_TOTAL, 1, "조회 A", article_number=21),
            _article(TrendingMetric.BOOKMARK_TOTAL, 2, "상승 D", article_number=22),
            _article(TrendingMetric.BOOKMARK_TOTAL, 3, "저장 G", article_number=23),
            _article(TrendingMetric.BOOKMARK_TOTAL, 4, "저장 H", article_number=24),
            _article(TrendingMetric.BOOKMARK_TOTAL, 5, "저장 I", article_number=25),
        ],
    }

    assert build_keyword_pool(rankings) == (
        "공통",
        "조회 A",
        "조회 B",
        "상승 D",
        "상승 E",
        "상승 F",
        "저장 G",
        "저장 H",
        "저장 I",
    )


def test_keyword_sample_is_unique_stable_in_bucket_and_changes_seed():
    pool = ("인공지능", "클라우드", "반도체", "전력망", "로봇", "데이터센터")

    first = sample_keyword_pool(pool, bucket_started_at=BUCKET)
    same_bucket = sample_keyword_pool(
        pool,
        bucket_started_at=BUCKET.astimezone(timezone(timedelta(hours=9))) + timedelta(minutes=4),
    )
    later_bucket = sample_keyword_pool(
        pool,
        bucket_started_at=BUCKET + timedelta(minutes=10),
    )

    assert first == same_bucket
    assert len(first) == len(set(first)) == 3
    assert set(first).issubset(pool)
    assert later_bucket != first
    assert sample_keyword_pool(pool[:2], bucket_started_at=BUCKET) in {
        ("인공지능", "클라우드"),
        ("클라우드", "인공지능"),
    }
    assert sample_keyword_pool((), bucket_started_at=BUCKET) == ()


def test_view_growth_formula_zero_denominator_minimum_and_decline():
    assert view_growth_score(5, 0) == 5
    assert view_growth_score(0, 5) == -1
    assert view_growth_score(4, 0) is None
    assert view_growth_score(7, 3) == pytest.approx(4 / 3)
    with pytest.raises(ValueError):
        view_growth_score(-1, 6)


def test_trending_article_rejects_malformed_repository_values():
    with pytest.raises(ValueError):
        TrendingArticle(
            metric=TrendingMetric.VIEW_TOTAL,
            article_id="article",
            rank=0,
            metric_value=1,
            recent_count=0,
            previous_count=0,
            keywords=(),
        )
    with pytest.raises(ValueError):
        TrendingArticle(
            metric=TrendingMetric.VIEW_TOTAL,
            article_id="article",
            rank=1,
            metric_value=float("nan"),
            recent_count=0,
            previous_count=0,
            keywords=(),
        )


def test_bucket_and_snapshot_value_contract_are_bounded_and_timezone_aware():
    as_of = datetime(2026, 8, 1, 12, 14, 59, 999999, tzinfo=timezone(timedelta(hours=9)))
    assert trending_bucket_started_at(as_of) == datetime(2026, 8, 1, 3, 10, tzinfo=UTC)
    with pytest.raises(ValueError):
        trending_bucket_started_at(datetime(2026, 8, 1, 3, 10))

    snapshot = TrendingSearchSnapshot(
        bucket_started_at=BUCKET,
        category_keywords={TrendingMetric.VIEW_TOTAL: ("인공지능",)},
        keyword_pool=("인공지능", "클라우드"),
        suggestions=("클라우드",),
        generated_at=BUCKET + timedelta(seconds=1),
    )
    assert snapshot.suggestions == ("클라우드",)
    with pytest.raises(ValueError):
        TrendingSearchSnapshot(
            bucket_started_at=BUCKET + timedelta(minutes=1),
            category_keywords={},
            keyword_pool=(),
            suggestions=(),
            generated_at=BUCKET + timedelta(minutes=1),
        )
