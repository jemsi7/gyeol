from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.recommendation import (
    ArticleRecommendationFeature,
    Daypart,
    RecommendationCandidate,
    RecommendationProfile,
    RecommendationSortKey,
    RecommendationSurface,
    ScoredCandidate,
    ScoreBreakdown,
    extract_article_recommendation_feature,
    hydrate_recommendation_profile,
    normalize_terms,
    rank_recommendations,
    score_candidate,
    select_diverse,
)


NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def _feature(
    number: int,
    *,
    terms: tuple[str, ...] = ("인공지능",),
    domain: str = "example.com",
    topic: str = "인공지능",
    age_days: int = 0,
    quality: float = 1.0,
    headline: str | None = None,
) -> ArticleRecommendationFeature:
    return ArticleRecommendationFeature(
        article_id=f"{number:064x}",
        terms=terms,
        term_weights={term: 1.0 for term in terms},
        representative_topic=topic,
        domain=domain,
        published_at=NOW - timedelta(days=age_days),
        quality_score=quality,
        headline=headline or f"기사 {number}",
    )


def _event(
    event_type: str,
    *,
    occurred_at: datetime = NOW,
    query: str | None = None,
    article_id: str | None = None,
    scene_index: int = 0,
    scene_count: int = 4,
    local_hour: int = 12,
) -> dict:
    return {
        "event_type": event_type,
        "occurred_at": occurred_at,
        "utc_offset_minutes": 0,
        "local_date": occurred_at.date(),
        "local_hour": local_hour,
        "query": query,
        "article_id": article_id,
        "scene_index": scene_index,
        "scene_count": scene_count,
    }


def _profile(
    *,
    count: int = 20,
    terms: dict[str, float] | None = None,
    daypart_terms: dict[str, float] | None = None,
    excluded: frozenset[str] = frozenset(),
    seen: frozenset[str] = frozenset(),
) -> RecommendationProfile:
    return RecommendationProfile(
        global_terms=terms or {"인공지능": 1.0},
        daypart_terms=daypart_terms or {},
        domain_weights={},
        negative_terms={},
        excluded_article_ids=excluded,
        recently_seen_article_ids=seen,
        meaningful_event_count=count,
        snapshot_at=NOW,
        current_daypart=Daypart.AFTERNOON,
    )


def test_feature_extractor_normalizes_bounded_non_body_terms():
    feature = extract_article_recommendation_feature(
        article_id="a" * 64,
        map_data={
            "headline": " AI가 바꾸는 도시 ",
            "subheadline": "새로운 이동",
            "summary": "인공지능과 대중교통",
            "keywords": ["AI", "도시"],
            "body_markdown": "이 비밀 본문 토큰은 인덱싱하지 않는다",
            "scenes": [{"title": "출근길 변화", "summary": "혼잡을 줄였다"}],
        },
        domain="News.Example.",
        published_at=NOW,
        status="degraded",
    )

    assert feature.domain == "news.example"
    assert feature.representative_topic == "ai"
    assert feature.term_weights["ai"] == 1.0
    assert feature.term_weights["새로운"] == 0.7
    assert feature.term_weights["출근길"] == 0.4
    assert "비밀" not in feature.terms
    assert feature.quality_score == 0.65
    assert len(feature.terms) <= 64
    assert normalize_terms("ＡＩ  AI") == ("ai",)


def test_profile_bounds_180_days_500_events_and_caps_repeats():
    events = [
        _event(
            "search",
            query=f"검색{i}",
            occurred_at=NOW - timedelta(minutes=i),
        )
        for i in range(501)
    ]
    events.extend(
        [
            _event("search", query="반복 검색"),
            _event("search", query="반복 검색", occurred_at=NOW - timedelta(seconds=1)),
            _event(
                "search",
                query="너무 오래됨",
                occurred_at=NOW - timedelta(days=181),
            ),
        ]
    )

    profile = hydrate_recommendation_profile(
        events=events,
        feedback=[],
        article_features={},
        snapshot_at=NOW,
        current_daypart=Daypart.AFTERNOON,
    )

    # The raw window is capped first; the duplicated query then contributes
    # once, so the meaningful count remains below the 500-row read bound.
    assert profile.meaningful_event_count == 499
    assert "너무" not in profile.global_terms
    assert all(weight > 0 for weight in profile.global_terms.values())


