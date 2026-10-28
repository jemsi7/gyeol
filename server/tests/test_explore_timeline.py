from __future__ import annotations

import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from app.explore_timeline import (
    TimelineCandidate,
    TimelineCluster,
    TimelineLane,
    TimelineSimilarityEdge,
    TimelineUpdateKind,
    TimelineValidationError,
    build_timeline_plan,
    validate_timeline_catalog,
)


NOW = datetime(2026, 8, 2, 12, tzinfo=UTC)
KST = ZoneInfo("Asia/Seoul")


def candidate(
    article_id: str,
    *,
    days_ago: float,
    domain: str = "news.example",
    relevance: float = 0.9,
    centrality: float = 0.5,
    published: bool = True,
    source_hash: str = "",
) -> TimelineCandidate:
    occurred_at = NOW - timedelta(days=days_ago)
    return TimelineCandidate(
        article_id=article_id,
        source_domain=domain,
        relevance_score=relevance,
        centrality=centrality,
        published_at=occurred_at if published else None,
        first_seen_at=occurred_at,
        source_hash=source_hash,
    )


def cluster(
    cluster_key: str,
    candidates: list[TimelineCandidate],
    *,
    member_ids: tuple[str, ...] | None = None,
) -> TimelineCluster:
    return TimelineCluster(
        cluster_key=cluster_key,
        topic=f"topic {cluster_key}",
        member_article_ids=member_ids or tuple(item.article_id for item in candidates),
    )


def complete_edges(candidates: list[TimelineCandidate]) -> list[TimelineSimilarityEdge]:
    return [
        TimelineSimilarityEdge(left.article_id, right.article_id, 0.5)
        for index, left in enumerate(candidates)
        for right in candidates[index + 1 :]
    ]


def make_plan(cluster_key: str, offset: int = 0):
    items = [
        candidate(
            f"{cluster_key}-article-{index}",
            days_ago=offset + index,
            domain="one.example" if index % 2 == 0 else "two.example",
            relevance=0.96 - index * 0.04,
            centrality=0.9 - index * 0.05,
        )
        for index in range(5)
    ]
    return build_timeline_plan(cluster(cluster_key, items), items, complete_edges(items))


def test_chronological_date_diff_marks_occurred_inserted_and_existing_with_stable_ids():
    before = [
        candidate("a", days_ago=2, domain="one.example", relevance=0.98),
        candidate("b", days_ago=3, domain="two.example", relevance=0.96),
        candidate("c", days_ago=4, relevance=0.94),
        candidate("d", days_ago=5, domain="two.example", relevance=0.92),
    ]
    previous = build_timeline_plan(cluster("stable", before), before, complete_edges(before))

    after = [
        candidate("new-latest", days_ago=1, domain="three.example", relevance=0.99),
        before[0],
        candidate("middle-context", days_ago=2.5, relevance=0.97),
        before[1],
        before[2],
        before[3],
    ]
    current = build_timeline_plan(
        cluster(
            "reclustered",
            after,
            member_ids=("a", "b", "c", "d", "new-latest", "middle-context"),
        ),
        after,
        complete_edges(after),
        previous,
    )

    assert current.stable_key == previous.stable_key
    assert [node.article_id for node in current.nodes] == [
        "d",
        "c",
        "b",
        "a",
        "middle-context",
        "new-latest",
    ]
    kinds = {node.article_id: node.update_kind for node in current.nodes}
    assert kinds["new-latest"] is TimelineUpdateKind.OCCURRED
    assert kinds["middle-context"] is TimelineUpdateKind.INSERTED
    assert kinds["a"] is TimelineUpdateKind.EXISTING
    assert next(node for node in current.nodes if node.article_id == "a").node_id == next(
        node for node in previous.nodes if node.article_id == "a"
    ).node_id


