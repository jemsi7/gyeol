"""Web research into who published an article (C-13212, DR-13245~DR-13247).

Runs only where the registry is silent, and only for articles whose triage
signals already suggested the piece is distribution rather than reporting. The
loop is the curation agent's shape -- bounded turns, bounded tool calls, no
reused call ids -- with one rule of its own that carries the legal weight:

    a URL may be cited as evidence only if we watched the model obtain it.

Search citations and successful page reads are the only two ways a URL enters
``observed_urls``. Everything a model asserts about a page it never opened is
dropped along with the whole submission, so a hallucinated source cannot become
a sentence about a named publisher.

Every failure -- provider error, protocol violation, invalid submission, turn
exhaustion -- returns ``None``. Nothing raises into the Map pipeline.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlsplit, urlunsplit

import httpx

from .publisher_context import asset_slot_is_allowed, owner_slot_is_allowed
from .publisher_research_prompts import (
    CONFIDENCE_VALUES,
    RELATIONS,
    build_research_messages,
    build_research_tools,
)
from .web_fetch import Resolver, WebFetchError, fetch_page

log = logging.getLogger("gdelt.publisher_research")

MAX_RESEARCH_TURNS: Final[int] = 6
MAX_RESEARCH_TOOL_CALLS: Final[int] = 10
MAX_PAGE_FETCHES: Final[int] = 4
MAX_EVIDENCE_ITEMS: Final[int] = 4
MAX_QUOTE_CHARS: Final[int] = 300
# Beyond three assets an article is a roundup, and "this publisher has an
# interest in the asset it covers" stops being about anything in particular.
MAX_SUBJECT_ASSETS_FOR_RESEARCH: Final[int] = 3
RESEARCH_TRIAGE_REASONS: Final[frozenset[str]] = frozenset(
    {"wire_domain", "sponsored_path", "ticker_title", "listicle_title"}
)
RESEARCH_SLOP_RISK_MIN: Final[int] = 3
DEFAULT_USER_AGENT: Final[str] = "GyeolPublisherContext/1.0"
SUBMIT_NUDGE: Final[str] = (
    "Call submit_interest now with what you established. "
    'Answer relation "none" if the pages you read did not settle it.'
)


@dataclass(frozen=True, slots=True)
class InterestEvidence:
    url: str
    quote: str

    def to_dict(self) -> dict[str, str]:
        return {"url": self.url, "quote": self.quote}


@dataclass(frozen=True, slots=True)
class InterestFinding:
    relation: str
    owner: str
    asset: str
    confidence: str
    evidence: tuple[InterestEvidence, ...] = ()

    @property
    def is_displayable(self) -> bool:
        """DR-13245: a possibility sentence still needs a checkable source."""
        return (
            self.relation != "none"
            and self.confidence == "likely"
            and bool(self.evidence)
            and bool(self.owner)
            and bool(self.asset)
        )


def assets_key(assets: frozenset[str] | Sequence[str]) -> str:
    """Stable cache key for one article's asset set."""
    return ",".join(sorted({str(asset).strip().upper() for asset in assets if asset}))


def should_research(
    *,
    has_factual_sentence: bool,
    registered_type: str | None,
    subject_assets: frozenset[str],
    triage_signals: Mapping[str, Any] | None,
) -> bool:
    """The four DR-13247 conditions, all required.

    The registry axis wins whenever it has anything to say: a recorded
    relationship is stronger evidence than anything this module can find.
    """
    if has_factual_sentence:
        return False
    if registered_type not in (None, "", "independent"):
        return False
    if not 1 <= len(subject_assets) <= MAX_SUBJECT_ASSETS_FOR_RESEARCH:
        return False
    if not isinstance(triage_signals, Mapping):
        return False
    reasons = triage_signals.get("reasons")
    if isinstance(reasons, Sequence) and not isinstance(reasons, (str, bytes)):
        if any(str(reason) in RESEARCH_TRIAGE_REASONS for reason in reasons):
            return True
    axes = triage_signals.get("axes")
    if isinstance(axes, Mapping):
        slop_risk = axes.get("slop_risk")
        if isinstance(slop_risk, int) and not isinstance(slop_risk, bool):
            return slop_risk >= RESEARCH_SLOP_RISK_MIN
    return False


