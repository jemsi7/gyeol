"""Cheap headline triage that ranks candidates for the daily Map budget.

Ingestion still stores every article. This module decides only the *order* in
which candidates spend a bounded daily allowance: deterministic signals catch
wire copy, sponsored paths, ticker/listicle headlines and syndicated duplicates,
and one batched provider call scores what a headline can actually support. The
expensive Delivery Map pipeline is unchanged and still owns article-level truth.
"""
from __future__ import annotations

import math
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Final, Literal, Mapping, Sequence
from urllib.parse import urlsplit

from .config import Settings
from .openrouter import OpenRouterClient, OpenRouterRateLimiter
from .triage_prompts import (
    CRYPTO_TRIAGE_PROMPT_VERSION,
    build_crypto_triage_prompt,
    build_triage_prompt,
)

log = logging.getLogger("gdelt.triage")

NEUTRAL_SCORE: Final[int] = 50
MAX_TITLE_CHARS: Final[int] = 180
MAX_DOMAIN_CHARS: Final[int] = 120
MAX_QUERY_CHARS: Final[int] = 120
CRYPTO_RELEVANCE_SAMPLE_SIZE: Final[int] = 50
CRYPTO_RELEVANCE_TOP_K: Final[int] = 20
CRYPTO_RELEVANCE_MIN_PRECISION: Final[float] = 0.90
CRYPTO_TRIAGE_PROFILE_VERSION: Final[str] = "crypto-profile-v1"
CRYPTO_RELEVANCE_VALUES: Final = frozenset({"on_topic", "off_topic"})

VALUE_AXES: Final = ("durability", "significance", "explanatory_value")
RISK_AXES: Final = ("ephemeral_risk", "slop_risk")
SCORE_AXES: Final = VALUE_AXES + RISK_AXES
AXIS_WEIGHTS: Final[dict[str, float]] = {
    "durability": 0.35,
    "significance": 0.25,
    "explanatory_value": 0.20,
    "ephemeral_risk": 0.10,
    "slop_risk": 0.10,
}
MAX_AXIS_VALUE: Final[int] = 5

# Distribution wires and aggregators republish the same corporate copy; they are
# penalised, never hard-rejected, so a genuine scoop from one can still surface.
WIRE_DOMAINS: Final[frozenset[str]] = frozenset(
    {
        "prnewswire.com",
        "globenewswire.com",
        "businesswire.com",
        "newswire.com",
        "accesswire.com",
        "einpresswire.com",
        "einnews.com",
        "prweb.com",
        "openpr.com",
        "presseportal.de",
        "marketscreener.com",
        "streetinsider.com",
        "stocktitan.net",
        "investing.com",
        "benzinga.com",
    }
)
SPONSORED_PATH_RE: Final = re.compile(
    r"/(press[-_]?releases?|sponsored|sponsor[-_]?content|partner[-_]?content"
    r"|advertorial|paid[-_]?post|promotions?)(/|$)",
    re.IGNORECASE,
)
TICKER_TITLE_RE: Final = re.compile(
    r"(\bshares?\s+(rise|rises|fall|falls|jump|jumps|slip|slips|gain|gains|drop|drops)\b"
    r"|\bstock(s)?\s+(to\s+watch|price\s+target|forecast|prediction)\b"
    r"|\b(nasdaq|nyse|kospi|kosdaq|tsx|lse)\s*:\s*[a-z]{1,6}\b"
    r"|\bprice\s+prediction\b|\b52[- ]week\b|\bmarket\s+cap\s+hits\b"
    r"|\bq[1-4]\s+earnings\s+call\s+transcript\b)",
    re.IGNORECASE,
)
LISTICLE_TITLE_RE: Final = re.compile(
    r"(^\s*(the\s+)?(top|best)\s+\d+\b|^\s*\d+\s+(best|top|things|ways|reasons|tips|deals)\b"
    r"|\bbest\s+deals?\b|\bdeal\s+of\s+the\s+day\b|\bcoupon\b|\bdiscount\s+code\b"
    r"|\bblack\s+friday\s+deals?\b|\bgift\s+guide\b)",
    re.IGNORECASE,
)
PENALTIES: Final[dict[str, int]] = {
    "missing_title": 100,
    "wire_domain": 30,
    "sponsored_path": 30,
    "ticker_title": 25,
    "listicle_title": 15,
    "duplicate_title": 20,
}

