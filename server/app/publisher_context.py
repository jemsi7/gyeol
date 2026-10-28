"""One sentence about where an article came from (DR-13203, DR-13245).

No model writes any of it. Every sentence a reader can see is one of the
literals in ``SENTENCE_TEMPLATES``; only the owner and asset slots are filled.

Two axes reach that dict. The registry axis is deterministic -- assembled from
registry facts, the URL path and the wire-domain list -- and states its
relationship outright. The researched axis (DR-13245) covers publishers the
registry does not know, is backed by URLs the researcher actually opened, and
states its relationship as a possibility rather than a fact. That difference in
strength is carried by the template wording, which is why the templates are
literals and not a format string someone can assemble.

The inferred axis -- how promotional a piece reads -- is still computed and
stored in ``map_queue.triage_signals``. It stays there. Publishing it would be
an assertion about a named outlet that nothing checkable backs up.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Sequence
from urllib.parse import urlsplit

from .publishers import PublisherRecord, PublisherRegistry
from .triage import TriageProfile, profile_for

# Beyond this many distinct assets the article is a roundup, and "this
# publisher has an interest in the asset it covers" stops being a statement
# about anything in particular.
MAX_SUBJECT_ASSETS_FOR_INTEREST: Final[int] = 15
MAX_SENTENCE_CHARS: Final[int] = 120
MAX_OWNER_SLOT_CHARS: Final[int] = 40

SENTENCE_TEMPLATES: Final[dict[str, str]] = {
    "exchange_listed": "{owner}이(가) 발행했고, 자사에 상장된 자산을 다룹니다.",
    "project_own": "{owner}이(가) 발행했고, 자사 프로토콜을 다룹니다.",
    "investor_portfolio": "{owner}이(가) 소유한 매체가 발행했고, 투자 포트폴리오 자산을 다룹니다.",
    "wire": "유료 배포 와이어를 통해 배포된 자료입니다.",
    "sponsored_path": "발행처가 광고·후원 콘텐츠 경로로 게시한 자료입니다.",
    "individual": "{owner} 개인 리서처가 발행했습니다.",
    # DR-13245. Web-researched, so these state a possibility. The registry
    # sentences above state a fact; nothing may blur the two.
    "researched_exchange_listed": (
        "{owner}이(가) 발행했고, 공개된 자료 기준 자사에 {asset}이(가) 상장된 것으로 보입니다."
    ),
    "researched_project_own": (
        "{owner}이(가) 발행했고, 공개된 자료 기준 {asset} 프로토콜을 직접 운영할 가능성이 있습니다."
    ),
    "researched_investor_portfolio": (
        "{owner}이(가) 소유한 매체로 보이며, 공개된 자료 기준 {asset}에 투자한 이력이 있습니다."
    ),
}

# The reader-facing half of DR-13203's rule, in code rather than only in a
# test: "피해야 할 표현" from service_description.md plus the judgement words
# this feature specifically rules out. Slot values are checked against it
# because a slot is the one part of a sentence a template cannot fix.
BANNED_SENTENCE_TERMS: Final[tuple[str, ...]] = (
    "편향", "중립", "팩트체크", "가장 정확", "실시간", "가장 빠른",
    "원문 전체", "원문 그대로", "전수 검수", "모든 분야",
    "주의", "신뢰도", "낮음", "높음", "위험", "의심", "홍보물", "광고성",
)

RESEARCHED_TEMPLATE_BY_RELATION: Final[dict[str, str]] = {
    "exchange_listed": "researched_exchange_listed",
    "project_own": "researched_project_own",
    "investor_portfolio": "researched_investor_portfolio",
}

# Owner names only: letters, digits, spaces and the punctuation that appears in
# real outlet names. Anything else -- markup, URLs, sentence punctuation -- is a
# sign the slot is carrying something other than a name.
OWNER_SLOT_RE: Final[re.Pattern[str]] = re.compile(r"[\w\s·.&()\-]+", re.UNICODE)
ASSET_SLOT_RE: Final[re.Pattern[str]] = re.compile(r"[A-Z0-9]{2,10}")


def owner_slot_is_allowed(owner: str) -> bool:
    """A slot value that may be dropped into a reader-facing template."""
    value = (owner or "").strip()
    if not 1 <= len(value) <= MAX_OWNER_SLOT_CHARS:
        return False
    if not OWNER_SLOT_RE.fullmatch(value):
        return False
    return not any(term in value for term in BANNED_SENTENCE_TERMS)


def asset_slot_is_allowed(asset: str) -> bool:
    return bool(ASSET_SLOT_RE.fullmatch((asset or "").strip()))


@dataclass(frozen=True, slots=True)
class PublisherContext:
    sentence: str
    basis: tuple[str, ...]
    publisher_type: str
    owner: str

    @property
    def is_empty(self) -> bool:
        return not self.sentence


EMPTY_CONTEXT: Final[PublisherContext] = PublisherContext("", (), "", "")


def _interest_rule(
    record: PublisherRecord,
    subject_assets: frozenset[str],
) -> str | None:
    if len(subject_assets) > MAX_SUBJECT_ASSETS_FOR_INTEREST:
        return None
    if not record.interest_in(subject_assets):
        return None
    return {
        "exchange": "exchange_listed",
        "project": "project_own",
        "investor_owned": "investor_portfolio",
    }.get(record.publisher_type)


def _matched_rules(
    *,
    url: str,
    domain: str,
    subject_assets: frozenset[str],
    registry: PublisherRegistry,
    profile: TriageProfile,
) -> tuple[list[str], PublisherRecord | None]:
    """Every rule this article satisfies, most informative first."""
    record = registry.lookup(domain)
    rules: list[str] = []
    if record is not None:
        interest = _interest_rule(record, subject_assets)
        if interest is not None:
            rules.append(interest)
    host = (domain or "").strip().casefold().removeprefix("www.")
    is_wire = (record is not None and record.publisher_type == "wire") or (
        bool(host)
        and any(
            host == known or host.endswith(f".{known}")
            for known in profile.wire_domains
        )
    )
    if is_wire:
        rules.append("wire")
    path = urlsplit(url).path if url else ""
    if path and profile.sponsored_path_re.search(path):
        rules.append("sponsored_path")
    if record is not None and record.publisher_type == "individual":
        rules.append("individual")
    return rules, record


def build_publisher_context(
    *,
    url: str,
    domain: str,
    subject_assets: frozenset[str],
    registry: PublisherRegistry,
    profile: TriageProfile | None = None,
    coverage_domain: str = "crypto",
) -> PublisherContext:
    """Facts only. Empty is a normal, common result."""
    active = profile or profile_for(coverage_domain)
    rules, record = _matched_rules(
        url=url,
        domain=domain,
        subject_assets=subject_assets,
        registry=registry,
        profile=active,
    )
    if not rules:
        return EMPTY_CONTEXT
    # The first rule writes the sentence -- a reader gets one statement, not a
    # list of badges -- but every match is kept so the ordering can be revised
    # later without re-deriving it.
    template = SENTENCE_TEMPLATES[rules[0]]
    owner = record.owner if record is not None else ""
    if "{owner}" in template and not owner:
        return EMPTY_CONTEXT
    sentence = template.format(owner=owner)
    if len(sentence) > MAX_SENTENCE_CHARS:
        return EMPTY_CONTEXT
    return PublisherContext(
        sentence=sentence,
        basis=tuple(rules),
        publisher_type=record.publisher_type if record is not None else "",
        owner=owner,
    )


def build_researched_context(
    *,
    relation: str,
    owner: str,
    asset: str,
    confidence: str,
    evidence_count: int,
) -> PublisherContext:
    """A possibility sentence for a publisher the registry does not know.

    The second of two checkpoints: the researcher already validated these
    values against what it read, and this one refuses anything that would put
    an unchecked string in front of a reader. Empty is a normal result -- an
    uncertain judgement and a judgement without evidence both end here.
    """
    if confidence != "likely" or evidence_count < 1:
        return EMPTY_CONTEXT
    key = RESEARCHED_TEMPLATE_BY_RELATION.get(relation)
    if key is None:
        return EMPTY_CONTEXT
    clean_owner = (owner or "").strip()
    clean_asset = (asset or "").strip().upper()
    if not owner_slot_is_allowed(clean_owner) or not asset_slot_is_allowed(clean_asset):
        return EMPTY_CONTEXT
    sentence = SENTENCE_TEMPLATES[key].format(owner=clean_owner, asset=clean_asset)
    if len(sentence) > MAX_SENTENCE_CHARS:
        return EMPTY_CONTEXT
    return PublisherContext(
        sentence=sentence,
        basis=(key,),
        # Deliberately blank: a researched relationship is not a registry type,
        # and reusing the field would let a later reader treat it as one.
        publisher_type="",
        owner=clean_owner,
    )


def subject_assets_from_ledger(claims: Sequence[dict]) -> frozenset[str]:
    """Uppercase asset symbols named in the claim evidence.

    Deliberately crude: the interest rules only need to know whether the
    article talks about an asset the publisher has a stake in, and a symbol
    that appears nowhere in the evidence cannot be that asset.
    """
    import re

    symbol_re = re.compile(r"\b[A-Z]{2,6}\b")
    symbols: set[str] = set()
    for claim in claims or ():
        if not isinstance(claim, dict):
            continue
        for span in claim.get("evidence") or ():
            if isinstance(span, dict):
                symbols.update(symbol_re.findall(str(span.get("quote") or "")))
    return frozenset(symbols)
