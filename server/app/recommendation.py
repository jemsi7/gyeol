"""Deterministic, privacy-bounded article recommendation domain pipeline.

The module deliberately contains no database, network, account, or logging
code.  A request-scoped profile is hydrated from bounded raw rows, used to
rank at most a small candidate pool, and then discarded by the caller.
"""
from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any, Mapping, Sequence


RANKING_VERSION = "content-v1"
ARTICLE_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
TERM_PATTERN = re.compile(r"[0-9a-z가-힣]{2,48}")
PROFILE_WINDOW = timedelta(days=180)
SHORT_PROFILE_WINDOW = timedelta(days=7)
RECENTLY_SEEN_WINDOW = timedelta(days=30)
PROFILE_EVENT_LIMIT = 500


class RecommendationSurface(StrEnum):
    HOME = "home"
    ARTICLE_BOUNDARY = "article_boundary"


class Daypart(StrEnum):
    NIGHT = "night"
    MORNING = "morning"
    AFTERNOON = "afternoon"
    EVENING = "evening"


@dataclass(frozen=True, slots=True)
class ArticleRecommendationFeature:
    article_id: str
    terms: tuple[str, ...]
    term_weights: Mapping[str, float]
    representative_topic: str | None
    domain: str | None
    published_at: datetime | None
    quality_score: float
    headline: str = ""

    def __post_init__(self) -> None:
        if ARTICLE_ID_PATTERN.fullmatch(self.article_id) is None:
            raise ValueError("article_id must be a lowercase sha256")
        if len(self.terms) > 64 or len(set(self.terms)) != len(self.terms):
            raise ValueError("feature terms must be unique and bounded")
        if any(term not in self.term_weights for term in self.terms):
            raise ValueError("every feature term must have a weight")
        if any(
            not isinstance(weight, (int, float))
            or isinstance(weight, bool)
            or not math.isfinite(float(weight))
            or float(weight) <= 0
            for weight in self.term_weights.values()
        ):
            raise ValueError("feature weights must be positive and finite")
        if not math.isfinite(self.quality_score) or not 0 <= self.quality_score <= 1:
            raise ValueError("quality_score must be between zero and one")
        if self.published_at is not None:
            _aware(self.published_at, "published_at")


@dataclass(frozen=True, slots=True)
class RecommendationProfile:
    global_terms: Mapping[str, float]
    daypart_terms: Mapping[str, float]
    domain_weights: Mapping[str, float]
    negative_terms: Mapping[str, float]
    excluded_article_ids: frozenset[str]
    recently_seen_article_ids: frozenset[str]
    meaningful_event_count: int
    snapshot_at: datetime
    current_daypart: Daypart
    short_terms: Mapping[str, float] = field(default_factory=dict)
    saved_terms: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RecommendationCandidate:
    feature: ArticleRecommendationFeature
    sources: frozenset[str] = frozenset()

    @property
    def article_id(self) -> str:
        return self.feature.article_id


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    global_interest: float
    daypart_interest: float
    context_similarity: float
    freshness: float
    quality: float
    source_affinity: float
    negative_penalty: float
    confidence: float
    base_score: float
    score_micros: int


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    candidate: RecommendationCandidate
    score: ScoreBreakdown
    representative_topic: str | None


@dataclass(frozen=True, slots=True)
class RecommendationSortKey:
    score_micros: int
    published_at: datetime
    article_id: str

    def __post_init__(self) -> None:
        _aware(self.published_at, "published_at")
        if ARTICLE_ID_PATTERN.fullmatch(self.article_id) is None:
            raise ValueError("cursor article_id must be a lowercase sha256")


@dataclass(frozen=True, slots=True)
class RankedRecommendationPage:
    items: tuple[ScoredCandidate, ...]
    next_sort_key: RecommendationSortKey | None
    strategy: str
    invalid_candidate_count: int = 0


