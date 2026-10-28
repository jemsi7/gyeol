"""Quality gates and immutable contracts for automatic article curation.

Provider output is never trusted directly.  Existing Delivery Maps first pass a
deterministic structural gate, then every selected article and route is checked
again here before a catalog can be persisted.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from typing import Any, Final, Mapping, Sequence

MIN_READABLE_CHARS = 500
MIN_SELECTED_CLAIMS = 4
MIN_KEYWORDS = 3
ROUTE_SIZE = 4
ROUTE_ROLES = ("start", "context", "evidence", "counterpoint")
MAX_GENERATED_CURATION_ROUTES: Final[int] = 4
CURATION_INPUT_REVISION = "global-agent-v4"
QUALITY_FIELDS = (
    "durability",
    "evidence_depth",
    "explanatory_value",
    "originality",
    "structural_significance",
    "ephemeral_risk",
    "slop_risk",
)
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")


class CurationValidationError(ValueError):
    """A stable, redacted validation failure safe to persist and log."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class CurationCandidate:
    article_id: str
    title: str
    headline: str
    summary: str
    keywords: tuple[str, ...]
    domain: str
    published_at: datetime | None
    scene_count: int
    selected_claim_count: int
    required_claim_count: int
    readable_chars: int
    source_hash: str

    def prompt_dict(self) -> dict[str, Any]:
        return {
            "article_id": self.article_id,
            "title": self.title[:160],
            "headline": self.headline[:160],
            "summary": self.summary[:420],
            "keywords": list(self.keywords[:8]),
            "domain": self.domain[:120],
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "scene_count": self.scene_count,
            "selected_claim_count": self.selected_claim_count,
            "required_claim_count": self.required_claim_count,
        }


@dataclass(frozen=True, slots=True)
class CurationQuality:
    durability: int
    evidence_depth: int
    explanatory_value: int
    originality: int
    structural_significance: int
    ephemeral_risk: int
    slop_risk: int
    structural_breaking: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CurationRouteItem:
    article_id: str
    role: str
    reason: str
    lasting_value: str
    quality: CurationQuality


@dataclass(frozen=True, slots=True)
class CurationRoute:
    slug: str
    title: str
    summary: str
    topic: str
    representative: bool
    items: tuple[CurationRouteItem, ...]


@dataclass(frozen=True, slots=True)
class CurationPlan:
    search_suggestions: tuple[str, ...]
    routes: tuple[CurationRoute, ...]