def test_profile_applies_search_open_progress_complete_decay():
    article = _feature(1, terms=("ai",))
    events = [
        _event("search", query="AI"),
        _event("article_opened", article_id=article.article_id),
        _event(
            "scene_viewed",
            article_id=article.article_id,
            scene_index=1,
            scene_count=4,
        ),
        _event(
            "scene_viewed",
            article_id=article.article_id,
            scene_index=3,
            scene_count=4,
        ),
        _event(
            "article_completed",
            article_id=article.article_id,
            scene_index=3,
            scene_count=4,
        ),
    ]

    profile = hydrate_recommendation_profile(
        events=events,
        feedback=[],
        article_features={article.article_id: article},
        snapshot_at=NOW,
        current_daypart=Daypart.AFTERNOON,
    )

    assert profile.meaningful_event_count == 4
    assert profile.global_terms["ai"] == pytest.approx(10.0)
    assert article.article_id in profile.recently_seen_article_ids

    old_profile = hydrate_recommendation_profile(
        events=[_event("search", query="AI", occurred_at=NOW - timedelta(days=30))],
        feedback=[],
        article_features={},
        snapshot_at=NOW,
        current_daypart=Daypart.AFTERNOON,
    )
    assert old_profile.global_terms["ai"] == pytest.approx(
        4.0 * 2 ** (-30 / 45)
    )
    assert old_profile.short_terms == {}


def test_saved_articles_anchor_profile_and_are_not_recommended_again():
    saved_article = _feature(11, terms=("기후", "에너지"))
    profile = hydrate_recommendation_profile(
        events=[],
        feedback=[],
        saved=[
            {
                "article_id": saved_article.article_id,
                "saved_at": NOW - timedelta(days=2),
            }
        ],
        article_features={saved_article.article_id: saved_article},
        snapshot_at=NOW,
        current_daypart=Daypart.AFTERNOON,
    )

    assert profile.saved_terms["기후"] > 4.0
    assert saved_article.article_id in profile.excluded_article_ids


def test_profile_feedback_penalties_are_reason_specific():
    topic = _feature(1, terms=("ai",), domain="trusted.example")
    source = _feature(2, terms=("경제",), domain="bad.example")
    profile = hydrate_recommendation_profile(
        events=[],
        feedback=[
            {"article_id": topic.article_id, "reason": "not_relevant"},
            {"article_id": source.article_id, "reason": "distrust_source"},
        ],
        article_features={topic.article_id: topic, source.article_id: source},
        snapshot_at=NOW,
        current_daypart=Daypart.AFTERNOON,
    )

    assert profile.excluded_article_ids == frozenset(
        {topic.article_id, source.article_id}
    )
    assert profile.negative_terms["ai"] == 2.0
    assert profile.domain_weights["bad.example"] == -3.0


def test_daypart_blends_only_after_ten_signals():
    nine = [
        _event("search", query=f"오후{i}", occurred_at=NOW - timedelta(minutes=i))
        for i in range(9)
    ]
    ten = nine + [
        _event("search", query="열번째", occurred_at=NOW - timedelta(minutes=10))
    ]

    nine_profile = hydrate_recommendation_profile(
        events=nine,
        feedback=[],
        article_features={},
        snapshot_at=NOW,
        current_daypart=Daypart.AFTERNOON,
    )
    ten_profile = hydrate_recommendation_profile(
        events=ten,
        feedback=[],
        article_features={},
        snapshot_at=NOW,
        current_daypart=Daypart.AFTERNOON,
    )

    assert nine_profile.daypart_terms == {}
    assert ten_profile.daypart_terms


def test_home_and_boundary_score_formulas_and_cold_start():
    context = _feature(1, terms=("반도체",), domain="context.example")
    candidate = RecommendationCandidate(
        _feature(2, terms=("반도체",), domain="candidate.example")
    )
    cold = _profile(count=10, terms={"반도체": 1.0})
    warm = _profile(count=20, terms={"반도체": 1.0})

    cold_home = score_candidate(
        cold,
        candidate,
        surface=RecommendationSurface.HOME,
        context=None,
        now=NOW,
    )
    warm_boundary = score_candidate(
        warm,
        candidate,
        surface=RecommendationSurface.ARTICLE_BOUNDARY,
        context=context,
        now=NOW,
    )

    assert cold_home.confidence == 0.5
    assert warm_boundary.confidence == 1.0
    assert warm_boundary.context_similarity == 1.0
    assert warm_boundary.score_micros == round(warm_boundary.base_score * 1_000_000)