def daypart_for_hour(hour: int) -> Daypart:
    if not 0 <= hour <= 23:
        raise ValueError("local hour must be between 0 and 23")
    if hour <= 5:
        return Daypart.NIGHT
    if hour <= 11:
        return Daypart.MORNING
    if hour <= 17:
        return Daypart.AFTERNOON
    return Daypart.EVENING


def normalize_terms(text: str, *, limit: int = 64) -> tuple[str, ...]:
    if not isinstance(text, str):
        raise ValueError("term source must be text")
    if not 1 <= limit <= 128:
        raise ValueError("term limit is out of range")
    normalized = " ".join(unicodedata.normalize("NFKC", text).casefold().split())
    terms: list[str] = []
    seen: set[str] = set()
    if 2 <= len(normalized) <= 48 and TERM_PATTERN.fullmatch(normalized):
        seen.add(normalized)
        terms.append(normalized)
    for term in TERM_PATTERN.findall(normalized):
        if term not in seen:
            seen.add(term)
            terms.append(term)
        if len(terms) >= limit:
            break
    return tuple(terms[:limit])


def extract_article_recommendation_feature(
    *,
    article_id: str,
    map_data: Mapping[str, Any],
    domain: str | None,
    published_at: datetime | None,
    status: str,
) -> ArticleRecommendationFeature:
    if status not in {"ok", "degraded"}:
        raise ValueError("only readable maps have recommendation features")
    if not isinstance(map_data, Mapping):
        raise ValueError("map_data must be an object")

    headline = _bounded_text(map_data.get("headline"))
    subheadline = _bounded_text(map_data.get("subheadline"))
    summary = _bounded_text(map_data.get("summary"))
    keywords = map_data.get("keywords")
    scenes = map_data.get("scenes")
    weighted_sources: list[tuple[str, float]] = []
    if isinstance(keywords, Sequence) and not isinstance(keywords, (str, bytes)):
        weighted_sources.extend(
            (value, 1.0)
            for value in keywords
            if isinstance(value, str) and value.strip()
        )
    if headline:
        weighted_sources.append((headline, 1.0))
    if subheadline:
        weighted_sources.append((subheadline, 0.7))
    if summary:
        weighted_sources.append((summary, 0.7))
    valid_scenes = 0
    if isinstance(scenes, Sequence) and not isinstance(scenes, (str, bytes)):
        for scene in scenes[:20]:
            if not isinstance(scene, Mapping):
                continue
            valid_scenes += 1
            for field in ("title", "summary"):
                value = _bounded_text(scene.get(field))
                if value:
                    weighted_sources.append((value, 0.4))

    ordered_terms: list[str] = []
    term_weights: dict[str, float] = {}
    for source, weight in weighted_sources:
        for term in normalize_terms(source):
            if term not in term_weights and len(ordered_terms) < 64:
                ordered_terms.append(term)
            if term in ordered_terms:
                term_weights[term] = max(term_weights.get(term, 0.0), weight)

    keyword_topic = None
    if isinstance(keywords, Sequence) and not isinstance(keywords, (str, bytes)):
        for value in keywords:
            if isinstance(value, str):
                normalized = normalize_terms(value, limit=1)
                if normalized:
                    keyword_topic = normalized[0]
                    break
    representative_topic = keyword_topic or (
        normalize_terms(headline, limit=1)[0]
        if headline and normalize_terms(headline, limit=1)
        else None
    )
    completeness = (
        0.4
        + (0.2 if headline else 0.0)
        + (0.2 if summary else 0.0)
        + (0.2 if valid_scenes else 0.0)
    )
    quality = (1.0 if status == "ok" else 0.65) * completeness
    return ArticleRecommendationFeature(
        article_id=article_id,
        terms=tuple(ordered_terms),
        term_weights=term_weights,
        representative_topic=representative_topic,
        domain=_normalize_domain(domain),
        published_at=published_at,
        quality_score=round(min(1.0, max(0.0, quality)), 6),
        headline=headline,
    )


