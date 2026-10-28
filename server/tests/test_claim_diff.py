"""Comparing a figure across an unfolding incident (I-13210-a, PP-4).

The hard constraint is what this must not do: decide who was right. Publishing
later is not evidence, so only a document whose purpose is to establish facts
turns a change into a correction, and the original scene is never touched.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.claim_diff import (
    AUTHORITATIVE_DOCUMENT_TYPES,
    MIN_REVISION_CHANGE,
    build_revisions,
    instrumental_particle,
    revision_for_article,
)


START = datetime(2026, 8, 1, tzinfo=UTC)
BANNED_TERMS = (
    "틀린", "잘못", "오보", "허위", "부정확", "신뢰", "정확한", "옳",
    "편향", "팩트체크", "실시간",
)


def _article(article_id, days, atom, *, document_type="", tag="price", subject="BTC"):
    return {
        "article_id": article_id,
        "published_at": START + timedelta(days=days),
        "document_type": document_type,
        "tagged_atoms": [
            {
                "atom": atom,
                "tag": tag,
                "subject_kind": "asset",
                "subject_id": subject,
            }
        ],
    }


# --- change versus correction ------------------------------------------------

def test_a_later_news_report_is_a_change_not_a_correction():
    revisions = build_revisions(
        [_article("a1", 0, "$30 million"), _article("a2", 2, "$45 million")]
    )
    assert len(revisions) == 1
    # "Published later" is not evidence of being right.
    assert revisions[0].kind == "change"
    assert revisions[0].sentence() == "이 기사 이후 같은 항목이 다시 보도되었습니다."


@pytest.mark.parametrize("document_type", sorted(AUTHORITATIVE_DOCUMENT_TYPES))
def test_a_fact_establishing_document_makes_it_a_correction(document_type):
    revisions = build_revisions(
        [
            _article("a1", 0, "$30 million"),
            _article("a2", 5, "$61 million", document_type=document_type),
        ]
    )
    assert revisions[0].kind == "correction"
    assert "갱신되었습니다" in revisions[0].sentence()


def test_a_foundation_notice_is_not_authoritative():
    revisions = build_revisions(
        [
            _article("a1", 0, "$30 million"),
            _article("a2", 5, "$61 million", document_type="foundation_notice"),
        ]
    )
    # An interested party's own announcement does not establish what happened.
    assert revisions[0].kind == "change"


def test_no_output_ever_judges_a_report():
    revisions = build_revisions(
        [
            _article("a1", 0, "$30 million"),
            _article("a2", 5, "$61 million", document_type="post_mortem"),
        ]
    )
    sentence = revisions[0].sentence()
    for term in BANNED_TERMS:
        assert term not in sentence


# --- what counts as a revision ------------------------------------------------

def test_a_small_difference_is_not_a_revision():
    below = 30 * (1 + MIN_REVISION_CHANGE / 2)
    revisions = build_revisions(
        [_article("a1", 0, "$30 million"), _article("a2", 2, f"${below} million")]
    )
    # The same claim rounded differently is not news.
    assert revisions == ()


def test_the_threshold_boundary_is_a_revision():
    at = 30 * (1 + MIN_REVISION_CHANGE)
    revisions = build_revisions(
        [_article("a1", 0, "$30 million"), _article("a2", 2, f"${at} million")]
    )
    assert len(revisions) == 1


def test_figures_are_compared_as_quantities_not_digits():
    revisions = build_revisions(
        [_article("a1", 0, "$30 million"), _article("a2", 2, "600억 원")]
    )
    assert len(revisions) == 1
    assert revisions[0].relative_change > 0


def test_different_subjects_are_never_compared():
    revisions = build_revisions(
        [
            _article("a1", 0, "$30 million", subject="BTC"),
            _article("a2", 2, "$90 million", subject="ETH"),
        ]
    )
    assert revisions == ()


def test_different_metrics_are_never_compared():
    revisions = build_revisions(
        [
            _article("a1", 0, "$30 million", tag="price"),
            _article("a2", 2, "$90 million", tag="marketcap"),
        ]
    )
    assert revisions == ()


def test_a_single_report_has_nothing_to_compare():
    assert build_revisions([_article("a1", 0, "$30 million")]) == ()
    assert build_revisions([]) == ()


def test_unparseable_or_zero_figures_are_skipped():
    assert build_revisions(
        [_article("a1", 0, "unknown"), _article("a2", 2, "$45 million")]
    ) == ()
    assert build_revisions(
        [_article("a1", 0, "0"), _article("a2", 2, "$45 million")]
    ) == ()


def test_malformed_entries_are_skipped():
    assert build_revisions(["nope", None, {"tagged_atoms": ["x"]}]) == ()


# --- ordering ----------------------------------------------------------------

def test_revisions_follow_publication_order():
    chain = [
        _article("a3", 9, "$61 million"),
        _article("a1", 0, "$30 million"),
        _article("a2", 2, "$45 million"),
    ]
    revisions = build_revisions(chain)
    assert [(r.earlier_article_id, r.later_article_id) for r in revisions] == [
        ("a1", "a2"),
        ("a2", "a3"),
    ]


def test_the_same_instant_still_orders_stably():
    chain = [_article("b", 0, "$90 million"), _article("a", 0, "$30 million")]
    first = build_revisions(chain)
    for _ in range(20):
        assert build_revisions(chain) == first
    assert first[0].earlier_article_id == "a"


# --- which note an article carries -------------------------------------------

def test_an_article_carries_its_own_forward_revision():
    revisions = build_revisions(
        [
            _article("a1", 0, "$30 million"),
            _article("a2", 2, "$45 million"),
            _article("a3", 9, "$61 million", document_type="post_mortem"),
        ]
    )
    assert revision_for_article("a1", revisions).later_article_id == "a2"
    assert revision_for_article("a2", revisions).kind == "correction"
    # The newest report has nothing after it yet.
    assert revision_for_article("a3", revisions) is None


def test_a_correction_outranks_a_plain_change():
    revisions = build_revisions(
        [
            _article("a1", 0, "$30 million", subject="BTC"),
            _article("a2", 1, "$90 million", subject="BTC"),
            _article("a1b", 0, "$30 million", subject="ETH", tag="price"),
        ]
    )
    # Craft both kinds for the same earlier article.
    chain = [
        _article("a1", 0, "$30 million", subject="BTC"),
        _article("a2", 1, "$45 million", subject="BTC"),
    ]
    chain[0]["tagged_atoms"].append(
        {"atom": "$10 million", "tag": "price", "subject_kind": "asset", "subject_id": "ETH"}
    )
    chain.append(_article("a3", 2, "$40 million", subject="ETH", document_type="audit_report"))
    revisions = build_revisions(chain)
    assert revision_for_article("a1", revisions).kind == "correction"


def test_no_revision_means_no_note():
    assert revision_for_article("a1", ()) is None


# --- Korean grammar ----------------------------------------------------------

@pytest.mark.parametrize(
    ("word", "expected"),
    [
        ("사후 보고서", "로"),
        ("감사 보고서", "로"),
        ("규제 파일링", "으로"),
        ("공식 문서", "로"),
        ("서울", "로"),
        ("", "으로"),
        ("report", "으로"),
    ],
)
def test_the_instrumental_particle_follows_the_final_consonant(word, expected):
    # A wrong particle is immediately visible to a reader, and the document
    # names this sentence interpolates fall on both sides of the rule.
    assert instrumental_particle(word) == expected


def test_every_document_name_renders_grammatically():
    from app.claim_diff import DOCUMENT_NAMES

    for document_type in DOCUMENT_NAMES:
        revisions = build_revisions(
            [
                _article("a1", 0, "$30 million"),
                _article("a2", 5, "$61 million", document_type=document_type),
            ]
        )
        sentence = revisions[0].sentence()
        assert "서으로" not in sentence
        assert "링로" not in sentence