def test_main_and_branch_topology_uses_best_main_and_deterministic_tie_anchor():
    items = [
        candidate("main-new", days_ago=1, relevance=0.99, centrality=0.99),
        candidate("main-mid", days_ago=2, domain="two.example", relevance=0.98, centrality=0.98),
        candidate("main-old", days_ago=3, relevance=0.97, centrality=0.97),
        candidate("branch-best", days_ago=4, relevance=0.70, centrality=0.10),
        candidate("branch-tie", days_ago=5, domain="two.example", relevance=0.69, centrality=0.10),
    ]
    edges = complete_edges(items)
    edges = [
        edge
        for edge in edges
        if "branch-best" not in (edge.left_article_id, edge.right_article_id)
        and "branch-tie" not in (edge.left_article_id, edge.right_article_id)
    ] + [
        TimelineSimilarityEdge("branch-best", "main-new", 0.6),
        TimelineSimilarityEdge("branch-best", "main-mid", 0.9),
        TimelineSimilarityEdge("branch-best", "main-old", 0.7),
        TimelineSimilarityEdge("branch-tie", "main-new", 0.8),
        TimelineSimilarityEdge("branch-tie", "main-mid", 0.8),
        TimelineSimilarityEdge("branch-tie", "main-old", 0.4),
    ]

    plan = build_timeline_plan(cluster("topology", items), items, edges)
    by_article = {node.article_id: node for node in plan.nodes}

    assert {node.article_id for node in plan.nodes if node.lane is TimelineLane.MAIN} == {
        "main-new",
        "main-mid",
        "main-old",
    }
    assert by_article["branch-best"].anchor_node_id == by_article["main-mid"].node_id
    assert by_article["branch-tie"].anchor_node_id == by_article["main-new"].node_id


def test_first_seen_fallback_is_explicit_and_participates_in_chronological_date_order():
    items = [
        candidate("provider", days_ago=2, domain="one.example"),
        candidate("fallback", days_ago=1, domain="two.example", published=False),
        candidate("third", days_ago=3),
        candidate("fourth", days_ago=4, domain="two.example"),
    ]

    plan = build_timeline_plan(cluster("date-fallback", items), items, complete_edges(items))

    assert plan.nodes[-1].article_id == "fallback"
    assert plan.nodes[-1].date_source == "first_seen"
    provider = next(node for node in plan.nodes if node.article_id == "provider")
    assert provider.date_source == "provider"


def test_top_n_replacement_enforces_two_source_domains():
    items = [
        candidate(f"same-{index}", days_ago=index + 1, relevance=0.99 - index * 0.01)
        for index in range(4)
    ] + [
        candidate("alternate", days_ago=5, domain="other.example", relevance=0.5),
    ]

    plan = build_timeline_plan(
        cluster("diverse", items),
        items,
        complete_edges(items),
        max_nodes=4,
    )

    assert len(plan.nodes) == 4
    assert "alternate" in {node.article_id for node in plan.nodes}
    assert len(plan.source_domains) == 2


def test_main_uses_next_ranked_day_when_higher_ranked_candidates_share_a_day():
    items = [
        candidate("same-day-first", days_ago=0.0, relevance=0.99, centrality=0.99),
        candidate("same-day-second", days_ago=0.1, relevance=0.98, centrality=0.98),
        candidate("same-day-third", days_ago=0.2, relevance=0.97, centrality=0.97),
        candidate("next-day", days_ago=1, relevance=0.96, centrality=0.96, domain="two.example"),
        candidate("third-day", days_ago=2, relevance=0.95, centrality=0.95),
        candidate("fourth-day", days_ago=3, relevance=0.94, centrality=0.94),
    ]

    plan = build_timeline_plan(
        cluster("main-day-backfill", items),
        items,
        complete_edges(items),
        max_nodes=6,
    )

    main = [node.article_id for node in plan.nodes if node.lane is TimelineLane.MAIN]
    assert set(main) == {"same-day-first", "next-day", "third-day"}
    assert len(
        {node.published_at.astimezone(KST).date() for node in plan.nodes if node.lane is TimelineLane.MAIN}
    ) == 3
    assert "same-day-second" in {node.article_id for node in plan.nodes if node.lane is TimelineLane.BRANCH}


