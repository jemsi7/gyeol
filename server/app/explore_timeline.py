"""Deterministic topology planner for precomputed Explore timelines.

This module intentionally has no provider, repository, or database imports.  It
turns an already-clustered and reranked article set into an immutable timeline,
diffs that topology against a previous edition, and validates the complete
4--8 timeline publication boundary.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Mapping, Sequence
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo


TIMELINE_ID_NAMESPACE = UUID("951c32d8-bcf7-5f96-8c68-e406be74661c")
MIN_MAIN_NODES = 3
TIMELINE_TIME_ZONE = ZoneInfo("Asia/Seoul")
TIMELINE_CONTENT_DUPLICATE_SIMILARITY = 0.92


class TimelineValidationError(ValueError):
    """A stable, redacted planner validation failure."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class TimelineLane(StrEnum):
    MAIN = "main"
    BRANCH = "branch"


class TimelineUpdateKind(StrEnum):
    EXISTING = "existing"
    INSERTED = "inserted"
    OCCURRED = "occurred"


class TimelineDateSource(StrEnum):
    PROVIDER = "provider"
    FIRST_SEEN = "first_seen"


@dataclass(frozen=True, slots=True)
class TimelineCluster:
    cluster_key: str
    topic: str
    member_article_ids: tuple[str, ...]
    title: str = ""
    summary: str = ""


@dataclass(frozen=True, slots=True)
class TimelineCandidate:
    article_id: str
    source_domain: str
    relevance_score: float
    centrality: float
    published_at: datetime | None = None
    first_seen_at: datetime | None = None
    source_hash: str = ""


@dataclass(frozen=True, slots=True)
class TimelineSimilarityEdge:
    left_article_id: str
    right_article_id: str
    similarity: float


@dataclass(frozen=True, slots=True)
class TimelineNode:
    node_id: UUID
    article_id: str
    lane: TimelineLane
    anchor_node_id: UUID | None
    display_order: int
    update_kind: TimelineUpdateKind
    relevance_score: float
    published_at: datetime
    date_source: TimelineDateSource
    # Planner-only metadata used by the topology validator.  The publication
    # adapter persists the public fields above and already owns article data.
    source_domain: str


@dataclass(frozen=True, slots=True)
class TimelinePlan:
    stable_key: str
    cluster_key: str
    topic: str
    title: str
    summary: str
    started_at: datetime
    ended_at: datetime
    nodes: tuple[TimelineNode, ...]
    # Full cluster membership is retained only so the next edition can perform
    # the specified member-Jaccard identity match instead of comparing top-N.
    member_article_ids: tuple[str, ...] = ()
    source_domains: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _ResolvedCandidate:
    article_id: str
    source_domain: str
    relevance_score: float
    centrality: float
    occurred_at: datetime
    date_source: TimelineDateSource
    source_hash: str


