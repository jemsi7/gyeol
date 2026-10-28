"""Prompt and tool schemas for publisher-interest research (I-13212-b).

Two of the three tools are ours; the search tool is executed by OpenRouter and
never comes back to us as a tool call. What the prompt has to buy, above all, is
restraint: "none" and "uncertain" have to read as correct answers, because the
sentence this research can produce is a public statement about a named outlet.
"""
from __future__ import annotations

from typing import Any, Final, Sequence

PUBLISHER_RESEARCH_PROMPT_VERSION: Final[str] = "publisher-research-v1"

RELATIONS: Final[tuple[str, ...]] = (
    "exchange_listed",
    "project_own",
    "investor_portfolio",
    "none",
)
CONFIDENCE_VALUES: Final[tuple[str, ...]] = ("likely", "uncertain")

PUBLISHER_RESEARCH_SYSTEM_PROMPT = """You establish who published an article and
whether that publisher has a financial interest in the asset the article covers.
You are not judging the article, its quality, or its motives. You are looking for
one checkable relationship.

The relationships, in the order you should prefer them when several fit:
- exchange_listed: the publisher operates a trading venue that lists the asset.
- project_own: the publisher is the protocol, foundation or issuer behind the asset.
- investor_portfolio: the publisher is owned by a firm that has invested in the asset.
- none: no such relationship is established by what you read.

How to work. Search the web for the publisher's own about, ownership, listing or
portfolio pages, and for independent coverage of who owns the outlet. Open the
promising pages with fetch_page and read them. Search again with a different query
when the first results are thin.

Evidence rules, which are absolute:
- Cite only URLs you actually opened with fetch_page or that a search result gave
  you in this session. Never write a URL from memory, and never reconstruct one you
  expect to exist. A submission citing an unopened URL is discarded whole.
- Each evidence quote must be text you actually saw on that page.
- If the pages you read do not settle the relationship, answer relation "none" or
  confidence "uncertain". Both are correct, expected answers and cost you nothing.
  A guess that reads as confident is the only wrong answer here.

The owner field is the publisher's plain name, 40 characters or fewer, without legal
suffixes. Use the Korean form when one is in common use, otherwise the English name.
The asset field must be one of the asset symbols supplied in the task, in uppercase.

Call submit_interest exactly once when you are done, and write no prose."""

_FETCH_TOOL: Final[dict[str, Any]] = {
    "type": "function",
    "function": {
        "name": "fetch_page",
        "description": "Open one public web page and return its title and text.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["url"],
            "properties": {
                "url": {
                    "type": "string",
                    "description": (
                        "Absolute http(s) URL seen in this session's search results."
                    ),
                }
            },
        },
    },
}

_SUBMIT_TOOL: Final[dict[str, Any]] = {
    "type": "function",
    "function": {
        "name": "submit_interest",
        "description": "Report the established relationship, or none, and stop.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["relation", "owner", "asset", "confidence", "evidence"],
            "properties": {
                "relation": {"type": "string", "enum": list(RELATIONS)},
                "owner": {"type": "string"},
                "asset": {"type": "string"},
                "confidence": {"type": "string", "enum": list(CONFIDENCE_VALUES)},
                "evidence": {
                    "type": "array",
                    "maxItems": 4,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["url", "quote"],
                        "properties": {
                            "url": {"type": "string"},
                            "quote": {"type": "string"},
                        },
                    },
                },
            },
        },
    },
}


def build_research_tools(
    *,
    max_search_results: int = 5,
    max_search_uses: int = 4,
) -> list[dict[str, Any]]:
    """Search (executed by OpenRouter), page reads and submission (ours)."""
    return [
        {
            "type": "openrouter:web_search",
            "parameters": {
                "max_results": max(1, int(max_search_results)),
                "max_uses": max(1, int(max_search_uses)),
            },
        },
        {
            "type": _FETCH_TOOL["type"],
            "function": dict(_FETCH_TOOL["function"]),
        },
        {
            "type": _SUBMIT_TOOL["type"],
            "function": dict(_SUBMIT_TOOL["function"]),
        },
    ]


def build_research_messages(
    *,
    domain: str,
    title: str,
    assets: Sequence[str],
) -> list[dict[str, Any]]:
    """The task. Article text is never included -- only what it is about."""
    symbols = ", ".join(sorted({str(asset).strip().upper() for asset in assets if asset}))
    task = (
        f"Publisher domain: {domain}\n"
        f"Article headline: {title}\n"
        f"Asset symbols the article covers: {symbols}\n\n"
        "Establish whether the publisher behind that domain has one of the listed "
        "relationships to one of those assets. Pick the single strongest one."
    )
    return [
        {"role": "system", "content": PUBLISHER_RESEARCH_SYSTEM_PROMPT},
        {"role": "user", "content": task},
    ]
