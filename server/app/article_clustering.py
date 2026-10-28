"""Deterministic clustering for the derived article-vector namespace.

The module deliberately has no database, provider, account, or logging code.
Callers supply a bounded set of public article candidates and directed k-nearest
neighbour edges.  Only mutual edges participate in clustering, and every
connected component is split again around a directly-similar medoid so a long
similarity chain cannot turn into one misleading major topic.
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo


CLUSTER_ALGORITHM_VERSION = "mutual-knn-medoid-v2-date"
CLUSTER_TIME_ZONE = ZoneInfo("Asia/Seoul")
MAX_CLUSTER_CANDIDATES = 500
MAX_CLUSTER_NEIGHBORS = 8
MAX_CLUSTER_EDGES = MAX_CLUSTER_CANDIDATES * MAX_CLUSTER_NEIGHBORS
DEFAULT_MEDOID_ASSIGNMENT_SIMILARITY = 0.65
_SOURCE_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ArticleVectorCandidate:
    article_id: str
    source_hash: str
    domain: str
    published_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.article_id, str) or not self.article_id:
            raise ValueError("cluster candidate article_id must not be empty")
        if _SOURCE_HASH_RE.fullmatch(self.source_hash) is None:
            raise ValueError("cluster candidate source_hash must be lowercase sha256")
        if not isinstance(self.domain, str):
            raise ValueError("cluster candidate domain must be text")
        _aware(self.published_at, "cluster candidate published_at")


@dataclass(frozen=True, slots=True)
class ArticleVectorEdge:
    source_article_id: str
    target_article_id: str
    similarity: float

    def __post_init__(self) -> None:
        if (
            not isinstance(self.source_article_id, str)
            or not self.source_article_id
            or not isinstance(self.target_article_id, str)
            or not self.target_article_id
            or self.source_article_id == self.target_article_id
        ):
            raise ValueError("cluster edge endpoints must be distinct article IDs")
        if (
            isinstance(self.similarity, bool)
            or not isinstance(self.similarity, (int, float))
            or not math.isfinite(float(self.similarity))
            or not 0.0 <= float(self.similarity) <= 1.0
        ):
            raise ValueError("cluster edge similarity must be finite and between zero and one")


@dataclass(frozen=True, slots=True)
class ArticleClusterMember:
    article_id: str
    similarity_to_medoid: float
    centrality_rank: int


@dataclass(frozen=True, slots=True)
class ArticleCluster:
    cluster_key: str
    rank: int
    medoid_article_id: str
    members: tuple[ArticleClusterMember, ...]
    domain_count: int
    first_published_at: datetime
    last_published_at: datetime
    mean_edge_similarity: float
    major_score: float

    @property
    def member_count(self) -> int:
        return len(self.members)


@dataclass(frozen=True, slots=True)
class ClusterPlan:
    algorithm_version: str
    article_count: int
    indexed_count: int
    clusters: tuple[ArticleCluster, ...]

    @property
    def coverage(self) -> float:
        if self.article_count <= 0:
            return 0.0
        return round(self.indexed_count / self.article_count, 6)

    def with_population(self, *, article_count: int, indexed_count: int) -> "ClusterPlan":
        """Attach worker-observed pool coverage without rebuilding topology."""
        updated = replace(
            self,
            article_count=article_count,
            indexed_count=indexed_count,
        )
        validate_cluster_plan(updated)
        return updated


class _UnionFind:
    def __init__(self, values: Sequence[str]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        root = value
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[value] != value:
            parent = self.parent[value]
            self.parent[value] = root
            value = parent
        return root

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return
        first, second = sorted((left_root, right_root))
        self.parent[second] = first


def build_article_clusters(
    candidates: Sequence[ArticleVectorCandidate],
    edges: Sequence[ArticleVectorEdge],
    *,
    min_members: int = 3,
    min_domains: int = 2,
    max_clusters: int = 8,
) -> ClusterPlan:
    """Build stable major clusters from a bounded directed kNN graph."""
    if (
        isinstance(candidates, (str, bytes))
        or isinstance(edges, (str, bytes))
        or not isinstance(candidates, Sequence)
        or not isinstance(edges, Sequence)
        or len(candidates) > MAX_CLUSTER_CANDIDATES
        or len(edges) > MAX_CLUSTER_EDGES
        or isinstance(min_members, bool)
        or not isinstance(min_members, int)
        or not 2 <= min_members <= 20
        or isinstance(min_domains, bool)
        or not isinstance(min_domains, int)
        or not 1 <= min_domains <= 20
        or isinstance(max_clusters, bool)
        or not isinstance(max_clusters, int)
        or not 1 <= max_clusters <= 8
    ):
        raise ValueError("invalid article clustering bounds")

    candidate_by_id: dict[str, ArticleVectorCandidate] = {}
    for candidate in candidates:
        if not isinstance(candidate, ArticleVectorCandidate):
            raise TypeError("candidates must contain ArticleVectorCandidate values")
        if candidate.article_id in candidate_by_id:
            raise ValueError("cluster candidate article IDs must be unique")
        candidate_by_id[candidate.article_id] = candidate

    directed: dict[tuple[str, str], float] = {}
    for edge in edges:
        if not isinstance(edge, ArticleVectorEdge):
            raise TypeError("edges must contain ArticleVectorEdge values")
        if (
            edge.source_article_id not in candidate_by_id
            or edge.target_article_id not in candidate_by_id
        ):
            raise ValueError("cluster edge references an unknown candidate")
        key = (edge.source_article_id, edge.target_article_id)
        directed[key] = max(directed.get(key, 0.0), float(edge.similarity))

    adjacency: dict[str, dict[str, float]] = {
        article_id: {} for article_id in candidate_by_id
    }
    for (source, target), similarity in sorted(directed.items()):
        reverse = directed.get((target, source))
        if reverse is None:
            continue
        mutual_similarity = min(similarity, reverse)
        adjacency[source][target] = mutual_similarity
        adjacency[target][source] = mutual_similarity

    components = _connected_components(tuple(sorted(candidate_by_id)), adjacency)
    refined: list[set[str]] = []
    for component in components:
        refined.extend(_split_chain_component(component, adjacency))

    newest_input = max(
        (
            cluster_day(candidate.published_at, "cluster candidate published_at")
            for candidate in candidate_by_id.values()
        ),
        default=None,
    )
    clusters: list[ArticleCluster] = []
    for component in refined:
        if len(component) < min_members:
            continue
        domains = {
            _normalized_domain(candidate_by_id[article_id].domain)
            for article_id in component
            if _normalized_domain(candidate_by_id[article_id].domain)
        }
        if len(domains) < min_domains:
            continue
        clusters.append(
            _build_cluster(
                component,
                candidate_by_id,
                adjacency,
                newest_input=newest_input,
            )
        )

    clusters.sort(key=lambda item: (-item.major_score, item.medoid_article_id))
    ranked = tuple(
        replace(cluster, rank=rank)
        for rank, cluster in enumerate(clusters[:max_clusters], start=1)
    )
    plan = ClusterPlan(
        algorithm_version=CLUSTER_ALGORITHM_VERSION,
        article_count=len(candidate_by_id),
        indexed_count=len(candidate_by_id),
        clusters=ranked,
    )
    validate_cluster_plan(plan)
    return plan


def validate_cluster_plan(plan: ClusterPlan) -> None:
    if not isinstance(plan, ClusterPlan):
        raise TypeError("plan must be a ClusterPlan")
    if (
        plan.algorithm_version != CLUSTER_ALGORITHM_VERSION
        or isinstance(plan.article_count, bool)
        or not isinstance(plan.article_count, int)
        or not 0 <= plan.article_count <= MAX_CLUSTER_CANDIDATES
        or isinstance(plan.indexed_count, bool)
        or not isinstance(plan.indexed_count, int)
        or not 0 <= plan.indexed_count <= plan.article_count
        or not 0 <= len(plan.clusters) <= 8
    ):
        raise ValueError("invalid cluster plan envelope")

    expected_ranks = list(range(1, len(plan.clusters) + 1))
    if [cluster.rank for cluster in plan.clusters] != expected_ranks:
        raise ValueError("cluster ranks must be contiguous")
    if list(plan.clusters) != sorted(
        plan.clusters,
        key=lambda item: (-item.major_score, item.medoid_article_id),
    ):
        raise ValueError("clusters must use deterministic major-score order")

    seen_keys: set[str] = set()
    seen_articles: set[str] = set()
    for cluster in plan.clusters:
        if (
            not isinstance(cluster, ArticleCluster)
            or _SOURCE_HASH_RE.fullmatch(cluster.cluster_key) is None
            or cluster.cluster_key in seen_keys
            or not 3 <= cluster.member_count <= MAX_CLUSTER_CANDIDATES
            or not 2 <= cluster.domain_count <= cluster.member_count
            or cluster.medoid_article_id
            not in {member.article_id for member in cluster.members}
            or not 0.0 <= cluster.mean_edge_similarity <= 1.0
            or not 0.0 <= cluster.major_score <= 1.0
        ):
            raise ValueError("invalid article cluster")
        _aware(cluster.first_published_at, "cluster first_published_at")
        _aware(cluster.last_published_at, "cluster last_published_at")
        if cluster.first_published_at > cluster.last_published_at:
            raise ValueError("cluster publication range is reversed")
        ranks = [member.centrality_rank for member in cluster.members]
        if ranks != list(range(1, len(cluster.members) + 1)):
            raise ValueError("cluster member centrality ranks must be contiguous")
        for member in cluster.members:
            if (
                not isinstance(member, ArticleClusterMember)
                or member.article_id in seen_articles
                or not 0.0 <= member.similarity_to_medoid <= 1.0
            ):
                raise ValueError("invalid cluster member")
            seen_articles.add(member.article_id)
        seen_keys.add(cluster.cluster_key)
    if len(seen_articles) > plan.indexed_count:
        raise ValueError("cluster members cannot exceed indexed population")


def _connected_components(
    values: Sequence[str],
    adjacency: Mapping[str, Mapping[str, float]],
) -> list[set[str]]:
    allowed = set(values)
    union = _UnionFind(values)
    for source in sorted(values):
        for target in sorted(adjacency[source]):
            if target in allowed:
                union.union(source, target)
    grouped: dict[str, set[str]] = {}
    for value in sorted(values):
        grouped.setdefault(union.find(value), set()).add(value)
    return sorted(grouped.values(), key=lambda group: tuple(sorted(group)))


def _split_chain_component(
    component: set[str],
    adjacency: Mapping[str, Mapping[str, float]],
) -> list[set[str]]:
    pending = [set(component)]
    output: list[set[str]] = []
    while pending:
        group = pending.pop(0)
        if len(group) <= 1:
            output.append(group)
            continue
        medoid = _medoid(group, adjacency)
        assigned = {medoid}
        assigned.update(
            article_id
            for article_id in group
            if article_id != medoid
            and adjacency[medoid].get(article_id, 0.0)
            >= DEFAULT_MEDOID_ASSIGNMENT_SIMILARITY
        )
        if assigned == group:
            output.append(group)
            continue
        output.append(assigned)
        remainder = group - assigned
        pending.extend(
            _connected_components(tuple(sorted(remainder)), adjacency)
        )
        pending.sort(key=lambda values: tuple(sorted(values)))
    return sorted(output, key=lambda group: tuple(sorted(group)))


def _medoid(
    component: set[str],
    adjacency: Mapping[str, Mapping[str, float]],
) -> str:
    return min(
        component,
        key=lambda article_id: (
            -sum(
                adjacency[article_id].get(other, 0.0)
                for other in component
                if other != article_id
            ),
            article_id,
        ),
    )


def _build_cluster(
    component: set[str],
    candidates: Mapping[str, ArticleVectorCandidate],
    adjacency: Mapping[str, Mapping[str, float]],
    *,
    newest_input: datetime | None,
) -> ArticleCluster:
    medoid = _medoid(component, adjacency)
    centrality = {
        article_id: sum(
            adjacency[article_id].get(other, 0.0)
            for other in component
            if other != article_id
        )
        for article_id in component
    }
    central_order = sorted(component, key=lambda value: (-centrality[value], value))
    members = tuple(
        ArticleClusterMember(
            article_id=article_id,
            similarity_to_medoid=(
                1.0 if article_id == medoid else adjacency[medoid][article_id]
            ),
            centrality_rank=rank,
        )
        for rank, article_id in enumerate(central_order, start=1)
    )
    dates = [
        cluster_day(
            candidates[article_id].published_at,
            "cluster candidate published_at",
        )
        for article_id in component
    ]
    domains = {
        _normalized_domain(candidates[article_id].domain)
        for article_id in component
        if _normalized_domain(candidates[article_id].domain)
    }
    similarities = [
        adjacency[source][target]
        for source in sorted(component)
        for target in sorted(adjacency[source])
        if source < target and target in component
    ]
    mean_edge = sum(similarities) / len(similarities) if similarities else 0.0
    latest = max(dates)
    age_days = (
        max(0.0, (newest_input - latest).total_seconds()) / 86_400.0
        if newest_input is not None
        else 30.0
    )
    size_score = min(1.0, len(component) / 20.0)
    diversity_score = min(1.0, len(domains) / 8.0)
    recency_score = max(0.0, 1.0 - age_days / 30.0)
    major_score = round(
        0.45 * size_score
        + 0.20 * diversity_score
        + 0.20 * recency_score
        + 0.15 * mean_edge,
        6,
    )
    identity = "\0".join(sorted(component)).encode("utf-8")
    cluster_key = hashlib.sha256(
        CLUSTER_ALGORITHM_VERSION.encode("ascii") + b"\0" + identity
    ).hexdigest()
    return ArticleCluster(
        cluster_key=cluster_key,
        rank=0,
        medoid_article_id=medoid,
        members=members,
        domain_count=len(domains),
        first_published_at=min(dates),
        last_published_at=latest,
        mean_edge_similarity=round(mean_edge, 6),
        major_score=major_score,
    )


def _normalized_domain(value: str) -> str:
    return value.strip().casefold()


def _aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def cluster_day(value: datetime, code: str) -> datetime:
    """Return the UTC instant at the beginning of ``value``'s KST date."""

    local = _aware(value, code).astimezone(CLUSTER_TIME_ZONE)
    return datetime(
        local.year,
        local.month,
        local.day,
        tzinfo=CLUSTER_TIME_ZONE,
    ).astimezone(UTC)