# Crypto reporting says "market cap hits" and "52-week" about assets the way
# technology reporting says nothing at all; keeping those patterns would score
# ordinary crypto coverage as ticker slop. "Price prediction" stays -- it is
# slop in every domain -- but at a lower weight, because even the surviving
# patterns are noisier here. These values are provisional until the T-12
# sample measurement replaces them (DR-13200).
CRYPTO_TICKER_TITLE_RE: Final = re.compile(
    r"(\bshares?\s+(rise|rises|fall|falls|jump|jumps|slip|slips|gain|gains|drop|drops)\b"
    r"|\bstock(s)?\s+(to\s+watch|price\s+target|forecast|prediction)\b"
    r"|\b(nasdaq|nyse|kospi|kosdaq|tsx|lse)\s*:\s*[a-z]{1,6}\b"
    r"|\bprice\s+prediction\b"
    r"|\bq[1-4]\s+earnings\s+call\s+transcript\b)",
    re.IGNORECASE,
)
CRYPTO_PENALTIES: Final[dict[str, int]] = {**PENALTIES, "ticker_title": 10}


@dataclass(frozen=True, slots=True)
class TriageProfile:
    """One coverage domain's deterministic penalty rules (DR-13200)."""

    name: str
    wire_domains: frozenset[str]
    sponsored_path_re: "re.Pattern[str]"
    ticker_title_re: "re.Pattern[str]"
    listicle_title_re: "re.Pattern[str]"
    penalties: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class CryptoTriageAxes:
    """Provider-backed crypto axes, including the hard relevance decision."""

    durability: int
    significance: int
    explanatory_value: int
    ephemeral_risk: int
    slop_risk: int
    relevance: Literal["on_topic", "off_topic"]


CORE_PROFILE: Final[TriageProfile] = TriageProfile(
    name="core",
    wire_domains=WIRE_DOMAINS,
    sponsored_path_re=SPONSORED_PATH_RE,
    ticker_title_re=TICKER_TITLE_RE,
    listicle_title_re=LISTICLE_TITLE_RE,
    penalties=PENALTIES,
)
CRYPTO_PROFILE: Final[TriageProfile] = TriageProfile(
    name="crypto",
    # Distribution wires are slop in crypto too, so this set is unchanged.
    wire_domains=WIRE_DOMAINS,
    sponsored_path_re=SPONSORED_PATH_RE,
    ticker_title_re=CRYPTO_TICKER_TITLE_RE,
    listicle_title_re=LISTICLE_TITLE_RE,
    penalties=CRYPTO_PENALTIES,
)
PROFILES: Final[dict[str, TriageProfile]] = {
    "core": CORE_PROFILE,
    "crypto": CRYPTO_PROFILE,
}


def profile_for(coverage_domain: str) -> TriageProfile:
    """Return the domain's profile, falling back to core.

    An unknown string must still be scoreable: leaving a candidate unscored
    would strand it in the queue, which is worse than scoring it with the
    stricter core rules.
    """
    return PROFILES.get((coverage_domain or "").strip().casefold(), CORE_PROFILE)


def crypto_relevance_required(
    *, coverage_domain: str, document_type: str | None
) -> bool:
    """Every crypto candidate requires relevance; document allowlists never bypass it."""
    del document_type
    return (coverage_domain or "").strip().casefold() == "crypto"

_TITLE_SUFFIX_RE = re.compile(r"\s[-|–—]\s(?=[^-|–—]{1,40}$)")
_PUNCTUATION_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")


