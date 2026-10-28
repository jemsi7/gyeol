"""Privacy-minimized audience aggregation for the shared curation catalog.

Raw account-scoped signals enter this module only inside one worker run.  The
only serializable output is a service-wide aggregate: no account identifier,
raw event, original query, or per-user profile crosses the provider boundary.
"""
from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Mapping, Sequence


AUDIENCE_LOOKBACK_DAYS = 180
AUDIENCE_EVENT_LIMIT_PER_ACCOUNT = 500
AUDIENCE_SAMPLE_LIMIT = 100
_HALF_LIFE_DAYS = 30.0
_TERM_RE = re.compile(r"[0-9A-Za-z가-힣][0-9A-Za-z가-힣.+#-]{1,31}")
_FEEDBACK_REASONS = (
    "not_relevant",
    "too_simple",
    "too_difficult",
    "seen_similar",
    "distrust_source",
    "other",
)
_SIGNAL_WEIGHTS = {
    "search": 4.0,
    "article_opened": 1.0,
    "article_completed": 3.0,
    "saved": 4.0,
}


@dataclass(frozen=True, slots=True)
class AudienceInterest:
    name: str
    score: float
    user_count: int
    user_share: float

    def prompt_dict(self) -> dict[str, str | float | int]:
        return {
            "name": self.name,
            "score": self.score,
            "user_count": self.user_count,
            "user_share": self.user_share,
        }


@dataclass(frozen=True, slots=True)
class CurationAudienceAggregate:
    eligible_user_count: int
    sampled_user_count: int
    interests: tuple[AudienceInterest, ...]
    domains: tuple[AudienceInterest, ...]
    engagement: Mapping[str, float]
    feedback: Mapping[str, float]

    def prompt_dict(self) -> dict[str, Any]:
        """Return the only audience payload allowed to reach the model."""
        return {
            "scope": "global_service_audience",
            "eligible_user_count": self.eligible_user_count,
            "sampled_user_count": self.sampled_user_count,
            "interests": [item.prompt_dict() for item in self.interests],
            "domains": [item.prompt_dict() for item in self.domains],
            "engagement": dict(self.engagement),
            "feedback": dict(self.feedback),
        }