def build_timeline_plan(
    cluster: TimelineCluster,
    reranked: Sequence[TimelineCandidate],
    edges: Sequence[TimelineSimilarityEdge],
    previous: TimelinePlan | Sequence[TimelinePlan] | None = None,
    *,
    min_nodes: int = 4,
    max_nodes: int = 8,
) -> TimelinePlan:
    """Build one provider-free, date-chronological timeline plan.

    ``reranked`` may arrive in any order; relevance and deterministic article
    IDs define candidate order.  ``previous`` accepts one plan or the previous
    catalog so cluster merges can retain the maximum-Jaccard identity.
    """

    _validate_node_bounds(min_nodes, max_nodes)
    cluster_key = _bounded_text(cluster.cluster_key, "timeline_cluster_invalid", 200)
    topic = _bounded_text(cluster.topic, "timeline_topic_invalid", 200)
    member_ids = _member_ids(cluster.member_article_ids)
    candidates = _resolve_candidates(reranked, member_ids)
    if len(candidates) < min_nodes:
        raise TimelineValidationError("timeline_nodes_too_few")

    _similarity_map(edges)

    selected, main_candidates = _select_timeline_candidates(
        candidates,
        edges,
        max_nodes=max_nodes,
    )
    if len(selected) < min_nodes:
        raise TimelineValidationError("timeline_nodes_too_few")

    matched = _match_previous(member_ids, previous)
    stable_key = matched.stable_key if matched is not None else _new_stable_key(cluster_key)
    _stable_uuid(stable_key)
    previous_by_article = {
        node.article_id: node for node in matched.nodes
    } if matched is not None else {}
    previous_latest_day = max(
        (_timeline_day(node.published_at, "timeline_date_invalid")
         for node in previous_by_article.values()),
        default=None,
    )

    main_spine = sorted(
        (item for item in selected if item.article_id in main_candidates),
        key=_latest_day_value,
        reverse=True,
    )
    main_order = {item.article_id: index for index, item in enumerate(main_spine, 1)}
    node_ids = {
        item.article_id: _node_id(stable_key, item.article_id) for item in selected
    }
    similarities = _similarity_map(edges)

    anchors: dict[str, UUID] = {}
    for item in selected:
        if item.article_id in main_candidates:
            continue
        anchor = _best_anchor(
            item.article_id,
            main_spine,
            main_order=main_order,
            node_ids=node_ids,
            similarities=similarities,
        )
        anchors[item.article_id] = node_ids[anchor.article_id]

    chronological = sorted(selected, key=_chronological_day_value)
    nodes = tuple(
        TimelineNode(
            node_id=node_ids[item.article_id],
            article_id=item.article_id,
            lane=(
                TimelineLane.MAIN
                if item.article_id in main_candidates
                else TimelineLane.BRANCH
            ),
            anchor_node_id=anchors.get(item.article_id),
            display_order=index,
            update_kind=_update_kind(
                item,
                previous_by_article=previous_by_article,
                previous_latest_day=previous_latest_day,
            ),
            relevance_score=item.relevance_score,
            published_at=item.occurred_at,
            date_source=item.date_source,
            source_domain=item.source_domain,
        )
        for index, item in enumerate(chronological, 1)
    )
    started_at = min(
        _timeline_day(node.published_at, "timeline_date_invalid") for node in nodes
    )
    ended_at = max(
        _timeline_day(node.published_at, "timeline_date_invalid") for node in nodes
    )
    title = " ".join(cluster.title.split()) or topic
    summary = " ".join(cluster.summary.split()) or (
        f"{_timeline_date_text(started_at)}–{_timeline_date_text(ended_at)} "
        f"· 사건 {len(nodes)}개"
    )
    plan = TimelinePlan(
        stable_key=stable_key,
        cluster_key=cluster_key,
        topic=topic,
        title=_bounded_text(title, "timeline_title_invalid", 200),
        summary=_bounded_text(summary, "timeline_summary_invalid", 500),
        started_at=started_at,
        ended_at=ended_at,
        nodes=nodes,
        member_article_ids=member_ids,
        source_domains=tuple(sorted({node.source_domain for node in nodes})),
    )
    _validate_timeline_plan(plan, min_nodes=min_nodes, max_nodes=max_nodes)
    return plan


def validate_timeline_catalog(
    plans: Sequence[TimelinePlan],
    *,
    min_timelines: int = 4,
    max_timelines: int = 8,
) -> tuple[TimelinePlan, ...]:
    """Validate and freeze the all-or-nothing public catalog boundary."""

    if not 1 <= min_timelines <= max_timelines <= 8:
        raise TimelineValidationError("timeline_catalog_bounds_invalid")
    frozen = tuple(plans)
    if len(frozen) < min_timelines:
        raise TimelineValidationError("timeline_catalog_too_small")
    if len(frozen) > max_timelines:
        raise TimelineValidationError("timeline_catalog_too_large")

    # Detect cross-plan reuse before per-plan identity checks so the catalog
    # reports the publication-level invariant deterministically.
    all_articles: set[str] = set()
    for plan in frozen:
        for node in plan.nodes:
            if node.article_id in all_articles:
                raise TimelineValidationError("timeline_article_duplicate")
            all_articles.add(node.article_id)

    stable_keys: set[str] = set()
    cluster_keys: set[str] = set()
    for plan in frozen:
        _validate_timeline_plan(plan, min_nodes=4, max_nodes=8)
        if plan.stable_key in stable_keys:
            raise TimelineValidationError("timeline_stable_key_duplicate")
        if plan.cluster_key in cluster_keys:
            raise TimelineValidationError("timeline_cluster_duplicate")
        stable_keys.add(plan.stable_key)
        cluster_keys.add(plan.cluster_key)
    return frozen