def build_curation_candidate(row: Mapping[str, Any]) -> CurationCandidate | None:
    """Return a bounded planner candidate, or ``None`` for structural rejects."""
    if row.get("status") != "ok":
        return None
    raw_map = row.get("data")
    if isinstance(raw_map, str):
        try:
            raw_map = json.loads(raw_map)
        except (TypeError, ValueError):
            return None
    if not isinstance(raw_map, Mapping):
        return None
    try:
        format_version = int(raw_map.get("format_version"))
    except (TypeError, ValueError):
        return None
    if format_version != 3:
        return None
    editorial = raw_map.get("editorial")
    if not isinstance(editorial, Mapping) or editorial.get("critic_verdict") != "pass":
        return None

    keywords = _clean_string_list(raw_map.get("keywords"), limit=8)
    selected_claims = _clean_string_list(raw_map.get("selected_claim_refs"), limit=100)
    required_claims = _clean_string_list(raw_map.get("required_claim_refs"), limit=100)
    scenes = raw_map.get("scenes")
    if (
        len(keywords) < MIN_KEYWORDS
        or len(selected_claims) < MIN_SELECTED_CLAIMS
        or not required_claims
        or not isinstance(scenes, list)
        or not 3 <= len(scenes) <= 9
    ):
        return None

    readable_parts = [raw_map.get("headline"), raw_map.get("summary")]
    for scene in scenes:
        if not isinstance(scene, Mapping):
            return None
        readable_parts.extend(
            scene.get(field) for field in ("title", "summary", "body", "secondary", "quote")
        )
    readable_chars = len("".join(_plain_text(value) for value in readable_parts))
    if readable_chars < MIN_READABLE_CHARS:
        return None

    article_id = _bounded_string(row.get("article_id"), 1, 128)
    headline = _bounded_string(raw_map.get("headline"), 1, 240)
    summary = _bounded_string(raw_map.get("summary"), 1, 800)
    if article_id is None or headline is None or summary is None:
        return None
    title = _bounded_string(row.get("title"), 0, 240) or headline
    domain = _bounded_string(row.get("domain"), 0, 200) or ""
    published_at = row.get("published_at")
    if not isinstance(published_at, datetime):
        published_at = None
    source_hash = _bounded_string(editorial.get("source_hash"), 1, 128)
    if source_hash is None:
        source_hash = hashlib.sha256(
            json.dumps(raw_map, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
    return CurationCandidate(
        article_id=article_id,
        title=title,
        headline=headline,
        summary=summary,
        keywords=tuple(keywords),
        domain=domain.casefold(),
        published_at=published_at,
        scene_count=len(scenes),
        selected_claim_count=len(selected_claims),
        required_claim_count=len(required_claims),
        readable_chars=readable_chars,
        source_hash=source_hash,
    )


def curation_input_hash(
    editorial_day: date,
    *,
    refresh_slot: str,
    pool_fingerprint: str,
) -> str:
    if (
        not isinstance(refresh_slot, str)
        or not 1 <= len(refresh_slot) <= 64
        or not re.fullmatch(r"[0-9a-f]{32}", pool_fingerprint)
    ):
        raise ValueError("invalid curation input identity")
    payload = {
        "contract_revision": CURATION_INPUT_REVISION,
        "editorial_day": editorial_day.isoformat(),
        "refresh_slot": refresh_slot,
        "pool_fingerprint": pool_fingerprint,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def curation_refresh_slot(now: datetime, interval_s: float) -> str:
    if (
        now.tzinfo is None
        or now.utcoffset() is None
        or not 3600.0 <= interval_s <= 86400.0
    ):
        raise ValueError("invalid curation refresh slot")
    slot_epoch = int(now.astimezone(UTC).timestamp() // interval_s * interval_s)
    return (
        datetime.fromtimestamp(slot_epoch, tz=UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def curation_plan_payload(plan: CurationPlan) -> dict[str, Any]:
    """Return a JSON-compatible validator input for final double validation."""
    return json.loads(
        json.dumps(asdict(plan), ensure_ascii=False, separators=(",", ":"))
    )


def validate_curation_plan(
    raw: Mapping[str, Any],
    candidates: Sequence[CurationCandidate],
    *,
    min_routes: int = 2,
    max_routes: int = MAX_GENERATED_CURATION_ROUTES,
) -> CurationPlan:
    if (
        not isinstance(raw, Mapping)
        or not 1
        <= min_routes
        <= max_routes
        <= MAX_GENERATED_CURATION_ROUTES
    ):
        raise CurationValidationError("invalid_plan_shape")
    suggestions_raw = raw.get("search_suggestions")
    routes_raw = raw.get("routes")
    if not isinstance(suggestions_raw, list) or not 3 <= len(suggestions_raw) <= 8:
        raise CurationValidationError("invalid_plan_shape")
    if not isinstance(routes_raw, list) or not min_routes <= len(routes_raw) <= max_routes:
        raise CurationValidationError("catalog_too_small")

    suggestions: list[str] = []
    suggestion_keys: set[str] = set()
    for value in suggestions_raw:
        suggestion = _require_string(value, 2, 40)
        key = _normalized_key(suggestion)
        if key in suggestion_keys:
            raise CurationValidationError("invalid_plan_shape")
        suggestion_keys.add(key)
        suggestions.append(suggestion)

    candidate_by_id = {candidate.article_id: candidate for candidate in candidates}
    if len(candidate_by_id) != len(candidates):
        raise CurationValidationError("candidate_duplicate")
    seen_article_ids: set[str] = set()
    seen_slugs: set[str] = set()
    routes: list[CurationRoute] = []
    representative_count = 0
    for raw_route in routes_raw:
        if not isinstance(raw_route, Mapping):
            raise CurationValidationError("invalid_plan_shape")
        slug = _require_string(raw_route.get("slug"), 2, 64)
        if not _SLUG_RE.fullmatch(slug) or slug in seen_slugs:
            raise CurationValidationError("invalid_plan_shape")
        seen_slugs.add(slug)
        title = _require_string(raw_route.get("title"), 4, 100)
        summary = _require_string(raw_route.get("summary"), 20, 300)
        topic = _require_string(raw_route.get("topic"), 2, 60)
        representative = raw_route.get("representative")
        if not isinstance(representative, bool):
            raise CurationValidationError("invalid_plan_shape")
        representative_count += int(representative)
        raw_items = raw_route.get("items")
        if not isinstance(raw_items, list) or len(raw_items) != ROUTE_SIZE:
            raise CurationValidationError("invalid_plan_shape")

        route_ids: set[str] = set()
        route_domains: set[str] = set()
        route_items: list[CurationRouteItem] = []
        for index, raw_item in enumerate(raw_items):
            if not isinstance(raw_item, Mapping):
                raise CurationValidationError("invalid_plan_shape")
            article_id = _require_string(raw_item.get("article_id"), 1, 128)
            candidate = candidate_by_id.get(article_id)
            if candidate is None:
                raise CurationValidationError("candidate_unknown")
            if article_id in route_ids or article_id in seen_article_ids:
                raise CurationValidationError("candidate_duplicate")
            role = raw_item.get("role")
            if role != ROUTE_ROLES[index]:
                raise CurationValidationError("invalid_plan_shape")
            reason = _require_string(raw_item.get("reason"), 20, 180)
            lasting_value = _require_string(raw_item.get("lasting_value"), 20, 180)
            quality = _validate_quality(raw_item.get("quality"))
            route_ids.add(article_id)
            seen_article_ids.add(article_id)
            if candidate.domain:
                route_domains.add(candidate.domain)
            route_items.append(
                CurationRouteItem(
                    article_id=article_id,
                    role=role,
                    reason=reason,
                    lasting_value=lasting_value,
                    quality=quality,
                )
            )
        if len(route_domains) < 2:
            raise CurationValidationError("route_source_diversity_failed")
        routes.append(
            CurationRoute(
                slug=slug,
                title=title,
                summary=summary,
                topic=topic,
                representative=representative,
                items=tuple(route_items),
            )
        )
    if representative_count != 1:
        raise CurationValidationError("invalid_plan_shape")
    return CurationPlan(tuple(suggestions), tuple(routes))


def _validate_quality(raw: Any) -> CurationQuality:
    if not isinstance(raw, Mapping):
        raise CurationValidationError("invalid_plan_shape")
    expected = set(QUALITY_FIELDS) | {"structural_breaking"}
    if set(raw) != expected:
        raise CurationValidationError("invalid_plan_shape")
    scores = {field: _require_score(raw.get(field)) for field in QUALITY_FIELDS}
    structural_breaking = raw.get("structural_breaking")
    if not isinstance(structural_breaking, bool):
        raise CurationValidationError("invalid_plan_shape")
    if (
        scores["durability"] < 3
        or scores["evidence_depth"] < 3
        or scores["explanatory_value"] < 3
        or scores["originality"] < 2
        or scores["slop_risk"] > 1
    ):
        raise CurationValidationError("quality_gate_failed")
    if scores["ephemeral_risk"] > 2 and not (
        structural_breaking
        and scores["structural_significance"] >= 4
        and scores["evidence_depth"] >= 4
    ):
        raise CurationValidationError("quality_gate_failed")
    return CurationQuality(structural_breaking=structural_breaking, **scores)


def _require_score(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 5:
        raise CurationValidationError("invalid_plan_shape")
    return value


def _require_string(value: Any, minimum: int, maximum: int) -> str:
    result = _bounded_string(value, minimum, maximum)
    if result is None:
        raise CurationValidationError("invalid_plan_shape")
    return result


def _bounded_string(value: Any, minimum: int, maximum: int) -> str | None:
    if not isinstance(value, str) or any(mark in value for mark in ("\x00", "\r")):
        return None
    value = value.strip()
    if not minimum <= len(value) <= maximum:
        return None
    return value


def _clean_string_list(value: Any, *, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value[:limit]:
        cleaned = _bounded_string(item, 1, 120)
        if cleaned is None:
            continue
        key = _normalized_key(cleaned)
        if key not in seen:
            seen.add(key)
            out.append(cleaned)
    return out


def _normalized_key(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _plain_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(value.split())