def build_curation_audience_aggregate(
    rows: Sequence[Mapping[str, Any]],
    *,
    eligible_user_count: int,
    sampled_user_count: int,
    snapshot_at: datetime,
) -> CurationAudienceAggregate:
    """Normalize each sampled account, then average into one global profile."""
    if snapshot_at.tzinfo is None or snapshot_at.utcoffset() is None:
        raise ValueError("snapshot_at must be timezone-aware")
    if (
        eligible_user_count < 0
        or sampled_user_count < 0
        or sampled_user_count > AUDIENCE_SAMPLE_LIMIT
        or sampled_user_count > eligible_user_count
    ):
        raise ValueError("invalid audience counts")
    snapshot_at = snapshot_at.astimezone(UTC)

    user_terms: dict[Any, Counter[str]] = defaultdict(Counter)
    user_domains: dict[Any, Counter[str]] = defaultdict(Counter)
    user_engagement: dict[Any, Counter[str]] = defaultdict(Counter)
    user_feedback: dict[Any, set[str]] = defaultdict(set)
    article_backed_terms: set[str] = set()
    seen_signal_keys: set[tuple[Any, ...]] = set()
    scene_groups: dict[tuple[Any, str, date], tuple[float, Mapping[str, Any]]] = {}

    for row in rows:
        account_id = row.get("account_id")
        signal_type = row.get("signal_type")
        if account_id is None or not isinstance(signal_type, str):
            continue
        if signal_type == "feedback":
            reason = row.get("feedback_reason")
            if reason in _FEEDBACK_REASONS:
                key = (account_id, "feedback", row.get("article_id"), reason)
                if key not in seen_signal_keys:
                    seen_signal_keys.add(key)
                    user_feedback[account_id].add(reason)
                    user_engagement[account_id]["feedback"] += 1
            continue

        occurred_at = _aware_datetime(row.get("occurred_at"))
        if occurred_at is None or occurred_at > snapshot_at:
            continue
        age_days = max(
            0.0, (snapshot_at - occurred_at.astimezone(UTC)).total_seconds() / 86400.0
        )
        if age_days > AUDIENCE_LOOKBACK_DAYS:
            continue
        local_date = _local_date(row.get("local_date"), occurred_at)

        if signal_type == "scene_viewed":
            article_id = _safe_text(row.get("article_id"), maximum=128)
            ratio = _scene_ratio(row.get("scene_index"), row.get("scene_count"))
            if article_id is None or ratio is None:
                continue
            key = (account_id, article_id, local_date)
            previous = scene_groups.get(key)
            if previous is None or ratio > previous[0]:
                scene_groups[key] = (ratio, row)
            continue

        if signal_type == "search":
            query_terms = _query_terms(row.get("query"))
            if not query_terms:
                continue
            query_key = " ".join(query_terms)
            key = (account_id, signal_type, query_key, local_date)
            if key in seen_signal_keys:
                continue
            seen_signal_keys.add(key)
            weight = _SIGNAL_WEIGHTS[signal_type] * _decay(age_days)
            _add_terms(user_terms[account_id], query_terms, weight)
            user_engagement[account_id]["search"] += 1
            continue

        if signal_type not in _SIGNAL_WEIGHTS:
            continue
        article_id = _safe_text(row.get("article_id"), maximum=128)
        if article_id is None:
            continue
        key = (account_id, signal_type, article_id, local_date)
        if key in seen_signal_keys:
            continue
        seen_signal_keys.add(key)
        weight = _SIGNAL_WEIGHTS[signal_type] * _decay(age_days)
        terms = _article_terms(row.get("map_data"))
        article_backed_terms.update(terms)
        _add_terms(user_terms[account_id], terms, weight)
        domain = _normalized_domain(row.get("domain"))
        if domain:
            user_domains[account_id][domain] += weight
        user_engagement[account_id][_engagement_name(signal_type)] += 1

    for (account_id, _article_id, _local_date_value), (ratio, row) in scene_groups.items():
        occurred_at = _aware_datetime(row.get("occurred_at"))
        if occurred_at is None:
            continue
        age_days = max(
            0.0, (snapshot_at - occurred_at.astimezone(UTC)).total_seconds() / 86400.0
        )
        weight = 2.0 * ratio * _decay(age_days)
        terms = _article_terms(row.get("map_data"))
        article_backed_terms.update(terms)
        _add_terms(user_terms[account_id], terms, weight)
        domain = _normalized_domain(row.get("domain"))
        if domain:
            user_domains[account_id][domain] += weight
        user_engagement[account_id]["progressed"] += 1

    normalized_terms = {
        account_id: _normalize_counter(values)
        for account_id, values in user_terms.items()
    }
    normalized_domains = {
        account_id: _normalize_counter(values)
        for account_id, values in user_domains.items()
    }
    interests = _aggregate_interests(
        normalized_terms,
        sampled_user_count=sampled_user_count,
        limit=24,
        safe_single_user_terms=article_backed_terms,
    )
    domains = _aggregate_interests(
        normalized_domains,
        sampled_user_count=sampled_user_count,
        limit=12,
        safe_single_user_terms=set(normalized_domain for normalized_domain in _all_keys(normalized_domains)),
    )
    engagement = _aggregate_engagement(
        user_engagement, sampled_user_count=sampled_user_count
    )
    feedback = _aggregate_feedback(
        user_feedback, sampled_user_count=sampled_user_count
    )
    return CurationAudienceAggregate(
        eligible_user_count=eligible_user_count,
        sampled_user_count=sampled_user_count,
        interests=interests,
        domains=domains,
        engagement=engagement,
        feedback=feedback,
    )


def _aggregate_interests(
    profiles: Mapping[Any, Mapping[str, float]],
    *,
    sampled_user_count: int,
    limit: int,
    safe_single_user_terms: set[str],
) -> tuple[AudienceInterest, ...]:
    if sampled_user_count == 0:
        return ()
    totals: Counter[str] = Counter()
    contributors: Counter[str] = Counter()
    for profile in profiles.values():
        for name, score in profile.items():
            if score <= 0 or not math.isfinite(score):
                continue
            totals[name] += score
            contributors[name] += 1
    ranked: list[AudienceInterest] = []
    for name, total in totals.items():
        user_count = int(contributors[name])
        if user_count < 2 and name not in safe_single_user_terms:
            continue
        ranked.append(
            AudienceInterest(
                name=name,
                score=_quantize(total / sampled_user_count),
                user_count=user_count,
                user_share=_quantize(user_count / sampled_user_count),
            )
        )
    ranked.sort(key=lambda item: (-item.score, -item.user_count, item.name))
    return tuple(ranked[:limit])