def hydrate_recommendation_profile(
    *,
    events: Sequence[Mapping[str, Any]],
    feedback: Sequence[Mapping[str, Any]],
    article_features: Mapping[str, ArticleRecommendationFeature],
    snapshot_at: datetime,
    current_daypart: Daypart,
    saved: Sequence[Mapping[str, Any]] = (),
) -> RecommendationProfile:
    snapshot = _aware(snapshot_at, "snapshot_at").astimezone(UTC)
    lower_bound = snapshot - PROFILE_WINDOW
    eligible: list[Mapping[str, Any]] = []
    for event in events:
        occurred_at = _event_datetime(event)
        if lower_bound <= occurred_at <= snapshot:
            eligible.append(event)
    eligible.sort(key=_event_datetime, reverse=True)
    eligible = eligible[:PROFILE_EVENT_LIMIT]

    searches: dict[tuple[str, date], Mapping[str, Any]] = {}
    opens: dict[tuple[str, date], Mapping[str, Any]] = {}
    completes: dict[tuple[str, date], Mapping[str, Any]] = {}
    scenes: dict[tuple[str, date], tuple[float, Mapping[str, Any]]] = {}
    recently_seen: set[str] = set()
    for event in eligible:
        kind = _event_type(event)
        local_date = _event_local_date(event)
        occurred_at = _event_datetime(event)
        if kind == "search":
            query = _bounded_text(event.get("query"))
            normalized_query = " ".join(normalize_terms(query)) if query else ""
            if normalized_query:
                searches.setdefault((normalized_query, local_date), event)
            continue
        article_id = str(event.get("article_id") or "")
        if not article_id:
            continue
        if (
            kind in {"article_opened", "article_completed"}
            and occurred_at >= snapshot - RECENTLY_SEEN_WINDOW
        ):
            recently_seen.add(article_id)
        key = (article_id, local_date)
        if kind == "article_opened":
            opens.setdefault(key, event)
        elif kind == "article_completed":
            completes.setdefault(key, event)
        elif kind == "scene_viewed":
            scene_index = _safe_int(event.get("scene_index"), default=-1)
            scene_count = _safe_int(event.get("scene_count"), default=0)
            if 0 <= scene_index < scene_count:
                progress = (scene_index + 1) / scene_count
                previous = scenes.get(key)
                if previous is None or progress > previous[0]:
                    scenes[key] = (progress, event)

    global_terms: dict[str, float] = {}
    short_terms: dict[str, float] = {}
    saved_terms: dict[str, float] = {}
    daypart_terms: dict[str, float] = {}
    domain_weights: dict[str, float] = {}
    meaningful = 0
    matching_daypart_signals = 0

    def apply_terms(
        terms: Mapping[str, float],
        *,
        weight: float,
        event: Mapping[str, Any],
        domain: str | None = None,
    ) -> None:
        nonlocal meaningful, matching_daypart_signals
        occurred_at = _event_datetime(event)
        signal_weight = weight * _decay_with_half_life(
            occurred_at, snapshot, half_life_days=45.0
        )
        if signal_weight <= 0 or not terms:
            return
        meaningful += 1
        is_current_daypart = daypart_for_hour(_event_local_hour(event)) == current_daypart
        if is_current_daypart:
            matching_daypart_signals += 1
        for term, term_weight in terms.items():
            contribution = signal_weight * float(term_weight)
            global_terms[term] = global_terms.get(term, 0.0) + contribution
            if occurred_at >= snapshot - SHORT_PROFILE_WINDOW:
                short_contribution = (
                    weight
                    * _decay_with_half_life(
                        occurred_at,
                        snapshot,
                        half_life_days=1.0,
                    )
                    * float(term_weight)
                )
                short_terms[term] = (
                    short_terms.get(term, 0.0) + short_contribution
                )
            if is_current_daypart:
                daypart_terms[term] = daypart_terms.get(term, 0.0) + contribution
        if domain:
            domain_weights[domain] = domain_weights.get(domain, 0.0) + signal_weight

    for event in searches.values():
        query_terms = {term: 1.0 for term in normalize_terms(str(event["query"]))}
        apply_terms(query_terms, weight=4.0, event=event)
    for grouped, base_weight in ((opens, 1.0), (completes, 3.0)):
        for (article_id, _), event in grouped.items():
            feature = article_features.get(article_id)
            if feature is not None:
                apply_terms(
                    feature.term_weights,
                    weight=base_weight,
                    event=event,
                    domain=feature.domain,
                )
    for (article_id, _), (progress, event) in scenes.items():
        feature = article_features.get(article_id)
        if feature is not None:
            apply_terms(
                feature.term_weights,
                weight=2.0 * progress,
                event=event,
                domain=feature.domain,
            )

    excluded: set[str] = set()
    for row in saved[:100]:
        article_id = str(row.get("article_id") or "")
        saved_at = row.get("saved_at")
        if not article_id:
            continue
        excluded.add(article_id)
        feature = article_features.get(article_id)
        if feature is None:
            continue
        saved_weight = 5.0
        if isinstance(saved_at, datetime):
            saved_weight *= _decay_with_half_life(
                _aware(saved_at, "saved_at").astimezone(UTC),
                snapshot,
                half_life_days=90.0,
            )
        for term, term_weight in feature.term_weights.items():
            saved_terms[term] = (
                saved_terms.get(term, 0.0)
                + saved_weight * float(term_weight)
            )
        if feature.domain:
            domain_weights[feature.domain] = (
                domain_weights.get(feature.domain, 0.0) + saved_weight
            )

    negative_terms: dict[str, float] = {}
    for row in feedback[:200]:
        article_id = str(row.get("article_id") or "")
        reason = str(row.get("reason") or "")
        if not article_id:
            continue
        excluded.add(article_id)
        feature = article_features.get(article_id)
        if feature is None:
            continue
        if reason in {"not_relevant", "seen_similar"}:
            penalty = 2.0 if reason == "not_relevant" else 1.5
            for term, weight in feature.term_weights.items():
                negative_terms[term] = max(
                    negative_terms.get(term, 0.0),
                    penalty * float(weight),
                )
        elif reason == "distrust_source" and feature.domain:
            domain_weights[feature.domain] = min(
                domain_weights.get(feature.domain, 0.0),
                -3.0,
            )

    if matching_daypart_signals < 10:
        daypart_terms = {}
    return RecommendationProfile(
        global_terms=_trim_weights(global_terms),
        daypart_terms=_trim_weights(daypart_terms),
        domain_weights=dict(domain_weights),
        negative_terms=_trim_weights(negative_terms),
        excluded_article_ids=frozenset(excluded),
        recently_seen_article_ids=frozenset(recently_seen),
        meaningful_event_count=meaningful,
        snapshot_at=snapshot,
        current_daypart=current_daypart,
        short_terms=_trim_weights(short_terms),
        saved_terms=_trim_weights(saved_terms),
    )