def _validate_timeline_plan(
    plan: TimelinePlan,
    *,
    min_nodes: int,
    max_nodes: int,
) -> None:
    _stable_uuid(plan.stable_key)
    _bounded_text(plan.cluster_key, "timeline_cluster_invalid", 200)
    _bounded_text(plan.topic, "timeline_topic_invalid", 200)
    _bounded_text(plan.title, "timeline_title_invalid", 200)
    _bounded_text(plan.summary, "timeline_summary_invalid", 500)
    _aware_utc(plan.started_at, "timeline_date_invalid")
    _aware_utc(plan.ended_at, "timeline_date_invalid")
    if plan.started_at > plan.ended_at:
        raise TimelineValidationError("timeline_period_invalid")
    if not min_nodes <= len(plan.nodes) <= max_nodes:
        code = (
            "timeline_nodes_too_few"
            if len(plan.nodes) < min_nodes
            else "timeline_nodes_too_many"
        )
        raise TimelineValidationError(code)

    article_ids: set[str] = set()
    node_ids: set[UUID] = set()
    main_ids: set[UUID] = set()
    main_days: set[datetime] = set()
    expected_order = list(range(1, len(plan.nodes) + 1))
    if [node.display_order for node in plan.nodes] != expected_order:
        raise TimelineValidationError("timeline_display_order_invalid")
    if list(plan.nodes) != sorted(plan.nodes, key=_node_chronological_day_value):
        raise TimelineValidationError("timeline_chronological_order_invalid")

    for node in plan.nodes:
        _bounded_text(node.article_id, "timeline_article_invalid", 256)
        _bounded_text(node.source_domain, "timeline_domain_invalid", 255)
        if node.article_id in article_ids:
            raise TimelineValidationError("timeline_article_duplicate")
        if node.node_id in node_ids:
            raise TimelineValidationError("timeline_node_duplicate")
        if node.node_id != _node_id(plan.stable_key, node.article_id):
            raise TimelineValidationError("timeline_node_identity_invalid")
        if not math.isfinite(node.relevance_score) or not 0 <= node.relevance_score <= 1:
            raise TimelineValidationError("timeline_score_invalid")
        _aware_utc(node.published_at, "timeline_date_invalid")
        try:
            lane = TimelineLane(node.lane)
            TimelineUpdateKind(node.update_kind)
            TimelineDateSource(node.date_source)
        except ValueError as error:
            raise TimelineValidationError("timeline_node_value_invalid") from error
        if lane is TimelineLane.MAIN:
            if node.anchor_node_id is not None:
                raise TimelineValidationError("timeline_main_anchor_invalid")
            day = _timeline_day(node.published_at, "timeline_date_invalid")
            if day in main_days:
                raise TimelineValidationError("timeline_main_day_duplicate")
            main_days.add(day)
            main_ids.add(node.node_id)
        article_ids.add(node.article_id)
        node_ids.add(node.node_id)

    if len(main_ids) < MIN_MAIN_NODES:
        raise TimelineValidationError("timeline_main_too_small")
    for node in plan.nodes:
        if TimelineLane(node.lane) is TimelineLane.BRANCH:
            if node.anchor_node_id is None or node.anchor_node_id not in main_ids:
                raise TimelineValidationError("timeline_branch_anchor_invalid")
            if node.anchor_node_id == node.node_id:
                raise TimelineValidationError("timeline_branch_anchor_invalid")

    if _timeline_day(plan.started_at, "timeline_period_invalid") != plan.started_at:
        raise TimelineValidationError("timeline_period_invalid")
    if _timeline_day(plan.ended_at, "timeline_period_invalid") != plan.ended_at:
        raise TimelineValidationError("timeline_period_invalid")
    if (
        min(
            _timeline_day(node.published_at, "timeline_period_invalid")
            for node in plan.nodes
        )
        != plan.started_at
    ):
        raise TimelineValidationError("timeline_period_invalid")
    if (
        max(
            _timeline_day(node.published_at, "timeline_period_invalid")
            for node in plan.nodes
        )
        != plan.ended_at
    ):
        raise TimelineValidationError("timeline_period_invalid")
    actual_domains = tuple(sorted({node.source_domain for node in plan.nodes}))
    if len(actual_domains) < 2:
        raise TimelineValidationError("timeline_domain_diversity_failed")
    if plan.source_domains and plan.source_domains != actual_domains:
        raise TimelineValidationError("timeline_domains_invalid")
    members = set(plan.member_article_ids)
    if members and (len(members) != len(plan.member_article_ids) or not article_ids <= members):
        raise TimelineValidationError("timeline_members_invalid")


