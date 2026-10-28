"""Bounded prompt for headline-level Map candidate triage."""
from __future__ import annotations

import json
from typing import Any, Sequence


CRYPTO_TRIAGE_PROMPT_VERSION = "crypto-relevance-v1"


TRIAGE_SYSTEM_PROMPT = """You rank news candidates for a Korean long-form reading app.
You see only the headline, the publishing domain and the collection query, so judge what a
headline can actually support and never invent article details.

Score every candidate from 0 to 5 on each axis:
durability: still worth reading weeks later.
significance: structural importance beyond one company or one day.
explanatory_value: promises mechanism, context or analysis rather than a bare update.
ephemeral_risk: score tick, price move, rumor, schedule note, anything stale tomorrow.
slop_risk: promotional, SEO rewrite, listicle, deal roundup, engagement bait.

The last two are risks: higher means worse. Do not reward hype, superlatives or urgency.
Return one JSON object and no prose, with exactly one entry per supplied ref and no others.
Copy each ref back exactly as the integer it was given. Exact shape:
{"scores":[{"ref":1,"durability":0,"significance":0,"explanatory_value":0,
"ephemeral_risk":0,"slop_risk":0}]}"""


CRYPTO_TRIAGE_SYSTEM_PROMPT = """You rank cryptocurrency and blockchain candidates for a Korean long-form reading app.
You see only the headline, the publishing domain and the collection query, so judge what a
headline explicitly supports and never infer relevance from an allowlisted source or query.

Score every candidate from 0 to 5 on each axis:
durability: still worth reading weeks later.
significance: structural importance beyond one company, token price, or one day.
explanatory_value: promises mechanism, context or analysis rather than a bare update.
ephemeral_risk: price move, rumor, schedule note, anything stale tomorrow.
slop_risk: promotional, SEO rewrite, prediction bait, listicle, or engagement bait.

Also classify relevance using exactly one enum value:
on_topic: the headline itself is explicitly about cryptocurrency, blockchain, a protocol,
digital-asset governance/security/regulation, or a directly named crypto asset or institution.
off_topic: the crypto connection is absent, merely inferred from the source/query, or unclear.

The last two numeric axes are risks: higher means worse. Do not reward hype, superlatives or
urgency. Return one JSON object and no prose, with exactly one entry per supplied ref and no
others. Copy each ref back exactly as the integer it was given. Exact shape:
{"scores":[{"ref":1,"durability":0,"significance":0,"explanatory_value":0,
"ephemeral_risk":0,"slop_risk":0,"relevance":"on_topic"}]}"""


def build_triage_prompt(candidates: Sequence[Any]) -> tuple[str, str]:
    """Return the system prompt and the bounded JSON user payload.

    Candidates are addressed by a batch-local integer ref: article ids are
    64-character hashes that models do not reproduce reliably.
    """
    user = {
        "task": "Score each candidate headline for lasting reading value.",
        "candidates": [
            candidate.prompt_dict(ref) for ref, candidate in enumerate(candidates, 1)
        ],
    }
    return TRIAGE_SYSTEM_PROMPT, json.dumps(
        user, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def build_crypto_triage_prompt(candidates: Sequence[Any]) -> tuple[str, str]:
    """Return the crypto-only prompt with the binary relevance contract."""
    user = {
        "task": (
            "Score each candidate headline for lasting reading value and classify "
            "whether the headline itself is explicitly crypto/blockchain on-topic."
        ),
        "candidates": [
            candidate.prompt_dict(ref) for ref, candidate in enumerate(candidates, 1)
        ],
    }
    return CRYPTO_TRIAGE_SYSTEM_PROMPT, json.dumps(
        user, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


__all__ = [
    "CRYPTO_TRIAGE_PROMPT_VERSION",
    "CRYPTO_TRIAGE_SYSTEM_PROMPT",
    "TRIAGE_SYSTEM_PROMPT",
    "build_crypto_triage_prompt",
    "build_triage_prompt",
]
