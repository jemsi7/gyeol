"""Deterministic domain logic for trending search suggestions.

This module deliberately contains no database, network, cache, or logging code.
The repository supplies three already-ranked article categories; the functions
here normalize their Map keywords, build a globally unique pool, and select a
stable sample for one five-minute UTC bucket.
"""
from __future__ import annotations

import hashlib
import math
import random
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Mapping, Sequence


MAX_ARTICLES_PER_CATEGORY = 10
MAX_KEYWORDS_PER_ARTICLE = 12
MAX_KEYWORDS_PER_CATEGORY = 3
MAX_KEYWORD_POOL = 9
MAX_SUGGESTIONS = 3
SNAPSHOT_BUCKET_SECONDS = 300


class TrendingMetric(StrEnum):
    VIEW_TOTAL = "view_total"
    VIEW_GROWTH = "view_growth"
    BOOKMARK_TOTAL = "bookmark_total"


TRENDING_METRIC_ORDER = (
    TrendingMetric.VIEW_TOTAL,
    TrendingMetric.VIEW_GROWTH,
    TrendingMetric.BOOKMARK_TOTAL,
)


@dataclass(frozen=True, slots=True)
class TrendingArticle:
    metric: TrendingMetric
    article_id: str
    rank: int
    metric_value: float
    recent_count: int
    previous_count: int
    keywords: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.metric, TrendingMetric):
            raise ValueError("metric must be a TrendingMetric")
        if not isinstance(self.article_id, str) or not 1 <= len(self.article_id) <= 128:
            raise ValueError("article_id must be bounded non-empty text")
        if (
            not isinstance(self.rank, int)
            or isinstance(self.rank, bool)
            or not 1 <= self.rank <= MAX_ARTICLES_PER_CATEGORY
        ):
            raise ValueError("rank must be between one and ten")
        if (
            not isinstance(self.metric_value, (int, float))
            or isinstance(self.metric_value, bool)
            or not math.isfinite(float(self.metric_value))
        ):
            raise ValueError("metric_value must be finite")
        if self.metric == TrendingMetric.VIEW_GROWTH:
            if float(self.metric_value) < -1.0:
                raise ValueError("view growth cannot be below negative one")
        elif float(self.metric_value) < 0.0:
            raise ValueError("total metrics cannot be negative")
        for label, count in (
            ("recent_count", self.recent_count),
            ("previous_count", self.previous_count),
        ):
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise ValueError(f"{label} must be a non-negative integer")
        if not isinstance(self.keywords, tuple):
            raise ValueError("keywords must be a tuple")
        if len(self.keywords) > MAX_KEYWORDS_PER_ARTICLE or any(
            not isinstance(keyword, str) for keyword in self.keywords
        ):
            raise ValueError("keywords must contain at most twelve strings")


@dataclass(frozen=True, slots=True)
class TrendingSearchSnapshot:
    bucket_started_at: datetime
    category_keywords: Mapping[TrendingMetric, tuple[str, ...]]
    keyword_pool: tuple[str, ...]
    suggestions: tuple[str, ...]
    generated_at: datetime

    def __post_init__(self) -> None:
        _require_aware(self.bucket_started_at, "bucket_started_at")
        _require_aware(self.generated_at, "generated_at")
        if trending_bucket_started_at(self.bucket_started_at) != self.bucket_started_at:
            raise ValueError("bucket_started_at must be on a five-minute UTC boundary")
        if not isinstance(self.category_keywords, Mapping):
            raise ValueError("category_keywords must be a mapping")
        for metric, keywords in self.category_keywords.items():
            if not isinstance(metric, TrendingMetric) or not isinstance(keywords, tuple):
                raise ValueError("category keywords must use metric keys and tuple values")
            if len(keywords) > MAX_KEYWORDS_PER_CATEGORY:
                raise ValueError("a category may contain at most three keywords")
            _normalized_unique_keywords(keywords)
        if not isinstance(self.keyword_pool, tuple) or len(self.keyword_pool) > MAX_KEYWORD_POOL:
            raise ValueError("keyword_pool must be a tuple with at most nine values")
        if not isinstance(self.suggestions, tuple) or len(self.suggestions) > MAX_SUGGESTIONS:
            raise ValueError("suggestions must be a tuple with at most three values")
        pool = _normalized_unique_keywords(self.keyword_pool)
        suggestions = _normalized_unique_keywords(self.suggestions)
        if not set(suggestions).issubset(pool):
            raise ValueError("suggestions must come from keyword_pool")


@dataclass(slots=True)
class _KeywordAggregate:
    score: int
    best_rank: int
    display_rank: int
    display: str


def normalize_keyword(value: str) -> tuple[str, str] | None:
    """Return a comparison identity and preserved display form for one keyword."""
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFKC", value)
    if any(unicodedata.category(character).startswith("C") for character in normalized):
        return None
    display = " ".join(normalized.split())
    identity = display.casefold()
    if not 2 <= len(display) <= 60 or not 2 <= len(identity) <= 60:
        return None
    return identity, display


def view_growth_score(
    recent_count: int,
    previous_count: int,
    *,
    min_growth_events: int = 5,
) -> float | None:
    """Apply the agreed adjacent-window growth formula to bounded event counts."""
    for label, count in (("recent_count", recent_count), ("previous_count", previous_count)):
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError(f"{label} must be a non-negative integer")
    if (
        not isinstance(min_growth_events, int)
        or isinstance(min_growth_events, bool)
        or min_growth_events < 1
    ):
        raise ValueError("min_growth_events must be positive")
    if recent_count + previous_count < min_growth_events:
        return None
    return (recent_count - previous_count) / max(previous_count, 1)