def score_candidate(
    profile: RecommendationProfile,
    candidate: RecommendationCandidate,
    *,
    surface: RecommendationSurface,
    context: ArticleRecommendationFeature | None,
    now: datetime,
) -> ScoreBreakdown:
    instant = _aware(now, "now").astimezone(UTC)
    feature = candidate.feature
    global_interest = _weighted_profile_interest(
        feature.term_weights,
        (
            (profile.global_terms, 0.55),
            (profile.short_terms, 0.30),
            (profile.saved_terms, 0.15),
        ),
    )
    daypart_interest = (
        _cosine(profile.daypart_terms, feature.term_weights)
        if profile.daypart_terms
        else global_interest
    )
    context_similarity = (
        _cosine(context.term_weights, feature.term_weights)
        if context is not None
        else 0.0
    )
    freshness = _freshness(feature.published_at, instant)
    quality = feature.quality_score
    raw_affinity = profile.domain_weights.get(feature.domain or "", 0.0)
    source_affinity = max(0.0, math.tanh(raw_affinity / 8.0))
    negative_penalty = min(
        1.0,
        _cosine(profile.negative_terms, feature.term_weights)
        + (max(0.0, -raw_affinity) / 3.0),
    )
    effective_signals = profile.meaningful_event_count + (
        5 if profile.saved_terms else 0
    )
    confidence = min(1.0, max(0.0, effective_signals / 20.0))
    if surface is RecommendationSurface.HOME:
        personalized = (
            0.45 * global_interest
            + 0.15 * daypart_interest
            + 0.20 * freshness
            + 0.10 * quality
            + 0.10 * source_affinity
        )
    else:
        personalized = (
            0.30 * global_interest
            + 0.10 * daypart_interest
            + 0.30 * context_similarity
            + 0.15 * freshness
            + 0.10 * quality
            + 0.05 * source_affinity
        )
    baseline = 0.67 * freshness + 0.33 * quality
    base_score = (
        confidence * personalized
        + (1.0 - confidence) * baseline
        - 0.25 * negative_penalty
    )
    base_score = min(1.0, max(-0.25, base_score))
    return ScoreBreakdown(
        global_interest=global_interest,
        daypart_interest=daypart_interest,
        context_similarity=context_similarity,
        freshness=freshness,
        quality=quality,
        source_affinity=source_affinity,
        negative_penalty=negative_penalty,
        confidence=confidence,
        base_score=base_score,
        score_micros=round(base_score * 1_000_000),
    )