def test_branch_may_share_main_day_but_exact_content_duplicate_is_replaced():
    exact_hash = "a" * 64
    items = [
        candidate("main", days_ago=0, relevance=0.99, centrality=0.99, source_hash=exact_hash),
        candidate("day-two", days_ago=1, relevance=0.98, centrality=0.98, domain="two.example"),
        candidate("day-three", days_ago=2, relevance=0.97, centrality=0.97),
        candidate("same-content", days_ago=0.1, relevance=0.96, source_hash=exact_hash),
        candidate("same-day-branch", days_ago=0.2, relevance=0.95),
    ]

    plan = build_timeline_plan(
        cluster("content-hash", items),
        items,
        complete_edges(items),
        max_nodes=4,
    )

    by_id = {node.article_id: node for node in plan.nodes}
    assert "same-content" not in by_id
    assert by_id["same-day-branch"].lane is TimelineLane.BRANCH
    assert by_id["same-day-branch"].published_at.date() == by_id["main"].published_at.date()


def test_semantic_duplicate_is_replaced_by_next_candidate():
    items = [
        candidate("main", days_ago=0, relevance=0.99, centrality=0.99),
        candidate("day-two", days_ago=1, relevance=0.98, centrality=0.98, domain="two.example"),
        candidate("day-three", days_ago=2, relevance=0.97, centrality=0.97),
        candidate("paraphrase", days_ago=3, relevance=0.96),
        candidate("next-unique", days_ago=4, relevance=0.95),
    ]
    edges = [
        TimelineSimilarityEdge(left.article_id, right.article_id, 0.5)
        for index, left in enumerate(items)
        for right in items[index + 1 :]
        if {left.article_id, right.article_id} != {"main", "paraphrase"}
    ]
    edges.append(TimelineSimilarityEdge("main", "paraphrase", 0.92))

    plan = build_timeline_plan(
        cluster("semantic-content", items),
        items,
        edges,
        max_nodes=4,
    )

    assert "paraphrase" not in {node.article_id for node in plan.nodes}
    assert "next-unique" in {node.article_id for node in plan.nodes}


def test_main_date_and_content_selection_is_shuffle_stable():
    items = [
        candidate("main", days_ago=0, relevance=0.99, centrality=0.99),
        candidate("same-day", days_ago=0.1, relevance=0.98, centrality=0.98),
        candidate("day-two", days_ago=1, relevance=0.97, centrality=0.97, domain="two.example"),
        candidate("paraphrase", days_ago=2, relevance=0.96),
        candidate("day-three", days_ago=3, relevance=0.95),
        candidate("branch", days_ago=0.2, relevance=0.94),
    ]
    edges = complete_edges(items)
    edges = [
        edge for edge in edges if {edge.left_article_id, edge.right_article_id} != {"main", "paraphrase"}
    ] + [TimelineSimilarityEdge("main", "paraphrase", 0.92)]
    shuffled_items = list(items)
    shuffled_edges = list(edges)
    random.Random(12801).shuffle(shuffled_items)
    random.Random(12802).shuffle(shuffled_edges)

    expected = build_timeline_plan(cluster("unique-stable", items), items, edges, max_nodes=5)
    actual = build_timeline_plan(
        cluster("unique-stable", items),
        shuffled_items,
        shuffled_edges,
        max_nodes=5,
    )

    assert actual == expected


def test_candidate_and_edge_shuffle_produces_identical_plan():
    items = [
        candidate(
            f"article-{index}",
            days_ago=index,
            domain="one.example" if index % 2 == 0 else "two.example",
            relevance=0.9 - index * 0.03,
            centrality=0.8 - index * 0.02,
        )
        for index in range(8)
    ]
    edges = complete_edges(items)
    shuffled_items = list(items)
    shuffled_edges = list(edges)
    random.Random(12205).shuffle(shuffled_items)
    random.Random(12204).shuffle(shuffled_edges)

    expected = build_timeline_plan(cluster("shuffle", items), items, edges)
    actual = build_timeline_plan(cluster("shuffle", items), shuffled_items, shuffled_edges)

    assert actual == expected