def normalize_url(value: str) -> str:
    """Compare URLs the way a reader would: same page, same string.

    Models echo a URL back with a trailing slash or a fragment often enough
    that exact matching would discard sound evidence, and normalising is not a
    weakening -- the host and path still have to be one we actually saw.
    """
    parts = urlsplit((value or "").strip())
    if parts.scheme.casefold() not in ("http", "https") or not parts.netloc:
        return ""
    path = parts.path.rstrip("/")
    return urlunsplit(
        (parts.scheme.casefold(), parts.netloc.casefold(), path, parts.query, "")
    )


def cited_urls(message: Mapping[str, Any]) -> set[str]:
    """URLs the provider's own search tool reported reading."""
    found: set[str] = set()
    annotations = message.get("annotations")
    if not isinstance(annotations, list):
        return found
    for annotation in annotations:
        if not isinstance(annotation, Mapping):
            continue
        if annotation.get("type") != "url_citation":
            continue
        citation = annotation.get("url_citation")
        raw = citation.get("url") if isinstance(citation, Mapping) else annotation.get("url")
        normalized = normalize_url(str(raw or ""))
        if normalized:
            found.add(normalized)
    return found


def validate_finding(
    arguments: Mapping[str, Any] | None,
    *,
    subject_assets: frozenset[str],
    observed_urls: set[str],
) -> InterestFinding | str:
    """Return the finding, or a stable error code the model can correct."""
    if not isinstance(arguments, Mapping):
        return "tool_invalid_arguments"
    relation = str(arguments.get("relation") or "").strip()
    if relation not in RELATIONS:
        return "relation_not_enum"
    confidence = str(arguments.get("confidence") or "").strip()
    if confidence not in CONFIDENCE_VALUES:
        return "confidence_not_enum"
    if relation == "none":
        # A real answer, and worth caching so the next article about the same
        # publisher costs nothing. It carries no slots and needs no evidence.
        return InterestFinding(
            relation="none", owner="", asset="", confidence=confidence
        )
    owner = str(arguments.get("owner") or "").strip()
    if not owner_slot_is_allowed(owner):
        return "owner_not_allowed"
    asset = str(arguments.get("asset") or "").strip().upper()
    if not asset_slot_is_allowed(asset) or asset not in subject_assets:
        return "asset_not_in_subject"
    raw_evidence = arguments.get("evidence")
    if not isinstance(raw_evidence, list) or not 1 <= len(raw_evidence) <= MAX_EVIDENCE_ITEMS:
        return "evidence_missing"
    evidence: list[InterestEvidence] = []
    for item in raw_evidence:
        if not isinstance(item, Mapping):
            return "evidence_missing"
        normalized = normalize_url(str(item.get("url") or ""))
        if not normalized:
            return "evidence_not_observed"
        if normalized not in observed_urls:
            # The rule DR-13245 rests on. One unopened URL voids the lot,
            # because a submission mixing seen and imagined sources tells us
            # nothing about which half the conclusion came from.
            return "evidence_not_observed"
        quote = str(item.get("quote") or "").strip()[:MAX_QUOTE_CHARS]
        if not quote:
            return "evidence_missing"
        evidence.append(InterestEvidence(url=normalized, quote=quote))
    return InterestFinding(
        relation=relation,
        owner=owner,
        asset=asset,
        confidence=confidence,
        evidence=tuple(evidence),
    )