def _validate_node_bounds(min_nodes: int, max_nodes: int) -> None:
    if not MIN_MAIN_NODES <= min_nodes <= max_nodes <= 8:
        raise TimelineValidationError("timeline_node_bounds_invalid")


def _resolve_candidates(
    candidates: Sequence[TimelineCandidate],
    member_ids: tuple[str, ...],
) -> tuple[_ResolvedCandidate, ...]:
    resolved: list[_ResolvedCandidate] = []
    seen: set[str] = set()
    members = set(member_ids)
    for candidate in candidates:
        article_id = _bounded_text(candidate.article_id, "timeline_article_invalid", 256)
        if article_id in seen:
            raise TimelineValidationError("timeline_candidate_duplicate")
        if members and article_id not in members:
            raise TimelineValidationError("timeline_candidate_unknown")
        domain = _bounded_text(candidate.source_domain, "timeline_domain_invalid", 255).casefold()
        relevance = _score(candidate.relevance_score)
        centrality = _score(candidate.centrality)
        if candidate.published_at is not None:
            occurred_at = _aware_utc(candidate.published_at, "timeline_date_invalid")
            date_source = TimelineDateSource.PROVIDER
        elif candidate.first_seen_at is not None:
            occurred_at = _aware_utc(candidate.first_seen_at, "timeline_date_invalid")
            date_source = TimelineDateSource.FIRST_SEEN
        else:
            raise TimelineValidationError("timeline_date_missing")
        resolved.append(
            _ResolvedCandidate(
                article_id=article_id,
                source_domain=domain,
                relevance_score=relevance,
                centrality=centrality,
                occurred_at=occurred_at,
                date_source=date_source,
                source_hash=_source_hash(candidate.source_hash),
            )
        )
        seen.add(article_id)
    return tuple(resolved)