class TriageValidationError(ValueError):
    """A stable, redacted contract failure safe to persist and log."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class TriageCandidate:
    article_id: str
    title: str
    domain: str
    source_query: str
    url: str = ""
    # Which penalty profile applies. Deliberately absent from prompt_dict:
    # the model ranks headlines, and the coverage domain is not its business.
    coverage_domain: str = "core"
    # Used only to prove that curated primary documents take the same crypto
    # relevance path. It is deliberately absent from prompt_dict as well:
    # allowlisted attribution must not persuade the model that a title is topical.
    document_type: str | None = None

    def prompt_dict(self, ref: int) -> dict[str, Any]:
        """Bounded payload addressed by a batch-local ref, never the hash id."""
        return {
            "ref": ref,
            "title": self.title[:MAX_TITLE_CHARS],
            "domain": self.domain[:MAX_DOMAIN_CHARS],
            "source_query": self.source_query[:MAX_QUERY_CHARS],
        }


@dataclass(frozen=True, slots=True)
class TriageResult:
    article_id: str
    score: int
    signals: dict[str, Any]


def candidate_from_row(row: Mapping[str, Any]) -> TriageCandidate:
    """Build a candidate from a queue row, tolerating missing metadata."""
    return TriageCandidate(
        article_id=str(row.get("article_id") or ""),
        title=str(row.get("title") or "").strip(),
        domain=str(row.get("domain") or "").strip(),
        source_query=str(row.get("source_query") or "").strip(),
        url=str(row.get("url") or "").strip(),
        coverage_domain=str(row.get("coverage_domain") or "core").strip() or "core",
        document_type=(str(row.get("document_type") or "").strip() or None),
    )


def normalized_title(title: str) -> str:
    """Fold a headline to a comparison key shared by syndicated copies."""
    text = unicodedata.normalize("NFKC", title or "").casefold()
    text = _WHITESPACE_RE.sub(" ", text).strip()
    if not text:
        return ""
    text = _TITLE_SUFFIX_RE.split(text, maxsplit=1)[0]
    text = _PUNCTUATION_RE.sub(" ", text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def _registrable_match(domain: str, known: frozenset[str]) -> bool:
    host = domain.casefold().strip().removeprefix("www.")
    if not host:
        return False
    return host in known or any(host.endswith(f".{item}") for item in known)


def deterministic_penalty(
    candidate: TriageCandidate,
    *,
    duplicate: bool = False,
    profile: TriageProfile | None = None,
) -> tuple[int, list[str]]:
    """Return the reproducible penalty and its reason codes for one candidate.

    ``profile`` defaults to the candidate's own coverage domain, so existing
    callers keep the core behaviour without passing anything.
    """
    active = profile or profile_for(candidate.coverage_domain)
    penalties = active.penalties
    if not candidate.title.strip():
        return penalties["missing_title"], ["missing_title"]
    reasons: list[str] = []
    total = 0
    if _registrable_match(candidate.domain, active.wire_domains):
        reasons.append("wire_domain")
        total += penalties["wire_domain"]
    path = urlsplit(candidate.url).path if candidate.url else ""
    if path and active.sponsored_path_re.search(path):
        reasons.append("sponsored_path")
        total += penalties["sponsored_path"]
    if active.ticker_title_re.search(candidate.title):
        reasons.append("ticker_title")
        total += penalties["ticker_title"]
    if active.listicle_title_re.search(candidate.title):
        reasons.append("listicle_title")
        total += penalties["listicle_title"]
    if duplicate:
        reasons.append("duplicate_title")
        total += penalties["duplicate_title"]
    return min(total, 100), reasons


def _axis_value(raw: Any) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise TriageValidationError("axis_not_integer")
    if not 0 <= raw <= MAX_AXIS_VALUE:
        raise TriageValidationError("axis_out_of_range")
    return raw


def compose_score(axes: Mapping[str, int], penalty: int) -> int:
    """Blend the axis scores into 0..100 and subtract the deterministic penalty."""
    if penalty < 0:
        raise ValueError("penalty must not be negative")
    weighted = 0.0
    for axis in VALUE_AXES:
        weighted += _axis_value(axes[axis]) * AXIS_WEIGHTS[axis]
    for axis in RISK_AXES:
        weighted += (MAX_AXIS_VALUE - _axis_value(axes[axis])) * AXIS_WEIGHTS[axis]
    base = round(weighted / MAX_AXIS_VALUE * 100)
    return max(0, min(100, base - penalty))


def normalize_triage_response(
    raw: Any,
    candidates: Sequence[TriageCandidate],
    *,
    require_crypto_relevance: bool = False,
) -> dict[str, dict[str, int | str]]:
    """Validate a provider reply into exactly one axis set per candidate.

    The reply addresses candidates by their batch-local ref; this restores the
    article ids locally so a model never has to echo a 64-character hash.
    """
    if not isinstance(raw, dict):
        raise TriageValidationError("response_not_object")
    scores = raw.get("scores")
    if not isinstance(scores, list):
        raise TriageValidationError("scores_not_list")
    by_ref = {ref: candidate for ref, candidate in enumerate(candidates, 1)}
    out: dict[str, dict[str, int | str]] = {}
    seen: set[int] = set()
    for item in scores:
        if not isinstance(item, dict):
            raise TriageValidationError("score_not_object")
        ref = item.get("ref")
        if isinstance(ref, str) and ref.strip().isdigit():
            ref = int(ref.strip())
        if isinstance(ref, bool) or not isinstance(ref, int) or ref not in by_ref:
            raise TriageValidationError("unknown_ref")
        if ref in seen:
            raise TriageValidationError("duplicate_ref")
        seen.add(ref)
        normalized: dict[str, int | str] = {
            axis: _axis_value(item.get(axis)) for axis in SCORE_AXES
        }
        if require_crypto_relevance:
            relevance = item.get("relevance")
            if (
                not isinstance(relevance, str)
                or relevance not in CRYPTO_RELEVANCE_VALUES
            ):
                raise TriageValidationError("relevance_not_enum")
            normalized["relevance"] = str(relevance)
        out[by_ref[ref].article_id] = normalized
    if seen != set(by_ref):
        raise TriageValidationError("missing_ref")
    return out


class TriageScorer:
    """One batched provider call per candidate group, on the cheap model."""

    def __init__(
        self,
        settings: Settings,
        *,
        request_limiter: OpenRouterRateLimiter | None = None,
        client_factory: Any | None = None,
    ):
        self.s = settings.model_copy(
            update={"openrouter_model": settings.triage_scoring_model}
        )
        self.model = self.s.openrouter_model
        self._request_limiter = request_limiter
        self._client_factory = client_factory or (
            lambda: OpenRouterClient(self.s, request_limiter=self._request_limiter)
        )

    @property
    def configured(self) -> bool:
        return bool(self.s.openrouter_api_key)

    async def score(
        self,
        candidates: Sequence[TriageCandidate],
        *,
        require_crypto_relevance: bool = False,
    ) -> dict[str, dict[str, int | str]]:
        """Return validated axis scores keyed by article_id."""
        if not candidates:
            return {}
        if require_crypto_relevance:
            system, user_text = build_crypto_triage_prompt(candidates)
        else:
            # Keep the core prompt and payload byte-for-byte identical.
            system, user_text = build_triage_prompt(candidates)
        async with self._client_factory() as llm:
            raw = await llm.complete_json(system, user_text)
        return normalize_triage_response(
            raw,
            candidates,
            require_crypto_relevance=require_crypto_relevance,
        )


def build_results(
    candidates: Sequence[TriageCandidate],
    *,
    axes_by_id: Mapping[str, Mapping[str, int | str]],
    duplicates: set[str],
    mode: str,
) -> list[TriageResult]:
    """Combine model axes and deterministic signals into storable results.

    A titled candidate without axes is a scorer fallback: it keeps the neutral
    score minus its deterministic penalty so ranking degrades instead of
    stalling. An untitled candidate needs no model call at all.
    """
    results: list[TriageResult] = []
    for candidate in candidates:
        active = profile_for(candidate.coverage_domain)
        penalty, reasons = deterministic_penalty(
            candidate,
            duplicate=candidate.article_id in duplicates,
            profile=active,
        )
        axes = axes_by_id.get(candidate.article_id)
        if axes is None:
            score = max(0, NEUTRAL_SCORE - penalty)
            signals: dict[str, Any] = {
                "axes": None,
                "penalty": penalty,
                "reasons": reasons,
                "mode": mode,
                "fallback": bool(candidate.title.strip()),
                "profile": active.name,
            }
        else:
            numeric_axes = {axis: axes[axis] for axis in SCORE_AXES}
            score = compose_score(numeric_axes, penalty)  # type: ignore[arg-type]
            signals = {
                "axes": numeric_axes,
                "penalty": penalty,
                "reasons": reasons,
                "mode": mode,
                "fallback": False,
                "profile": active.name,
            }
            if crypto_relevance_required(
                coverage_domain=candidate.coverage_domain,
                document_type=candidate.document_type,
            ):
                relevance = axes.get("relevance")
                if (
                    not isinstance(relevance, str)
                    or relevance not in CRYPTO_RELEVANCE_VALUES
                ):
                    raise TriageValidationError("relevance_not_enum")
                signals.update(
                    {
                        "relevance": relevance,
                        "prompt_version": CRYPTO_TRIAGE_PROMPT_VERSION,
                        "profile_version": CRYPTO_TRIAGE_PROFILE_VERSION,
                    }
                )
        results.append(
            TriageResult(
                article_id=candidate.article_id,
                score=score,
                signals=signals,
            )
        )
    return results


def _measurement_value(row: Mapping[str, object], key: str) -> object:
    if key in row:
        return row[key]
    if key == "score" and "triage_score" in row:
        return row["triage_score"]
    if key == "model" and "triage_model" in row:
        return row["triage_model"]
    signals = row.get("signals", row.get("triage_signals"))
    if isinstance(signals, Mapping):
        return signals.get(key)
    return None


def _rank_payload(
    rows: Sequence[dict[str, object]], *, top_k: int
) -> tuple[list[dict[str, object]], float]:
    ranked = sorted(rows, key=lambda item: (-int(item["score"]), int(item["index"])))
    selected = ranked[:top_k]
    correct = sum(bool(item["on_topic"]) for item in selected)
    payload = [
        {
            "sample_id": item["sample_id"],
            "duplicate_family": item["duplicate_family"],
            "score": item["score"],
            "on_topic": item["on_topic"],
        }
        for item in selected
    ]
    return payload, correct / top_k


def _collapse_families(rows: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    representatives: dict[str, dict[str, object]] = {}
    for row in rows:
        family = str(row["duplicate_family"])
        previous = representatives.get(family)
        if previous is None or (-int(row["score"]), int(row["index"])) < (
            -int(previous["score"]),
            int(previous["index"]),
        ):
            representatives[family] = row
    return list(representatives.values())


def measure_crypto_relevance_gate(
    rows: Sequence[Mapping[str, object]], *, top_k: int = CRYPTO_RELEVANCE_TOP_K
) -> dict[str, object]:
    """Measure the fixed crypto relevance sample without doing any I/O.

    Only provider-accepted ``on_topic`` candidates participate in ranking,
    mirroring the runtime claim predicate. Missing accepted slots count against
    precision@K instead of shrinking the denominator.
    """
    if not 1 <= top_k <= CRYPTO_RELEVANCE_SAMPLE_SIZE:
        raise ValueError("top_k must be between 1 and 50")

    reasons: list[str] = []
    if len(rows) != CRYPTO_RELEVANCE_SAMPLE_SIZE:
        reasons.append("sample_size_not_50")

    normalized: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    models: set[str] = set()
    subtype_counts = {"article": 0, "document": 0}
    for index, row in enumerate(rows):
        sample_id = str(row.get("sample_id") or "").strip()
        subtype = str(row.get("subtype") or "").strip()
        family = str(row.get("duplicate_family") or "").strip()
        human_label = row.get("on_topic")
        score = _measurement_value(row, "score")
        relevance = _measurement_value(row, "relevance")
        fallback = _measurement_value(row, "fallback")
        profile = _measurement_value(row, "profile")
        profile_version = _measurement_value(row, "profile_version")
        prompt_version = _measurement_value(row, "prompt_version")
        model = str(_measurement_value(row, "model") or "").strip()

        valid = True
        if not sample_id or sample_id in seen_ids:
            valid = False
        else:
            seen_ids.add(sample_id)
        if subtype not in subtype_counts:
            valid = False
        else:
            subtype_counts[subtype] += 1
        if not family or not isinstance(human_label, bool):
            valid = False
        if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
            valid = False
        if (
            not isinstance(relevance, str)
            or relevance not in CRYPTO_RELEVANCE_VALUES
            or fallback is not False
        ):
            valid = False
        if (
            profile != "crypto"
            or profile_version != CRYPTO_TRIAGE_PROFILE_VERSION
            or prompt_version != CRYPTO_TRIAGE_PROMPT_VERSION
            or not model
        ):
            valid = False
        if not valid:
            reasons.append("provider_decision_incomplete")
            continue
        models.add(model)
        normalized.append(
            {
                "index": index,
                "sample_id": sample_id,
                "subtype": subtype,
                "duplicate_family": family,
                "on_topic": human_label,
                "score": score,
                "relevance": relevance,
            }
        )

    if subtype_counts != {"article": 43, "document": 7}:
        reasons.append("sample_strata_not_43_7")
    if len(models) != 1:
        reasons.append("provider_model_identity_not_single")

    all_families = {
        str(item["duplicate_family"])
        for item in normalized
    }
    positive_article_families = {
        str(item["duplicate_family"])
        for item in normalized
        if item["subtype"] == "article" and item["on_topic"] is True
    }
    if len(all_families) < top_k:
        reasons.append("fewer_than_twenty_families")
    if len(positive_article_families) < top_k:
        reasons.append("fewer_than_twenty_positive_article_families")

    # De-duplicate stable reason codes without hiding the first detection order.
    reasons = list(dict.fromkeys(reasons))
    identity = {
        "model": next(iter(models)) if len(models) == 1 else None,
        "prompt_version": CRYPTO_TRIAGE_PROMPT_VERSION,
        "profile_version": CRYPTO_TRIAGE_PROFILE_VERSION,
    }
    base: dict[str, object] = {
        "status": "inconclusive" if reasons else "pending",
        "reasons": reasons,
        "sample_size": len(rows),
        "subtype_counts": subtype_counts,
        "family_count": len(all_families),
        "positive_article_family_count": len(positive_article_families),
        "identity": identity,
    }
    if reasons:
        return base

    accepted = [item for item in normalized if item["relevance"] == "on_topic"]
    accepted_articles = [item for item in accepted if item["subtype"] == "article"]
    raw_rank, raw_precision = _rank_payload(accepted, top_k=top_k)
    family_rank, family_precision = _rank_payload(
        _collapse_families(accepted), top_k=top_k
    )
    article_family_rank, article_family_precision = _rank_payload(
        _collapse_families(accepted_articles), top_k=top_k
    )
    document_false_positives = sum(
        item["subtype"] == "document"
        and item["relevance"] == "on_topic"
        and item["on_topic"] is False
        for item in normalized
    )
    subtype_confusion: dict[str, dict[str, int]] = {}
    for subtype in ("article", "document"):
        subset = [item for item in normalized if item["subtype"] == subtype]
        subtype_confusion[subtype] = {
            "true_positive": sum(
                item["relevance"] == "on_topic" and item["on_topic"] is True
                for item in subset
            ),
            "false_positive": sum(
                item["relevance"] == "on_topic" and item["on_topic"] is False
                for item in subset
            ),
            "true_negative": sum(
                item["relevance"] == "off_topic" and item["on_topic"] is False
                for item in subset
            ),
            "false_negative": sum(
                item["relevance"] == "off_topic" and item["on_topic"] is True
                for item in subset
            ),
        }
    threshold_count = math.ceil(top_k * CRYPTO_RELEVANCE_MIN_PRECISION)
    passed = (
        family_precision >= CRYPTO_RELEVANCE_MIN_PRECISION
        and article_family_precision >= CRYPTO_RELEVANCE_MIN_PRECISION
        and document_false_positives == 0
    )
    base.update(
        {
            "status": "pass" if passed else "fail",
            "reasons": [] if passed else ["precision_or_document_gate_failed"],
            "top_k": top_k,
            "minimum_correct": threshold_count,
            "raw": {"precision": raw_precision, "ranking": raw_rank},
            "family": {"precision": family_precision, "ranking": family_rank},
            "article_family": {
                "precision": article_family_precision,
                "ranking": article_family_rank,
            },
            "document_false_positives": document_false_positives,
            "subtype_confusion": subtype_confusion,
            "document_confusion": {
                "accepted": sum(
                    item["subtype"] == "document"
                    and item["relevance"] == "on_topic"
                    for item in normalized
                ),
                "rejected": sum(
                    item["subtype"] == "document"
                    and item["relevance"] == "off_topic"
                    for item in normalized
                ),
            },
        }
    )
    return base