def test_merge_preserves_highest_jaccard_previous_identity_and_low_overlap_is_new():
    left = make_plan("left")
    right = make_plan("right", offset=10)
    merged_items = [
        candidate(node.article_id, days_ago=index + 1, domain=node.source_domain)
        for index, node in enumerate((*right.nodes, *left.nodes[:2]))
    ]
    merged = build_timeline_plan(
        cluster("merged", merged_items),
        merged_items,
        complete_edges(merged_items),
        (left, right),
    )
    unrelated_items = [
        candidate(f"unrelated-{index}", days_ago=index, domain=f"d{index % 2}.example")
        for index in range(4)
    ]
    unrelated = build_timeline_plan(
        cluster("unrelated", unrelated_items),
        unrelated_items,
        complete_edges(unrelated_items),
        (left, right),
    )

    assert merged.stable_key == right.stable_key
    assert unrelated.stable_key not in {left.stable_key, right.stable_key}


def test_split_child_at_threshold_preserves_previous_identity():
    previous = make_plan("split-source")
    child_items = [
        candidate(node.article_id, days_ago=index, domain=node.source_domain)
        for index, node in enumerate(previous.nodes[:4])
    ]

    child = build_timeline_plan(
        cluster("split-child", child_items),
        child_items,
        complete_edges(child_items),
        previous,
    )

    assert child.stable_key == previous.stable_key
    assert all(node.node_id.version == 5 for node in child.nodes)


def test_same_date_uses_article_id_ascending_as_chronological_tie_break():
    items = [
        candidate("old-main", days_ago=3, relevance=0.90),
        candidate("same-day-main", days_ago=2, domain="two.example", relevance=0.89),
        candidate("same-day-branch", days_ago=2, relevance=0.88),
        candidate("new-main", days_ago=1, relevance=0.87),
    ]

    plan = build_timeline_plan(cluster("timestamp-tie", items), items, complete_edges(items))

    assert [node.article_id for node in plan.nodes] == [
        "old-main",
        "same-day-branch",
        "same-day-main",
        "new-main",
    ]


def test_timeline_order_uses_kst_dates_not_source_times():
    items = [
        TimelineCandidate(
            article_id="aug-1",
            source_domain="one.example",
            relevance_score=0.99,
            centrality=0.9,
            published_at=datetime(2026, 7, 31, 16, tzinfo=UTC),  # KST 8/1 01:00
        ),
        TimelineCandidate(
            article_id="same-day-b",
            source_domain="two.example",
            relevance_score=0.98,
            centrality=0.8,
            published_at=datetime(2026, 8, 1, 16, tzinfo=UTC),  # KST 8/2 01:00
        ),
        TimelineCandidate(
            article_id="same-day-a",
            source_domain="one.example",
            relevance_score=0.97,
            centrality=0.7,
            published_at=datetime(2026, 8, 2, 12, tzinfo=UTC),  # KST 8/2 21:00
        ),
        TimelineCandidate(
            article_id="aug-3",
            source_domain="two.example",
            relevance_score=0.96,
            centrality=0.6,
            published_at=datetime(2026, 8, 2, 15, tzinfo=UTC),  # KST 8/3 00:00
        ),
    ]

    plan = build_timeline_plan(cluster("kst-days", items), items, complete_edges(items))

    assert [node.article_id for node in plan.nodes] == [
        "aug-1",
        "same-day-a",
        "same-day-b",
        "aug-3",
    ]
    assert plan.started_at == datetime(2026, 7, 31, 15, tzinfo=UTC)
    assert plan.ended_at == datetime(2026, 8, 2, 15, tzinfo=UTC)