def select_diverse(
    candidates: Sequence[ScoredCandidate],
    *,
    surface: RecommendationSurface,
    limit: int,
) -> list[ScoredCandidate]:
    if not 1 <= limit <= 6:
        raise ValueError("recommendation limit is out of range")
    domain_cap = 1
    topic_cap = 1 if surface is RecommendationSurface.HOME else limit
    selected: list[ScoredCandidate] = []
    selected_ids: set[str] = set()
    selected_headlines: set[str] = set()
    domains: dict[str, int] = {}
    topics: dict[str, int] = {}

    def add(item: ScoredCandidate) -> bool:
        feature = item.candidate.feature
        headline_key = _headline_key(feature.headline)
        if (
            feature.article_id in selected_ids
            or (headline_key and headline_key in selected_headlines)
        ):
            return False
        domain = feature.domain or ""
        topic = item.representative_topic or ""
        if domain and domains.get(domain, 0) >= domain_cap:
            return False
        if topic and topics.get(topic, 0) >= topic_cap:
            return False
        selected.append(item)
        selected_ids.add(feature.article_id)
        if headline_key:
            selected_headlines.add(headline_key)
        if domain:
            domains[domain] = domains.get(domain, 0) + 1
        if topic:
            topics[topic] = topics.get(topic, 0) + 1
        return True

    if surface is RecommendationSurface.HOME:
        lane_caps = {"core": 2, "now": 1, "adjacent": 1, "surprise": 1}
        lane_counts = {lane: 0 for lane in lane_caps}
        for item in candidates:
            if len(selected) >= limit:
                break
            lane = _candidate_lane(item.candidate)
            if lane_counts[lane] >= lane_caps[lane]:
                continue
            if add(item):
                lane_counts[lane] += 1

    def fill(*, enforce_topic: bool, enforce_domain: bool) -> None:
        for item in candidates:
            if len(selected) >= limit:
                return
            feature = item.candidate.feature
            headline_key = _headline_key(feature.headline)
            if (
                feature.article_id in selected_ids
                or (headline_key and headline_key in selected_headlines)
            ):
                continue
            domain = feature.domain or ""
            topic = item.representative_topic or ""
            if enforce_domain and domain and domains.get(domain, 0) >= domain_cap:
                continue
            if enforce_topic and topic and topics.get(topic, 0) >= topic_cap:
                continue
            selected.append(item)
            selected_ids.add(feature.article_id)
            if headline_key:
                selected_headlines.add(headline_key)
            if domain:
                domains[domain] = domains.get(domain, 0) + 1
            if topic:
                topics[topic] = topics.get(topic, 0) + 1

    fill(enforce_topic=True, enforce_domain=True)
    if surface is RecommendationSurface.HOME:
        domain_cap = 2
        topic_cap = 2
    fill(enforce_topic=True, enforce_domain=True)
    fill(enforce_topic=False, enforce_domain=True)
    fill(enforce_topic=False, enforce_domain=False)
    return selected