def _aggregate_engagement(
    values: Mapping[Any, Counter[str]], *, sampled_user_count: int
) -> dict[str, float]:
    names = ("search", "opened", "progressed", "completed", "saved", "feedback")
    if sampled_user_count == 0:
        return {f"{name}_per_user": 0.0 for name in names} | {
            f"users_with_{name}_share": 0.0 for name in names
        }
    result: dict[str, float] = {}
    for name in names:
        total = sum(counter.get(name, 0) for counter in values.values())
        users = sum(1 for counter in values.values() if counter.get(name, 0) > 0)
        result[f"{name}_per_user"] = _quantize(total / sampled_user_count)
        result[f"users_with_{name}_share"] = _quantize(users / sampled_user_count)
    return result


def _aggregate_feedback(
    values: Mapping[Any, set[str]], *, sampled_user_count: int
) -> dict[str, float]:
    if sampled_user_count == 0:
        return {reason: 0.0 for reason in _FEEDBACK_REASONS}
    return {
        reason: _quantize(
            sum(1 for reasons in values.values() if reason in reasons)
            / sampled_user_count
        )
        for reason in _FEEDBACK_REASONS
    }


def _article_terms(raw_map: Any) -> tuple[str, ...]:
    if isinstance(raw_map, str):
        try:
            raw_map = json.loads(raw_map)
        except (TypeError, ValueError):
            return ()
    if not isinstance(raw_map, Mapping):
        return ()
    source: list[str] = []
    keywords = raw_map.get("keywords")
    if isinstance(keywords, list):
        source.extend(item for item in keywords[:12] if isinstance(item, str))
    headline = raw_map.get("headline")
    if isinstance(headline, str):
        source.append(headline)
    terms: list[str] = []
    seen: set[str] = set()
    for value in source:
        normalized = " ".join(unicodedata.normalize("NFKC", value).casefold().split())
        if 2 <= len(normalized) <= 32 and _safe_term(normalized) and normalized not in seen:
            seen.add(normalized)
            terms.append(normalized)
        for token in _TERM_RE.findall(normalized):
            if _safe_term(token) and token not in seen:
                seen.add(token)
                terms.append(token)
            if len(terms) >= 16:
                return tuple(terms)
    return tuple(terms)


def _query_terms(value: Any) -> tuple[str, ...]:
    if not isinstance(value, str):
        return ()
    normalized = " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    if not 2 <= len(normalized) <= 80 or "@" in normalized or "://" in normalized:
        return ()
    terms: list[str] = []
    seen: set[str] = set()
    for token in _TERM_RE.findall(normalized):
        if _safe_term(token) and token not in seen:
            seen.add(token)
            terms.append(token)
        if len(terms) >= 8:
            break
    return tuple(terms)


def _safe_term(value: str) -> bool:
    return (
        2 <= len(value) <= 32
        and not value.isdigit()
        and "\x00" not in value
        and "\n" not in value
        and "\r" not in value
    )


def _add_terms(counter: Counter[str], terms: Sequence[str], weight: float) -> None:
    if not terms or weight <= 0 or not math.isfinite(weight):
        return
    share = weight / len(terms)
    for term in terms:
        counter[term] += share


def _normalize_counter(counter: Mapping[str, float]) -> dict[str, float]:
    total = sum(
        value for value in counter.values() if value > 0 and math.isfinite(value)
    )
    if total <= 0:
        return {}
    return {
        key: value / total
        for key, value in counter.items()
        if value > 0 and math.isfinite(value)
    }


def _all_keys(values: Mapping[Any, Mapping[str, float]]) -> set[str]:
    return {key for mapping in values.values() for key in mapping}


def _decay(age_days: float) -> float:
    return 2.0 ** (-age_days / _HALF_LIFE_DAYS)


def _scene_ratio(scene_index: Any, scene_count: Any) -> float | None:
    if (
        isinstance(scene_index, bool)
        or isinstance(scene_count, bool)
        or not isinstance(scene_index, int)
        or not isinstance(scene_count, int)
        or scene_count <= 0
        or not 0 <= scene_index < scene_count
    ):
        return None
    return (scene_index + 1) / scene_count


def _aware_datetime(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return None
    return value


def _local_date(value: Any, fallback: datetime) -> date:
    return value if isinstance(value, date) and not isinstance(value, datetime) else fallback.date()


def _normalized_domain(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    result = value.strip().casefold()
    if not result or len(result) > 200 or any(mark in result for mark in ("\x00", "\n", "\r")):
        return ""
    return result


def _safe_text(value: Any, *, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    result = value.strip()
    if not result or len(result) > maximum or any(
        mark in result for mark in ("\x00", "\n", "\r")
    ):
        return None
    return result


def _engagement_name(signal_type: str) -> str:
    return {
        "article_opened": "opened",
        "article_completed": "completed",
        "saved": "saved",
    }[signal_type]


def _quantize(value: float) -> float:
    return round(value, 6)