class PublisherInterestResearcher:
    def __init__(
        self,
        llm: Any,
        *,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        fetch_timeout_s: float = 8.0,
        max_search_results: int = 5,
        max_search_uses: int = 4,
        resolver: Resolver | None = None,
    ):
        self.llm = llm
        self._client_factory = client_factory or _default_client
        self._fetch_timeout_s = fetch_timeout_s
        self._max_search_results = max_search_results
        self._max_search_uses = max_search_uses
        self._resolver = resolver

    @property
    def configured(self) -> bool:
        return bool(getattr(self.llm, "configured", False))

    async def research(
        self,
        *,
        domain: str,
        title: str,
        subject_assets: frozenset[str],
    ) -> InterestFinding | None:
        """One bounded investigation. ``None`` means "say nothing"."""
        assets = frozenset(
            str(asset).strip().upper() for asset in subject_assets if str(asset).strip()
        )
        if not assets or not domain:
            return None
        messages = build_research_messages(
            domain=domain, title=title, assets=sorted(assets)
        )
        tools = build_research_tools(
            max_search_results=self._max_search_results,
            max_search_uses=self._max_search_uses,
        )
        session_id = "pubint-" + hashlib.sha256(
            f"{domain}:{assets_key(assets)}".encode("utf-8")
        ).hexdigest()[:24]
        observed: set[str] = set()
        used_call_ids: set[str] = set()
        tool_calls = 0
        fetches = 0
        nudged = False
        client: httpx.AsyncClient | None = None
        try:
            for _turn in range(MAX_RESEARCH_TURNS):
                try:
                    assistant = await self.llm.complete_with_tools(
                        messages=messages,
                        tools=tools,
                        reasoning_effort="high",
                        session_id=session_id,
                    )
                except Exception as exc:  # noqa: BLE001 - research is optional
                    log.info(
                        "publisher research unavailable domain=%s code=%s",
                        domain,
                        type(exc).__name__,
                    )
                    return None
                if not isinstance(assistant, Mapping):
                    return None
                observed |= cited_urls(assistant)
                messages.append(dict(assistant))

                raw_calls = assistant.get("tool_calls")
                if not isinstance(raw_calls, list) or not raw_calls:
                    if nudged:
                        return None
                    nudged = True
                    messages.append({"role": "user", "content": SUBMIT_NUDGE})
                    continue
                tool_calls += len(raw_calls)
                if tool_calls > MAX_RESEARCH_TOOL_CALLS:
                    return None

                for raw_call in raw_calls:
                    parsed = _parse_tool_call(raw_call)
                    if parsed is None:
                        return None
                    call_id, name, arguments = parsed
                    if call_id in used_call_ids:
                        return None
                    used_call_ids.add(call_id)

                    if name == "submit_interest":
                        outcome = validate_finding(
                            arguments,
                            subject_assets=assets,
                            observed_urls=observed,
                        )
                        if isinstance(outcome, InterestFinding):
                            return outcome
                        messages.append(
                            _tool_message(call_id, {"ok": False, "error": outcome})
                        )
                        continue

                    if name != "fetch_page":
                        messages.append(
                            _tool_message(call_id, {"ok": False, "error": "unknown_tool"})
                        )
                        continue

                    if not isinstance(arguments, Mapping) or set(arguments) != {"url"}:
                        messages.append(
                            _tool_message(
                                call_id, {"ok": False, "error": "tool_invalid_arguments"}
                            )
                        )
                        continue
                    if fetches >= MAX_PAGE_FETCHES:
                        messages.append(
                            _tool_message(
                                call_id, {"ok": False, "error": "fetch_budget_exhausted"}
                            )
                        )
                        continue
                    url = str(arguments.get("url") or "")
                    fetches += 1
                    if client is None:
                        client = self._client_factory()
                    try:
                        page = await fetch_page(
                            url,
                            client=client,
                            timeout_s=self._fetch_timeout_s,
                            resolver=self._resolver,
                        )
                    except WebFetchError as exc:
                        messages.append(
                            _tool_message(call_id, {"ok": False, "error": exc.code})
                        )
                        continue
                    except Exception as exc:  # noqa: BLE001 - never fail a Map
                        log.info(
                            "publisher research fetch failed code=%s", type(exc).__name__
                        )
                        messages.append(
                            _tool_message(
                                call_id, {"ok": False, "error": "transport_failed"}
                            )
                        )
                        continue
                    for seen in (url, page.url):
                        normalized = normalize_url(seen)
                        if normalized:
                            observed.add(normalized)
                    messages.append(
                        _tool_message(
                            call_id,
                            {
                                "ok": True,
                                "url": page.url,
                                "title": page.title,
                                "text": page.text,
                            },
                        )
                    )
            return None
        finally:
            if client is not None:
                await client.aclose()


def _default_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(headers={"user-agent": DEFAULT_USER_AGENT})


def _parse_tool_call(raw: Any) -> tuple[str, str, dict[str, Any] | None] | None:
    if not isinstance(raw, Mapping) or raw.get("type") not in (None, "function"):
        return None
    call_id = raw.get("id")
    function = raw.get("function")
    if (
        not isinstance(call_id, str)
        or not 1 <= len(call_id) <= 128
        or not isinstance(function, Mapping)
        or not isinstance(function.get("name"), str)
    ):
        return None
    arguments = function.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (TypeError, ValueError):
            return call_id, function["name"], None
    if not isinstance(arguments, dict):
        return call_id, function["name"], None
    return call_id, function["name"], arguments


def _tool_message(call_id: str, result: Mapping[str, Any]) -> dict[str, str]:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": json.dumps(
            result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
    }
