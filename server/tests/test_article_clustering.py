from __future__ import annotations

import hashlib
import random
from datetime import UTC, datetime, timedelta

import pytest

from app.article_clustering import (
    CLUSTER_ALGORITHM_VERSION,
    ArticleVectorCandidate,
    ArticleVectorEdge,
    build_article_clusters,
    validate_cluster_plan,
)
from app.article_cluster_worker import _cluster_input_hash


NOW = datetime(2026, 8, 2, 12, tzinfo=UTC)


def _candidate(
    article_id: str,
    *,
    domain: str,
    age_days: int = 0,
    published_at: datetime | None = None,
) -> ArticleVectorCandidate:
    return ArticleVectorCandidate(
        article_id=article_id,
        source_hash=hashlib.sha256(f"source:{article_id}".encode()).hexdigest(),
        domain=domain,
        published_at=published_at or NOW - timedelta(days=age_days),
    )


def _mutual(left: str, right: str, similarity: float) -> list[ArticleVectorEdge]:
    return [
        ArticleVectorEdge(left, right, similarity),
        ArticleVectorEdge(right, left, similarity),
    ]


def test_mutual_knn_is_order_invariant_and_breaks_chain_drift():
    candidates = [
        _candidate(article_id, domain=f"source-{index % 2}.example")
        for index, article_id in enumerate(("a", "b", "c", "d", "e"))
    ]
    edges = [
        *_mutual("a", "b", 0.91),
        *_mutual("b", "c", 0.90),
        *_mutual("c", "d", 0.89),
        *_mutual("d", "e", 0.88),
    ]

    first = build_article_clusters(candidates, edges)
    shuffled_candidates = list(reversed(candidates))
    shuffled_edges = list(edges)
    random.Random(12200).shuffle(shuffled_edges)
    second = build_article_clusters(shuffled_candidates, shuffled_edges)

    assert first == second
    assert first.algorithm_version == CLUSTER_ALGORITHM_VERSION
    assert len(first.clusters) == 1
    assert first.clusters[0].member_count == 3
    assert {
        member.article_id for member in first.clusters[0].members
    } != {candidate.article_id for candidate in candidates}
    assert first.clusters[0].medoid_article_id in {
        member.article_id for member in first.clusters[0].members
    }


def test_one_way_knn_edges_do_not_form_a_cluster():
    candidates = [
        _candidate("a", domain="a.example"),
        _candidate("b", domain="b.example"),
        _candidate("c", domain="c.example"),
    ]
    edges = [
        ArticleVectorEdge("a", "b", 0.95),
        ArticleVectorEdge("b", "c", 0.95),
        ArticleVectorEdge("c", "a", 0.95),
    ]

    assert build_article_clusters(candidates, edges).clusters == ()


def test_major_cluster_requires_three_articles_and_two_domains():
    candidates = [
        _candidate("a", domain="one.example"),
        _candidate("b", domain="one.example"),
        _candidate("c", domain="two.example"),
        _candidate("d", domain="same.example"),
        _candidate("e", domain="same.example"),
        _candidate("f", domain="same.example"),
        _candidate("g", domain="g.example"),
        _candidate("h", domain="h.example"),
    ]
    edges = [
        *_mutual("a", "b", 0.90),
        *_mutual("a", "c", 0.88),
        *_mutual("b", "c", 0.87),
        *_mutual("d", "e", 0.95),
        *_mutual("d", "f", 0.94),
        *_mutual("e", "f", 0.93),
        *_mutual("g", "h", 0.99),
    ]

    plan = build_article_clusters(candidates, edges)

    assert len(plan.clusters) == 1
    cluster = plan.clusters[0]
    assert {member.article_id for member in cluster.members} == {"a", "b", "c"}
    assert cluster.domain_count == 2
    assert cluster.rank == 1
    assert [member.centrality_rank for member in cluster.members] == [1, 2, 3]
    assert 0.0 <= cluster.major_score <= 1.0


def test_cluster_plan_population_tracks_bounded_vector_coverage():
    candidates = [
        _candidate("a", domain="a.example"),
        _candidate("b", domain="b.example"),
        _candidate("c", domain="c.example"),
    ]
    edges = [
        *_mutual("a", "b", 0.9),
        *_mutual("a", "c", 0.9),
        *_mutual("b", "c", 0.9),
    ]
    plan = build_article_clusters(candidates, edges).with_population(
        article_count=5,
        indexed_count=4,
    )

    assert plan.article_count == 5
    assert plan.indexed_count == 4
    assert plan.coverage == 0.8
    validate_cluster_plan(plan)

    with pytest.raises(ValueError, match="envelope"):
        build_article_clusters(candidates, edges).with_population(
            article_count=3,
            indexed_count=4,
        )
    with pytest.raises(ValueError, match="cluster members"):
        build_article_clusters(candidates, edges).with_population(
            article_count=4,
            indexed_count=2,
        )


def test_cluster_inputs_reject_unknown_edges_duplicates_and_naive_dates():
    valid = _candidate("a", domain="a.example")
    with pytest.raises(ValueError, match="unique"):
        build_article_clusters([valid, valid], [])
    with pytest.raises(ValueError, match="unknown"):
        build_article_clusters(
            [valid, _candidate("b", domain="b.example")],
            [ArticleVectorEdge("a", "missing", 0.8)],
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        ArticleVectorCandidate(
            "a",
            hashlib.sha256(b"a").hexdigest(),
            "a.example",
            datetime(2026, 8, 2),
        )


def test_cluster_period_and_hash_use_kst_dates_not_source_times():
    first = [
        _candidate(
            "a",
            domain="one.example",
            published_at=datetime(2026, 8, 1, 15, tzinfo=UTC),  # KST 8/2 00:00
        ),
        _candidate(
            "b",
            domain="two.example",
            published_at=datetime(2026, 8, 2, 2, tzinfo=UTC),  # KST 8/2 11:00
        ),
        _candidate(
            "c",
            domain="one.example",
            published_at=datetime(2026, 8, 2, 14, tzinfo=UTC),  # KST 8/2 23:00
        ),
    ]
    corrected_times = [
        _candidate(
            "a",
            domain="one.example",
            published_at=datetime(2026, 8, 1, 18, tzinfo=UTC),
        ),
        _candidate(
            "b",
            domain="two.example",
            published_at=datetime(2026, 8, 2, 7, tzinfo=UTC),
        ),
        _candidate(
            "c",
            domain="one.example",
            published_at=datetime(2026, 8, 2, 13, tzinfo=UTC),
        ),
    ]
    edges = [*_mutual("a", "b", 0.9), *_mutual("a", "c", 0.9), *_mutual("b", "c", 0.9)]

    first_plan = build_article_clusters(first, edges)
    corrected_plan = build_article_clusters(corrected_times, edges)

    assert first_plan == corrected_plan
    assert first_plan.clusters[0].first_published_at == datetime(2026, 8, 1, 15, tzinfo=UTC)
    assert first_plan.clusters[0].last_published_at == datetime(2026, 8, 1, 15, tzinfo=UTC)
    assert _cluster_input_hash(first, candidate_count=3) == _cluster_input_hash(
        corrected_times,
        candidate_count=3,
    )
