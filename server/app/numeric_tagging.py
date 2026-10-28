"""What a number in a crypto article is actually about (C-13203).

This is the largest technical unknown in the coverage extension. "480억 달러"
may be Ethereum's whole TVL or one protocol's, and the difference is only in
the surrounding prose -- so a wrong subject is not a wrong label, it is wrong
information shown to a reader.

Everything here therefore biases toward not tagging. A deterministic pass first
throws away numbers with no unit context at all, one bounded model call labels
what survives, and four independent checks drop anything the evidence does not
support. Producing no tags is the normal outcome, not a failure.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal, Mapping, Sequence

from .map_output_schemas import (
    StructuredOutputSpec,
    _array,
    _object,
    _string,
    _validated_spec,
)

MeaningTag = Literal[
    "price", "marketcap", "tvl", "supply", "holdings", "fee", "hashrate"
]
SubjectKind = Literal["asset", "protocol", "chain"]

MEANING_TAGS: Final[tuple[str, ...]] = (
    "price",
    "marketcap",
    "tvl",
    "supply",
    "holdings",
    "fee",
    "hashrate",
)
SUBJECT_KINDS: Final[tuple[str, ...]] = ("asset", "protocol", "chain")

# The unit has to be *adjacent* to the number, not merely somewhere in the
# sentence. "fell to $48 billion while 3 people watched" contains a scale word,
# but only one of those numbers is a market value; a window-based test admits
# both and hands the model work it should never have been given.
CURRENCY_PREFIX_RE: Final = re.compile(r"(\$|₩|€|£)\s*$")
METRIC_PREFIX_RE: Final = re.compile(
    r"(tvl|시총|시가총액|market\s*cap|유통량|공급량|supply|circulating"
    r"|보유(량)?|holdings?|reserves?|수수료|fee|gas"
    r"|해시레이트|hashrate|hash\s*rate)"
    r"[\s:은는이가의를을,]*$",
    re.IGNORECASE,
)
UNIT_SUFFIX_RE: Final = re.compile(
    r"^\s*(억|조|만|billion|million|trillion|thousand"
    r"|달러|원|dollars?|usd|krw|eth|btc"
    r"|th/s|eh/s|ph/s|gwei)\b",
    re.IGNORECASE,
)
# A percentage is a change, not a level: there is nothing current to look up.
PERCENT_SUFFIX_RE: Final = re.compile(r"^\s*(%|퍼센트|percent)")
ADJACENCY_CHARS: Final[int] = 24
NUMBER_RE: Final = re.compile(r"\d[\d,]*(?:\.\d+)?")
SUBJECT_ID_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

CONTEXT_WINDOW_CHARS: Final[int] = 120
MAX_CANDIDATES: Final[int] = 40
MAX_ATOM_CHARS: Final[int] = 40
DEFAULT_MIN_CONFIDENCE: Final[float] = 0.7


@dataclass(frozen=True, slots=True)
class AtomCandidate:
    claim_id: str
    atom: str
    context: str


@dataclass(frozen=True, slots=True)
class TaggedAtom:
    claim_id: str
    atom: str
    tag: str
    subject_kind: str
    subject_id: str
    as_of: datetime
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "atom": self.atom,
            "tag": self.tag,
            "subject_kind": self.subject_kind,
            "subject_id": self.subject_id,
            "as_of": self.as_of.isoformat(),
            "confidence": round(self.confidence, 3),
        }


def _context_window(text: str, start: int, end: int) -> str:
    left = max(0, start - CONTEXT_WINDOW_CHARS)
    right = min(len(text), end + CONTEXT_WINDOW_CHARS)
    return text[left:right]


def _has_adjacent_unit(text: str, start: int, end: int) -> bool:
    """True when a unit or metric word sits directly against the number."""
    before = text[max(0, start - ADJACENCY_CHARS) : start]
    after = text[end : end + ADJACENCY_CHARS]
    if PERCENT_SUFFIX_RE.match(after):
        return False
    if UNIT_SUFFIX_RE.match(after):
        return True
    return bool(
        CURRENCY_PREFIX_RE.search(before) or METRIC_PREFIX_RE.search(before)
    )


def candidate_atoms(ledger: Mapping[str, Any]) -> tuple[AtomCandidate, ...]:
    """Numbers worth asking about, in a stable order.

    Deterministic on purpose: the same ledger must always produce the same
    question, or the measurement in I-13203-b measures nothing.
    """
    seen: set[tuple[str, str]] = set()
    candidates: list[AtomCandidate] = []
    for claim in ledger.get("claims") or ():
        if not isinstance(claim, dict):
            continue
        claim_id = str(claim.get("id") or "")
        if not claim_id:
            continue
        for span in claim.get("evidence") or ():
            if not isinstance(span, dict):
                continue
            quote = str(span.get("quote") or "")
            for match in NUMBER_RE.finditer(quote):
                atom = match.group().replace(",", "")
                if len(atom) > MAX_ATOM_CHARS:
                    continue
                if not _has_adjacent_unit(quote, match.start(), match.end()):
                    continue
                window = _context_window(quote, match.start(), match.end())
                key = (claim_id, atom)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(
                    AtomCandidate(claim_id=claim_id, atom=atom, context=window)
                )
    return tuple(candidates[:MAX_CANDIDATES])


def numeric_tagging_output_schema(
    candidates: Sequence[AtomCandidate],
) -> StructuredOutputSpec:
    """One decision per candidate, addressed by its batch-local ref."""
    if not candidates:
        raise ValueError("numeric tagging schema needs at least one candidate")
    refs = [str(index) for index in range(1, len(candidates) + 1)]
    decision = _object(
        {
            "ref": _string(enum=refs),
            "tag": _string(enum=list(MEANING_TAGS)),
            "subject_kind": _string(enum=list(SUBJECT_KINDS)),
            "subject_id": _string(min_length=1, max_length=64),
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        }
    )
    schema = _object(
        {"tags": _array(decision, max_items=len(candidates))}
    )
    return _validated_spec("article_map_numeric_tags_v1", schema)


def normalize_tagging_response(
    raw: Any,
    candidates: Sequence[AtomCandidate],
    *,
    as_of: datetime,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> tuple[TaggedAtom, ...]:
    """Keep only decisions the evidence and the schema both support."""
    if not isinstance(raw, dict) or not candidates:
        return ()
    by_ref = {str(index): item for index, item in enumerate(candidates, 1)}
    accepted: dict[tuple[str, str], TaggedAtom] = {}
    conflicted: set[tuple[str, str]] = set()
    for item in raw.get("tags") if isinstance(raw.get("tags"), list) else []:
        if not isinstance(item, dict):
            continue
        candidate = by_ref.get(str(item.get("ref")))
        if candidate is None:
            continue
        tag = str(item.get("tag") or "")
        subject_kind = str(item.get("subject_kind") or "")
        subject_id = str(item.get("subject_id") or "").strip()
        if tag not in MEANING_TAGS or subject_kind not in SUBJECT_KINDS:
            continue
        if not SUBJECT_ID_RE.fullmatch(subject_id):
            continue
        confidence = item.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            continue
        if not 0.0 <= float(confidence) <= 1.0 or float(confidence) < min_confidence:
            continue
        key = (candidate.claim_id, candidate.atom)
        existing = accepted.get(key)
        if existing is not None and (
            existing.tag != tag
            or existing.subject_kind != subject_kind
            or existing.subject_id != subject_id
        ):
            # Two different readings of the same number is exactly the
            # ambiguity this unit must not resolve by guessing.
            conflicted.add(key)
            continue
        accepted[key] = TaggedAtom(
            claim_id=candidate.claim_id,
            atom=candidate.atom,
            tag=tag,
            subject_kind=subject_kind,
            subject_id=subject_id,
            as_of=as_of,
            confidence=float(confidence),
        )
    return tuple(
        atom for key, atom in accepted.items() if key not in conflicted
    )


def build_tagging_prompt(candidates: Sequence[AtomCandidate]) -> str:
    import json

    payload = {
        "candidates": [
            {
                "ref": str(index),
                "number": candidate.atom,
                "context": candidate.context,
            }
            for index, candidate in enumerate(candidates, 1)
        ]
    }
    return (
        "Label only the numbers whose subject the surrounding context makes "
        "certain.\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


TAGGING_SYSTEM_PROMPT: Final[str] = """You label numbers taken from a crypto news
article so a later step can look up today's value of the same thing. Return one
JSON object and nothing else.

For each candidate decide what the number measures (`tag`) and what it measures
it for (`subject_kind` and `subject_id`). Use the asset's ticker in uppercase
for `asset`, and a lowercase slug for `protocol` or `chain`.

Only label a number that states a level at the time of writing. Skip a number
that is a change, a percentage, a past comparison, a forecast, a count of
things, a date, or a block height.

`confidence` is how certain the subject is, not how certain the tag is. When
the context names more than one asset or protocol near the number, or leaves
the subject implicit, return a low confidence. Omitting a candidate entirely is
always acceptable and is the right answer whenever the context does not settle
the subject."""