def test_selector_deduplicates_and_relaxes_topic_before_domain():
    breakdown = ScoreBreakdown(0, 0, 0, 1, 1, 0, 0, 1, 1, 1_000_000)
    scored = [
        ScoredCandidate(
            RecommendationCandidate(
                _feature(
                    index,
                    domain="same.example" if index <= 3 else f"d{index}.example",
                    topic="same",
                    headline="중복" if index in {1, 2} else f"기사 {index}",
                )
            ),
            breakdown,
            "same",
        )
        for index in range(1, 6)
    ]

    selected = select_diverse(
        scored,
        surface=RecommendationSurface.HOME,
        limit=5,
    )

    assert len(selected) == 4
    assert [item.candidate.article_id for item in selected].count(f"{1:064x}") == 1
    assert f"{2:064x}" not in {item.candidate.article_id for item in selected}
    assert [item.candidate.feature.domain for item in selected[:2]] == [
        "same.example",
        "same.example",
    ]


def test_rank_is_deterministic_cursor_stable_and_filters_history():
    excluded = _feature(1)
    seen = _feature(2)
    candidates = [
        RecommendationCandidate(excluded),
        RecommendationCandidate(seen),
        *[RecommendationCandidate(_feature(index)) for index in range(3, 10)],
    ]
    profile = _profile(
        excluded=frozenset({excluded.article_id}),
        seen=frozenset({seen.article_id}),
    )

    first = rank_recommendations(
        profile=profile,
        candidates=candidates,
        surface=RecommendationSurface.HOME,
        context=None,
        generated_at=NOW,
        limit=5,
    )
    repeated = rank_recommendations(
        profile=profile,
        candidates=candidates,
        surface=RecommendationSurface.HOME,
        context=None,
        generated_at=NOW,
        limit=5,
    )

    assert first == repeated
    assert first.next_sort_key is not None
    assert excluded.article_id not in {item.candidate.article_id for item in first.items}
    assert seen.article_id not in {item.candidate.article_id for item in first.items}
    continuation = rank_recommendations(
        profile=profile,
        candidates=candidates,
        surface=RecommendationSurface.HOME,
        context=None,
        generated_at=NOW,
        limit=5,
        after=RecommendationSortKey(
            score_micros=first.next_sort_key.score_micros,
            published_at=first.next_sort_key.published_at,
            article_id=first.next_sort_key.article_id,
        ),
    )
    assert not (
        {item.candidate.article_id for item in first.items}
        & {item.candidate.article_id for item in continuation.items}
    )


def test_rank_seed_is_stable_within_session_and_changes_tied_order():
    candidates = [
        RecommendationCandidate(
            _feature(index, age_days=0, domain=f"d{index}.example")
        )
        for index in range(1, 18)
    ]
    profile = _profile()

    first = rank_recommendations(
        profile=profile,
        candidates=candidates,
        surface=RecommendationSurface.HOME,
        context=None,
        generated_at=NOW,
        limit=5,
        rank_seed=b"session-a",
    )
    repeated = rank_recommendations(
        profile=profile,
        candidates=candidates,
        surface=RecommendationSurface.HOME,
        context=None,
        generated_at=NOW,
        limit=5,
        rank_seed=b"session-a",
    )
    changed = rank_recommendations(
        profile=profile,
        candidates=candidates,
        surface=RecommendationSurface.HOME,
        context=None,
        generated_at=NOW,
        limit=5,
        rank_seed=b"session-b",
    )

    assert first == repeated
    assert [item.candidate.article_id for item in first.items] != [
        item.candidate.article_id for item in changed.items
    ]


def test_pipeline_never_emits_raw_identity_or_query():
    raw_query = "회계팀만 아는 비밀 검색"
    profile = hydrate_recommendation_profile(
        events=[_event("search", query=raw_query)],
        feedback=[],
        article_features={},
        snapshot_at=NOW,
        current_daypart=Daypart.AFTERNOON,
    )
    page = rank_recommendations(
        profile=profile,
        candidates=[RecommendationCandidate(_feature(1))],
        surface=RecommendationSurface.HOME,
        context=None,
        generated_at=NOW,
        limit=1,
    )

    assert raw_query not in repr(profile)
    assert raw_query not in repr(page)