def _select_timeline_candidates(
    candidates: Sequence[_ResolvedCandidate],
    edges: Sequence[TimelineSimilarityEdge],
    *,
    max_nodes: int,
) -> tuple[tuple[_ResolvedCandidate, ...], frozenset[str]]:
    """Pick a date-unique main spine and content-unique branches.

    The input is the complete bounded rerank window, not merely the number of
    cards that will be published.  That lets the selector replace a same-day
    main or paraphrase with the next eligible event instead of shortening the
    timeline.
    """

    by_id = {item.article_id: item for item in candidates}
    content_groups = _content_groups(candidates, edges)
    main_target = max(MIN_MAIN_NODES, (min(max_nodes, len(candidates)) + 1) // 2)
    main: list[_ResolvedCandidate] = []
    used_days: set[datetime] = set()
    used_groups: set[str] = set()
    for item in sorted(candidates, key=_main_rank_value, reverse=True):
        day = _timeline_day(item.occurred_at, "timeline_date_invalid")
        group = content_groups[item.article_id]
        if day in used_days or group in used_groups:
            continue
        main.append(item)
        used_days.add(day)
        used_groups.add(group)
        if len(main) == main_target:
            break
    if len(main) < MIN_MAIN_NODES:
        raise TimelineValidationError("timeline_main_days_too_few")

    selected = list(main)
    for item in sorted(candidates, key=_rerank_key):
        if item.article_id in {value.article_id for value in selected}:
            continue
        group = content_groups[item.article_id]
        if group in used_groups:
            continue
        selected.append(item)
        used_groups.add(group)
        if len(selected) == max_nodes:
            break

    selected, main_ids = _ensure_domain_diversity(
        selected,
        main_article_ids=frozenset(item.article_id for item in main),
        ranked=sorted(candidates, key=_rerank_key),
        content_groups=content_groups,
    )
    if any(item.article_id not in by_id for item in selected):
        raise TimelineValidationError("timeline_candidate_unknown")
    return tuple(selected), main_ids


def _ensure_domain_diversity(
    selected: Sequence[_ResolvedCandidate],
    *,
    main_article_ids: frozenset[str],
    ranked: Sequence[_ResolvedCandidate],
    content_groups: Mapping[str, str],
) -> tuple[list[_ResolvedCandidate], frozenset[str]]:
    if len({item.source_domain for item in selected}) >= 2:
        return list(selected), main_article_ids
    selected_ids = {item.article_id for item in selected}
    for alternate in ranked:
        if alternate.article_id in selected_ids:
            continue
        for index in range(len(selected) - 1, -1, -1):
            replaced = selected[index]
            if (
                replaced.article_id in main_article_ids
                and _timeline_day(alternate.occurred_at, "timeline_date_invalid")
                != _timeline_day(replaced.occurred_at, "timeline_date_invalid")
            ):
                continue
            proposed = list(selected)
            proposed[index] = alternate
            groups = [content_groups[item.article_id] for item in proposed]
            if len(groups) != len(set(groups)):
                continue
            if len({item.source_domain for item in proposed}) < 2:
                continue
            main_ids = set(main_article_ids)
            if replaced.article_id in main_ids:
                main_ids.remove(replaced.article_id)
                main_ids.add(alternate.article_id)
            return proposed, frozenset(main_ids)
    raise TimelineValidationError("timeline_domain_diversity_failed")


def _content_groups(
    candidates: Sequence[_ResolvedCandidate],
    edges: Sequence[TimelineSimilarityEdge],
) -> dict[str, str]:
    """Return deterministic equivalence groups for exact and semantic copies."""

    parent = {item.article_id: item.article_id for item in candidates}

    def find(value: str) -> str:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    by_hash: dict[str, str] = {}
    for item in sorted(candidates, key=lambda value: value.article_id):
        if not item.source_hash:
            continue
        original = by_hash.setdefault(item.source_hash, item.article_id)
        union(original, item.article_id)

    known = set(parent)
    for edge in edges:
        if (
            edge.left_article_id in known
            and edge.right_article_id in known
            and edge.similarity >= TIMELINE_CONTENT_DUPLICATE_SIMILARITY
        ):
            union(edge.left_article_id, edge.right_article_id)
    return {article_id: find(article_id) for article_id in parent}


def _match_previous(
    member_ids: tuple[str, ...],
    previous: TimelinePlan | Sequence[TimelinePlan] | None,
) -> TimelinePlan | None:
    if previous is None:
        return None
    plans = (previous,) if isinstance(previous, TimelinePlan) else tuple(previous)
    current = set(member_ids)
    matches: list[tuple[float, str, TimelinePlan]] = []
    for plan in plans:
        prior = set(plan.member_article_ids) or {node.article_id for node in plan.nodes}
        union = current | prior
        score = len(current & prior) / len(union) if union else 0.0
        if score >= 0.40:
            matches.append((score, plan.stable_key, plan))
    if not matches:
        return None
    matches.sort(key=lambda item: (-item[0], item[1]))
    matched = matches[0][2]
    _stable_uuid(matched.stable_key)
    return matched


def _similarity_map(
    edges: Sequence[TimelineSimilarityEdge],
) -> dict[tuple[str, str], float]:
    similarities: dict[tuple[str, str], float] = {}
    for edge in edges:
        left = _bounded_text(edge.left_article_id, "timeline_edge_invalid", 256)
        right = _bounded_text(edge.right_article_id, "timeline_edge_invalid", 256)
        if left == right:
            raise TimelineValidationError("timeline_edge_invalid")
        similarity = _score(edge.similarity)
        key = tuple(sorted((left, right)))
        if key in similarities:
            raise TimelineValidationError("timeline_edge_duplicate")
        similarities[key] = similarity
    return similarities


def _best_anchor(
    branch_id: str,
    main_spine: Sequence[_ResolvedCandidate],
    *,
    main_order: dict[str, int],
    node_ids: dict[str, UUID],
    similarities: dict[tuple[str, str], float],
) -> _ResolvedCandidate:
    choices: list[tuple[float, int, str, _ResolvedCandidate]] = []
    for main in main_spine:
        similarity = similarities.get(tuple(sorted((branch_id, main.article_id))))
        if similarity is not None:
            choices.append(
                (-similarity, main_order[main.article_id], str(node_ids[main.article_id]), main)
            )
    if not choices:
        raise TimelineValidationError("timeline_branch_similarity_missing")
    choices.sort(key=lambda item: (item[0], item[1], item[2]))
    return choices[0][3]


def _update_kind(
    candidate: _ResolvedCandidate,
    *,
    previous_by_article: dict[str, TimelineNode],
    previous_latest_day: datetime | None,
) -> TimelineUpdateKind:
    if not previous_by_article:
        return TimelineUpdateKind.EXISTING
    if candidate.article_id in previous_by_article:
        return TimelineUpdateKind.EXISTING
    candidate_day = _timeline_day(candidate.occurred_at, "timeline_date_invalid")
    if previous_latest_day is not None and candidate_day > previous_latest_day:
        return TimelineUpdateKind.OCCURRED
    return TimelineUpdateKind.INSERTED


def _member_ids(raw_ids: Sequence[str]) -> tuple[str, ...]:
    values = tuple(_bounded_text(value, "timeline_member_invalid", 256) for value in raw_ids)
    if len(values) != len(set(values)):
        raise TimelineValidationError("timeline_member_duplicate")
    if not values:
        raise TimelineValidationError("timeline_members_empty")
    return tuple(sorted(values))


def _new_stable_key(cluster_key: str) -> str:
    return str(uuid5(TIMELINE_ID_NAMESPACE, f"timeline:{cluster_key}"))


def _stable_uuid(stable_key: str) -> UUID:
    try:
        value = UUID(stable_key)
    except (TypeError, ValueError, AttributeError) as error:
        raise TimelineValidationError("timeline_stable_key_invalid") from error
    if value.version != 5:
        raise TimelineValidationError("timeline_stable_key_invalid")
    return value


def _node_id(stable_key: str, article_id: str) -> UUID:
    return uuid5(TIMELINE_ID_NAMESPACE, f"node:{stable_key}:{article_id}")


def _score(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TimelineValidationError("timeline_score_invalid")
    normalized = float(value)
    if not math.isfinite(normalized) or not 0 <= normalized <= 1:
        raise TimelineValidationError("timeline_score_invalid")
    return normalized


def _aware_utc(value: datetime, code: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise TimelineValidationError(code)
    return value.astimezone(UTC)


def _source_hash(value: str) -> str:
    if not isinstance(value, str):
        raise TimelineValidationError("timeline_source_hash_invalid")
    normalized = value.strip().lower()
    if not normalized:
        return ""
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        raise TimelineValidationError("timeline_source_hash_invalid")
    return normalized


def _bounded_text(value: str, code: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise TimelineValidationError(code)
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > maximum or any(ord(char) < 32 for char in normalized):
        raise TimelineValidationError(code)
    return normalized


def _rerank_key(item: _ResolvedCandidate) -> tuple[float, str]:
    return (-item.relevance_score, item.article_id)


def _main_rank_value(item: _ResolvedCandidate) -> tuple[float, float, float, float, str]:
    return (
        item.relevance_score + item.centrality,
        item.relevance_score,
        item.centrality,
        _timeline_day(item.occurred_at, "timeline_date_invalid").timestamp(),
        item.article_id,
    )


def _latest_day_value(item: _ResolvedCandidate) -> tuple[float, str]:
    return (_timeline_day(item.occurred_at, "timeline_date_invalid").timestamp(), item.article_id)


def _chronological_day_value(item: _ResolvedCandidate) -> tuple[float, str]:
    return (_timeline_day(item.occurred_at, "timeline_date_invalid").timestamp(), item.article_id)


def _node_chronological_day_value(node: TimelineNode) -> tuple[float, str]:
    return (_timeline_day(node.published_at, "timeline_date_invalid").timestamp(), node.article_id)


def _timeline_day(value: datetime, code: str) -> datetime:
    """Return the UTC instant for the start of ``value``'s KST calendar day."""

    local = _aware_utc(value, code).astimezone(TIMELINE_TIME_ZONE)
    return datetime(
        local.year,
        local.month,
        local.day,
        tzinfo=TIMELINE_TIME_ZONE,
    ).astimezone(UTC)


def _timeline_date_text(value: datetime) -> str:
    local = _aware_utc(value, "timeline_date_invalid").astimezone(TIMELINE_TIME_ZONE)
    return f"{local.month}/{local.day}"