def _candidate_lane(candidate: RecommendationCandidate) -> str:
    for lane in ("surprise", "adjacent", "core", "now"):
        if lane in candidate.sources:
            return lane
    return "core"


def rank_recommendations(
    *,
    profile: RecommendationProfile,
    candidates: Sequence[RecommendationCandidate],
    surface: RecommendationSurface,
    context: ArticleRecommendationFeature | None,
    generated_at: datetime,
    limit: int,
    after: RecommendationSortKey | None = None,
    rank_seed: bytes = b"",
) -> RankedRecommendationPage:
    instant = _aware(generated_at, "generated_at").astimezone(UTC)
    if not 1 <= limit <= 5:
        raise ValueError("recommendation limit is out of range")
    scored: list[ScoredCandidate] = []
    invalid = 0
    context_id = context.article_id if context else None
    seen_candidate_ids: set[str] = set()
    for candidate in candidates[:300]:
        feature = candidate.feature
        if (
            feature.article_id in seen_candidate_ids
            or feature.article_id in profile.excluded_article_ids
            or feature.article_id in profile.recently_seen_article_ids
            or feature.article_id == context_id
        ):
            continue
        seen_candidate_ids.add(feature.article_id)
        try:
            score = score_candidate(
                profile,
                candidate,
                surface=surface,
                context=context,
                now=instant,
            )
            if rank_seed:
                jitter = _seeded_jitter(rank_seed, feature.article_id)
                jittered_base = min(
                    1.0,
                    max(-0.25, score.base_score + jitter),
                )
                score = ScoreBreakdown(
                    global_interest=score.global_interest,
                    daypart_interest=score.daypart_interest,
                    context_similarity=score.context_similarity,
                    freshness=score.freshness,
                    quality=score.quality,
                    source_affinity=score.source_affinity,
                    negative_penalty=score.negative_penalty,
                    confidence=score.confidence,
                    base_score=jittered_base,
                    score_micros=round(jittered_base * 1_000_000),
                )
        except (TypeError, ValueError, OverflowError):
            invalid += 1
            continue
        if not math.isfinite(score.base_score):
            invalid += 1
            continue
        scored.append(
            ScoredCandidate(
                candidate=candidate,
                score=score,
                representative_topic=feature.representative_topic,
            )
        )
    scored.sort(key=_scored_order_key)
    if after is not None:
        after_key = _cursor_order_key(after)
        scored = [item for item in scored if _scored_order_key(item) > after_key]
    selected = select_diverse(scored, surface=surface, limit=limit + 1)
    has_more = len(selected) > limit
    visible = tuple(selected[:limit])
    next_sort_key = _sort_key_for(visible[-1]) if has_more and visible else None
    return RankedRecommendationPage(
        items=visible,
        next_sort_key=next_sort_key,
        strategy=(
            "personalized"
            if profile.meaningful_event_count >= 20
            else "blended_cold_start"
        ),
        invalid_candidate_count=invalid,
    )


def _scored_order_key(item: ScoredCandidate) -> tuple[int, float, int]:
    feature = item.candidate.feature
    published = feature.published_at or datetime.min.replace(tzinfo=UTC)
    return (
        -item.score.score_micros,
        -published.astimezone(UTC).timestamp(),
        -int(feature.article_id, 16),
    )


def _cursor_order_key(key: RecommendationSortKey) -> tuple[int, float, int]:
    return (
        -key.score_micros,
        -key.published_at.astimezone(UTC).timestamp(),
        -int(key.article_id, 16),
    )


