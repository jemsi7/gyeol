"""Domain-scoped triage penalty profiles (I-13200-e, DR-13200).

The crypto lane inherits every wire and sponsored rule unchanged -- those are
facts about a URL, not about a topic -- and differs only where the ticker
pattern misreads ordinary crypto reporting as market slop.
"""
from __future__ import annotations

import pytest

from app.triage import (
    CORE_PROFILE,
    CRYPTO_PROFILE,
    PENALTIES,
    PROFILES,
    TriageCandidate,
    build_results,
    candidate_from_row,
    deterministic_penalty,
    profile_for,
)


def _candidate(
    title: str,
    *,
    domain: str = "example.com",
    url: str = "https://example.com/a",
    coverage_domain: str = "core",
) -> TriageCandidate:
    return TriageCandidate(
        article_id=f"id-{title[:12]}-{coverage_domain}",
        title=title,
        domain=domain,
        source_query="q",
        url=url,
        coverage_domain=coverage_domain,
    )


def test_core_profile_reuses_the_shipped_penalty_table():
    assert CORE_PROFILE.penalties is PENALTIES
    assert PROFILES["core"] is CORE_PROFILE
    assert PROFILES["crypto"] is CRYPTO_PROFILE


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Shares rise after the earnings call", 25),
        ("Nvidia stock price target raised", 25),
        ("Bitcoin market cap hits $2T after ETF inflows", 25),
        ("Ether 52-week high revisited", 25),
        ("XRP price prediction 2027", 25),
    ],
)
def test_core_ticker_rules_are_unchanged(title, expected):
    penalty, reasons = deterministic_penalty(_candidate(title))
    assert reasons == ["ticker_title"]
    assert penalty == expected


def test_crypto_allows_market_cap_and_52_week_headlines():
    for title in (
        "Bitcoin market cap hits $2T after ETF inflows",
        "Ether 52-week high revisited",
    ):
        penalty, reasons = deterministic_penalty(
            _candidate(title, coverage_domain="crypto")
        )
        assert reasons == []
        assert penalty == 0


def test_crypto_still_penalises_price_prediction_at_a_lower_weight():
    penalty, reasons = deterministic_penalty(
        _candidate("XRP price prediction 2027", coverage_domain="crypto")
    )
    assert reasons == ["ticker_title"]
    assert penalty == 10


def test_same_headline_scores_differently_by_domain():
    title = "Bitcoin market cap hits $2T after ETF inflows"
    core, _ = deterministic_penalty(_candidate(title))
    crypto, _ = deterministic_penalty(_candidate(title, coverage_domain="crypto"))
    assert core == 25
    assert crypto == 0


def test_wire_and_sponsored_rules_are_domain_agnostic():
    for coverage_domain in ("core", "crypto"):
        wire, wire_reasons = deterministic_penalty(
            _candidate(
                "A protocol upgrade shipped",
                domain="prnewswire.com",
                coverage_domain=coverage_domain,
            )
        )
        assert wire_reasons == ["wire_domain"]
        assert wire == 30

        sponsored, sponsored_reasons = deterministic_penalty(
            _candidate(
                "A protocol upgrade shipped",
                url="https://example.com/sponsored/post",
                coverage_domain=coverage_domain,
            )
        )
        assert sponsored_reasons == ["sponsored_path"]
        assert sponsored == 30


def test_missing_title_short_circuits_in_every_profile():
    for coverage_domain in ("core", "crypto"):
        penalty, reasons = deterministic_penalty(
            _candidate("   ", coverage_domain=coverage_domain)
        )
        assert (penalty, reasons) == (100, ["missing_title"])


def test_unregistered_domain_uses_the_core_profile():
    assert profile_for("sports") is CORE_PROFILE
    assert profile_for("") is CORE_PROFILE
    assert profile_for("  CRYPTO ") is CRYPTO_PROFILE


def test_candidate_from_row_reads_the_domain_and_defaults_to_core():
    assert candidate_from_row({"article_id": "a"}).coverage_domain == "core"
    assert (
        candidate_from_row({"article_id": "a", "coverage_domain": "crypto"})
        .coverage_domain
        == "crypto"
    )
    assert (
        candidate_from_row({"article_id": "a", "coverage_domain": None})
        .coverage_domain
        == "core"
    )


def test_prompt_payload_never_leaks_the_coverage_domain():
    payload = _candidate("A title", coverage_domain="crypto").prompt_dict(1)
    assert "coverage_domain" not in payload


def test_build_results_records_which_profile_scored_each_candidate():
    candidates = [
        _candidate("Bitcoin market cap hits $2T", coverage_domain="crypto"),
        _candidate("Bitcoin market cap hits $2T"),
    ]
    results = build_results(
        candidates,
        axes_by_id={},
        duplicates=set(),
        mode="shadow",
    )
    profiles = {result.article_id: result.signals["profile"] for result in results}
    assert profiles == {
        candidates[0].article_id: "crypto",
        candidates[1].article_id: "core",
    }
    # Same headline, different profile, therefore different fallback score.
    scores = {result.article_id: result.score for result in results}
    assert scores[candidates[0].article_id] > scores[candidates[1].article_id]
