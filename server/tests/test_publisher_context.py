"""Factual publisher-context sentences (I-13202-b, DR-13203).

Two properties carry the whole decision. The output is deterministic, because
it is assembled from registry facts rather than asked of a model. And no reader
can ever see an evaluative word, because every sentence is one of the literals
in SENTENCE_TEMPLATES and the test below checks all of them against the banned
list in service_description.md.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.publisher_context import (
    BANNED_SENTENCE_TERMS,
    MAX_SUBJECT_ASSETS_FOR_INTEREST,
    SENTENCE_TEMPLATES,
    build_publisher_context,
    build_researched_context,
    subject_assets_from_ledger,
)
from app.publishers import PublisherRecord, PublisherRegistry


# "피해야 할 표현" in docs/service_description.md, plus the judgement words
# DR-13203 rules out for this feature specifically.
BANNED_TERMS = (
    "편향", "중립", "팩트체크", "가장 정확", "실시간", "가장 빠른",
    "원문 전체", "원문 그대로", "전수 검수", "모든 분야",
    "주의", "신뢰도", "낮음", "높음", "위험", "의심", "홍보물", "광고성",
)


def _record(**overrides) -> PublisherRecord:
    values = {
        "domain": "research.example.com",
        "publisher_type": "exchange",
        "owner": "예시거래소",
        "source_url": "https://example.com/listings",
        "verified_on": date(2026, 8, 9),
        "listed_assets": frozenset({"BTC", "ETH"}),
    }
    values.update(overrides)
    return PublisherRecord(**values)


def _registry(*records: PublisherRecord) -> PublisherRegistry:
    return PublisherRegistry(records)


def _build(registry, *, url="https://research.example.com/a", assets=("BTC",),
           domain="research.example.com"):
    return build_publisher_context(
        url=url,
        domain=domain,
        subject_assets=frozenset(assets),
        registry=registry,
    )


def test_exchange_covering_its_own_listing():
    context = _build(_registry(_record()))
    assert context.sentence == "예시거래소이(가) 발행했고, 자사에 상장된 자산을 다룹니다."
    assert context.basis == ("exchange_listed",)
    assert context.publisher_type == "exchange"


def test_exchange_without_overlap_says_nothing():
    context = _build(_registry(_record()), assets=("SOL",))
    assert context.is_empty
    assert context.basis == ()


def test_project_covering_its_own_protocol():
    record = _record(
        publisher_type="project",
        owner="이더리움 재단",
        listed_assets=frozenset(),
        protocol_assets=frozenset({"ETH"}),
    )
    assert "자사 프로토콜" in _build(_registry(record), assets=("ETH",)).sentence


def test_investor_owned_outlet_covering_its_portfolio():
    record = _record(
        publisher_type="investor_owned",
        owner="예시캐피탈",
        listed_assets=frozenset(),
        portfolio_assets=frozenset({"SOL"}),
    )
    context = _build(_registry(record), assets=("SOL",))
    assert "투자 포트폴리오" in context.sentence


def test_unregistered_wire_domain_is_still_a_fact():
    # The wire list is a fact about distribution, so it works with no registry.
    context = _build(
        _registry(),
        url="https://prnewswire.com/news/x",
        domain="prnewswire.com",
        assets=(),
    )
    assert context.sentence == SENTENCE_TEMPLATES["wire"]
    assert context.basis == ("wire",)


def test_sponsored_path_is_read_from_the_url():
    context = _build(
        _registry(),
        url="https://news.test/sponsored/a-post",
        domain="news.test",
        assets=(),
    )
    assert context.basis == ("sponsored_path",)


def test_unregistered_ordinary_publisher_says_nothing():
    context = _build(_registry(), url="https://news.test/a", domain="news.test",
                     assets=("BTC",))
    assert context.is_empty


def test_independent_registry_entry_says_nothing():
    record = _record(publisher_type="independent", owner="", listed_assets=frozenset())
    assert _build(_registry(record)).is_empty


def test_first_rule_writes_the_sentence_and_every_match_is_kept():
    context = _build(
        _registry(_record()),
        url="https://research.example.com/sponsored/a",
    )
    # Interest outranks distribution, but the sponsored path is not forgotten.
    assert "자사에 상장된" in context.sentence
    assert context.basis == ("exchange_listed", "sponsored_path")


def test_roundup_articles_drop_the_interest_rules():
    many = tuple(f"A{index}" for index in range(MAX_SUBJECT_ASSETS_FOR_INTEREST + 1))
    context = _build(_registry(_record()), assets=("BTC", *many))
    assert context.is_empty


def test_missing_owner_suppresses_an_owner_sentence():
    record = _record(owner="")
    assert _build(_registry(record)).is_empty


def test_output_is_deterministic():
    registry = _registry(_record())
    results = {_build(registry).sentence for _ in range(50)}
    assert len(results) == 1


@pytest.mark.parametrize("template", sorted(SENTENCE_TEMPLATES.values()))
def test_no_template_contains_a_banned_term(template):
    rendered = template.format(owner="예시", asset="BTC")
    for term in BANNED_TERMS:
        assert term not in rendered


def test_the_runtime_banned_list_covers_this_test_list():
    """The check that matters runs in the app, not only here (DR-13245).

    Slot values come from a model now, so the list has to exist where the
    sentence is built. This asserts the two never drift apart.
    """
    assert set(BANNED_TERMS) <= set(BANNED_SENTENCE_TERMS)


def test_templates_are_the_only_source_of_reader_text():
    import inspect

    from app import publisher_context

    body = inspect.getsource(publisher_context.build_publisher_context)
    # The sentence is always a template lookup, never assembled inline.
    assert "SENTENCE_TEMPLATES[" in body
    assert body.count("sentence=") == 1


def test_subject_assets_come_from_evidence_only():
    claims = [
        {
            "evidence": [
                {"quote": "BTC and ETH rose while the SEC filing landed"},
                {"quote": "no symbols here"},
            ]
        },
        {"evidence": [{"quote": "SOL fell"}]},
        "not a claim",
    ]
    assert subject_assets_from_ledger(claims) == frozenset({"BTC", "ETH", "SEC", "SOL"})
    assert subject_assets_from_ledger([]) == frozenset()


# --- researched axis (I-13212-d, DR-13245) --------------------------------


def _researched(**overrides):
    values = {
        "relation": "exchange_listed",
        "owner": "예시거래소",
        "asset": "BTC",
        "confidence": "likely",
        "evidence_count": 2,
    }
    values.update(overrides)
    return build_researched_context(**values)


def test_researched_sentence_is_hedged():
    """The registry axis asserts; this axis may only suggest (DR-13245)."""
    sentence = _researched().sentence
    assert "예시거래소" in sentence and "BTC" in sentence
    assert "공개된 자료 기준" in sentence
    assert any(hedge in sentence for hedge in ("보입니다", "가능성", "이력이 있습니다"))


@pytest.mark.parametrize(
    ("relation", "fragment"),
    [
        ("exchange_listed", "상장된 것으로 보입니다"),
        ("project_own", "운영할 가능성이 있습니다"),
        ("investor_portfolio", "투자한 이력이 있습니다"),
    ],
)
def test_each_relation_has_its_own_hedged_literal(relation, fragment):
    assert _researched(relation=relation).sentence.endswith(f"{fragment}.")


def test_researched_requires_likely():
    assert _researched(confidence="uncertain").is_empty


def test_researched_requires_evidence():
    assert _researched(evidence_count=0).is_empty


def test_researched_rejects_unknown_relation():
    assert _researched(relation="none").is_empty
    assert _researched(relation="partnership").is_empty


def test_researched_rejects_a_banned_owner_slot():
    assert _researched(owner="신뢰도 낮음 매체").is_empty


def test_researched_rejects_markup_in_the_owner_slot():
    assert _researched(owner="<b>예시</b>").is_empty
    assert _researched(owner="예시 https://x.example").is_empty


def test_researched_rejects_an_overlong_owner_slot():
    assert _researched(owner="가" * 41).is_empty


def test_researched_rejects_a_non_symbol_asset():
    assert _researched(asset="bitcoin, the asset").is_empty
    assert _researched(asset="").is_empty


def test_researched_basis_is_the_template_key():
    context = _researched()
    assert context.basis == ("researched_exchange_listed",)
    # Not a registry type: nothing downstream may read it as one.
    assert context.publisher_type == ""


def test_researched_templates_are_the_only_source_of_reader_text():
    import inspect

    from app import publisher_context

    body = inspect.getsource(publisher_context.build_researched_context)
    assert "SENTENCE_TEMPLATES[" in body
    assert body.count("sentence=") == 1