def _sort_key_for(item: ScoredCandidate) -> RecommendationSortKey:
    feature = item.candidate.feature
    return RecommendationSortKey(
        score_micros=item.score.score_micros,
        published_at=feature.published_at or datetime.min.replace(tzinfo=UTC),
        article_id=feature.article_id,
    )


def _cosine(left: Mapping[str, float], right: Mapping[str, float]) -> float:
    if not left or not right:
        return 0.0
    dot = sum(float(weight) * float(right.get(term, 0.0)) for term, weight in left.items())
    left_norm = math.sqrt(sum(float(weight) ** 2 for weight in left.values()))
    right_norm = math.sqrt(sum(float(weight) ** 2 for weight in right.values()))
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    return min(1.0, max(0.0, dot / (left_norm * right_norm)))


def _weighted_profile_interest(
    feature_terms: Mapping[str, float],
    profiles: Sequence[tuple[Mapping[str, float], float]],
) -> float:
    active = [
        (_cosine(profile_terms, feature_terms), weight)
        for profile_terms, weight in profiles
        if profile_terms
    ]
    total_weight = sum(weight for _, weight in active)
    if total_weight <= 0:
        return 0.0
    return sum(value * weight for value, weight in active) / total_weight


def _seeded_jitter(seed: bytes, article_id: str) -> float:
    digest = hashlib.blake2b(
        seed + article_id.encode("ascii"),
        digest_size=8,
        person=b"gyeol-rec",
    ).digest()
    unit = int.from_bytes(digest, "big") / ((1 << 64) - 1)
    return (unit - 0.5) * 0.03


def _freshness(published_at: datetime | None, now: datetime) -> float:
    if published_at is None:
        return 0.0
    published = _aware(published_at, "published_at").astimezone(UTC)
    age_days = max(0.0, (now - published).total_seconds() / 86_400)
    return 2 ** (-age_days / 14.0)


def _decay(occurred_at: datetime, snapshot_at: datetime) -> float:
    return _decay_with_half_life(
        occurred_at, snapshot_at, half_life_days=45.0
    )


def _decay_with_half_life(
    occurred_at: datetime,
    snapshot_at: datetime,
    *,
    half_life_days: float,
) -> float:
    age_days = max(
        0.0,
        (snapshot_at - occurred_at.astimezone(UTC)).total_seconds() / 86_400,
    )
    return 2 ** (-age_days / half_life_days)


def _event_datetime(event: Mapping[str, Any]) -> datetime:
    value = event.get("occurred_at")
    if not isinstance(value, datetime):
        raise ValueError("event occurred_at must be a datetime")
    return _aware(value, "occurred_at").astimezone(UTC)


def _event_type(event: Mapping[str, Any]) -> str:
    value = event.get("event_type")
    return value.value if hasattr(value, "value") else str(value or "")


def _event_local_date(event: Mapping[str, Any]) -> date:
    value = event.get("local_date")
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("event local_date is invalid") from exc
    occurred_at = _event_datetime(event)
    offset = _safe_int(event.get("utc_offset_minutes"), default=0)
    return (occurred_at + timedelta(minutes=offset)).date()


def _event_local_hour(event: Mapping[str, Any]) -> int:
    value = event.get("local_hour")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    occurred_at = _event_datetime(event)
    offset = _safe_int(event.get("utc_offset_minutes"), default=0)
    return (occurred_at + timedelta(minutes=offset)).hour


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return value


def _trim_weights(values: Mapping[str, float], *, limit: int = 128) -> dict[str, float]:
    return dict(
        sorted(
            (
                (term, float(weight))
                for term, weight in values.items()
                if math.isfinite(float(weight)) and float(weight) > 0
            ),
            key=lambda item: (-item[1], item[0]),
        )[:limit]
    )


def _bounded_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:1000]


def _normalize_domain(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().casefold().rstrip(".")
    return normalized[:253] or None


def _headline_key(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())[:500]


def _safe_int(value: Any, *, default: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return default