def rank_category_keywords(
    articles: Sequence[TrendingArticle],
) -> tuple[str, ...]:
    """Rank all valid keywords in one article category by article-rank weight."""
    if isinstance(articles, (str, bytes)) or not isinstance(articles, Sequence):
        raise ValueError("articles must be a sequence")
    if len(articles) > MAX_ARTICLES_PER_CATEGORY:
        raise ValueError("a category may contain at most ten articles")
    if any(not isinstance(article, TrendingArticle) for article in articles):
        raise ValueError("articles must contain TrendingArticle values")
    if len({article.article_id for article in articles}) != len(articles):
        raise ValueError("article IDs must be unique within a category")
    if len({article.rank for article in articles}) != len(articles):
        raise ValueError("article ranks must be unique within a category")
    if len({article.metric for article in articles}) > 1:
        raise ValueError("a category cannot mix metrics")

    aggregates: dict[str, _KeywordAggregate] = {}
    for article in articles:
        weight = max(1, 11 - article.rank)
        seen_in_article: set[str] = set()
        for raw_keyword in article.keywords:
            normalized = normalize_keyword(raw_keyword)
            if normalized is None:
                continue
            identity, display = normalized
            if identity in seen_in_article:
                continue
            seen_in_article.add(identity)
            aggregate = aggregates.get(identity)
            if aggregate is None:
                aggregates[identity] = _KeywordAggregate(
                    score=weight,
                    best_rank=article.rank,
                    display_rank=article.rank,
                    display=display,
                )
                continue
            aggregate.score += weight
            aggregate.best_rank = min(aggregate.best_rank, article.rank)
            if article.rank < aggregate.display_rank:
                aggregate.display_rank = article.rank
                aggregate.display = display

    ranked = sorted(
        aggregates.values(),
        key=lambda value: (
            -value.score,
            value.best_rank,
            value.display.casefold(),
            value.display,
        ),
    )
    return tuple(value.display for value in ranked)


def build_keyword_pool(
    rankings: Mapping[TrendingMetric, Sequence[TrendingArticle]],
    *,
    per_category: int = MAX_KEYWORDS_PER_CATEGORY,
) -> tuple[str, ...]:
    """Build the ordered, cross-category unique pool with deterministic backfill."""
    if not isinstance(rankings, Mapping):
        raise ValueError("rankings must be a mapping")
    if (
        not isinstance(per_category, int)
        or isinstance(per_category, bool)
        or not 1 <= per_category <= MAX_KEYWORDS_PER_CATEGORY
    ):
        raise ValueError("per_category must be between one and three")

    pool: list[str] = []
    seen_identities: set[str] = set()
    for metric in TRENDING_METRIC_ORDER:
        articles = rankings.get(metric, ())
        if any(article.metric != metric for article in articles):
            raise ValueError("ranking values must match their metric key")
        added = 0
        for display in rank_category_keywords(articles):
            identity, _ = normalize_keyword(display) or (None, None)
            if identity is None or identity in seen_identities:
                continue
            seen_identities.add(identity)
            pool.append(display)
            added += 1
            if added >= per_category:
                break
    return tuple(pool)


def trending_bucket_started_at(as_of: datetime) -> datetime:
    """Floor an aware instant to its five-minute UTC snapshot bucket."""
    _require_aware(as_of, "as_of")
    epoch_seconds = int(as_of.astimezone(UTC).timestamp())
    bucket_epoch = epoch_seconds // SNAPSHOT_BUCKET_SECONDS * SNAPSHOT_BUCKET_SECONDS
    return datetime.fromtimestamp(bucket_epoch, tz=UTC)


def sample_keyword_pool(
    pool: Sequence[str],
    *,
    bucket_started_at: datetime,
    limit: int = MAX_SUGGESTIONS,
) -> tuple[str, ...]:
    """Select a unique sample that is stable for a UTC five-minute bucket."""
    if isinstance(pool, (str, bytes)) or not isinstance(pool, Sequence):
        raise ValueError("pool must be a sequence")
    if len(pool) > MAX_KEYWORD_POOL:
        raise ValueError("pool may contain at most nine values")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 0 <= limit <= MAX_SUGGESTIONS:
        raise ValueError("limit must be between zero and three")

    displays: list[str] = []
    identities: list[str] = []
    seen: set[str] = set()
    for raw_keyword in pool:
        normalized = normalize_keyword(raw_keyword)
        if normalized is None:
            continue
        identity, display = normalized
        if identity in seen:
            continue
        seen.add(identity)
        identities.append(identity)
        displays.append(display)
    if not displays or limit == 0:
        return ()

    bucket = trending_bucket_started_at(bucket_started_at)
    bucket_text = bucket.isoformat(timespec="seconds")
    seed_material = (bucket_text + "\0" + "\0".join(identities)).encode("utf-8")
    seed = int.from_bytes(hashlib.sha256(seed_material).digest(), "big")
    indexes = random.Random(seed).sample(range(len(displays)), k=min(limit, len(displays)))
    return tuple(displays[index] for index in indexes)


def _require_aware(value: datetime, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


def _normalized_unique_keywords(values: Sequence[str]) -> set[str]:
    identities: set[str] = set()
    for value in values:
        normalized = normalize_keyword(value)
        if normalized is None:
            raise ValueError("snapshot keywords must be valid")
        identity = normalized[0]
        if identity in identities:
            raise ValueError("snapshot keywords must be unique")
        identities.add(identity)
    return identities