def test_same_kst_day_is_inserted_and_next_kst_day_is_occurred():
    before = [
        TimelineCandidate(
            article_id="old-a",
            source_domain="one.example",
            relevance_score=0.99,
            centrality=0.9,
            published_at=datetime(2026, 8, 1, 15, tzinfo=UTC),  # KST 8/2
        ),
        TimelineCandidate(
            article_id="old-b",
            source_domain="two.example",
            relevance_score=0.98,
            centrality=0.8,
            published_at=datetime(2026, 8, 1, 14, tzinfo=UTC),  # KST 8/1
        ),
        TimelineCandidate(
            article_id="old-c",
            source_domain="one.example",
            relevance_score=0.97,
            centrality=0.7,
            published_at=datetime(2026, 7, 31, 15, tzinfo=UTC),  # KST 8/1
        ),
        TimelineCandidate(
            article_id="old-d",
            source_domain="two.example",
            relevance_score=0.96,
            centrality=0.6,
            published_at=datetime(2026, 7, 30, 15, tzinfo=UTC),  # KST 7/31
        ),
    ]
    previous = build_timeline_plan(cluster("same-day", before), before, complete_edges(before))
    current_items = [
        *before,
        TimelineCandidate(
            article_id="same-day-new",
            source_domain="three.example",
            relevance_score=0.95,
            centrality=0.5,
            published_at=datetime(2026, 8, 2, 14, tzinfo=UTC),  # KST 8/2
        ),
        TimelineCandidate(
            article_id="next-day-new",
            source_domain="three.example",
            relevance_score=0.94,
            centrality=0.4,
            published_at=datetime(2026, 8, 2, 15, tzinfo=UTC),  # KST 8/3
        ),
    ]

    current = build_timeline_plan(
        cluster("same-day-reclustered", current_items),
        current_items,
        complete_edges(current_items),
        previous,
        max_nodes=6,
    )

    kinds = {node.article_id: node.update_kind for node in current.nodes}
    assert kinds["same-day-new"] is TimelineUpdateKind.INSERTED
    assert kinds["next-day-new"] is TimelineUpdateKind.OCCURRED


@pytest.mark.parametrize("count", [4, 8])
def test_catalog_accepts_exact_timeline_bounds(count: int):
    plans = tuple(make_plan(f"catalog-{index}", offset=index * 10) for index in range(count))

    assert validate_timeline_catalog(plans) == plans


@pytest.mark.parametrize(
    ("count", "code"),
    [(3, "timeline_catalog_too_small"), (9, "timeline_catalog_too_large")],
)
def test_catalog_rejects_out_of_bounds_timeline_counts(count: int, code: str):
    plans = tuple(make_plan(f"bounds-{index}", offset=index * 10) for index in range(count))

    with pytest.raises(TimelineValidationError, match=code):
        validate_timeline_catalog(plans)


def test_catalog_rejects_article_reuse_across_timelines():
    first = make_plan("duplicate-first")
    second = make_plan("duplicate-second")
    duplicated_node = replace(second.nodes[0], article_id=first.nodes[0].article_id)
    second = replace(second, nodes=(duplicated_node, *second.nodes[1:]))

    with pytest.raises(TimelineValidationError, match="timeline_article_duplicate"):
        validate_timeline_catalog((first, second, make_plan("third"), make_plan("fourth")))


def test_catalog_validator_rejects_orphan_branch_anchor():
    damaged = make_plan("damaged")
    branch_index = next(
        index for index, node in enumerate(damaged.nodes) if node.lane is TimelineLane.BRANCH
    )
    damaged_nodes = list(damaged.nodes)
    damaged_nodes[branch_index] = replace(damaged_nodes[branch_index], anchor_node_id=uuid4())
    damaged = replace(damaged, nodes=tuple(damaged_nodes))

    with pytest.raises(TimelineValidationError, match="timeline_branch_anchor_invalid"):
        validate_timeline_catalog(
            (damaged, make_plan("valid-two"), make_plan("valid-three"), make_plan("valid-four"))
        )


def test_builder_rejects_too_few_nodes_and_branch_without_main_similarity():
    too_few = [
        candidate(f"few-{index}", days_ago=index, domain=f"d{index % 2}.example")
        for index in range(3)
    ]
    with pytest.raises(TimelineValidationError, match="timeline_nodes_too_few"):
        build_timeline_plan(cluster("few", too_few), too_few, complete_edges(too_few))

    disconnected = [
        candidate(
            f"disconnected-{index}",
            days_ago=index,
            domain=f"d{index % 2}.example",
            relevance=0.99 - index * 0.1,
            centrality=0.99 - index * 0.1,
        )
        for index in range(4)
    ]
    with pytest.raises(TimelineValidationError, match="timeline_branch_similarity_missing"):
        build_timeline_plan(cluster("disconnected", disconnected), disconnected, [])
