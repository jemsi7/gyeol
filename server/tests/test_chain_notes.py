"""Correction and progress notes at the point of entry (I-13210-b, I-13211-a).

Both notes exist because the archive preserves first reports that later turned
out wrong. Both are one line beside the article: no modal, no badge, no push,
and no edit to the scene that was saved.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.chain_notes import (
    attach_chain_progress,
    attach_revision_notes,
    chain_progress_note,
)
from app.claim_diff import build_revisions


SAVED = datetime(2026, 8, 5, tzinfo=UTC)
START = datetime(2026, 8, 1, tzinfo=UTC)
IOS_MODELS = (
    Path(__file__).resolve().parents[2]
    / "ios/ArticleFeedKit/Sources/ArticleFeedKit/Models.swift"
)


def _article(article_id, days, atom, *, document_type=""):
    return {
        "article_id": article_id,
        "published_at": START + timedelta(days=days),
        "document_type": document_type,
        "tagged_atoms": [
            {
                "atom": atom,
                "tag": "price",
                "subject_kind": "asset",
                "subject_id": "BTC",
            }
        ],
    }


CHAIN = [
    _article("a1", 0, "$30 million"),
    _article("a2", 2, "$45 million"),
    _article("a3", 9, "$61 million", document_type="post_mortem"),
]


def _items(*article_ids):
    return [{"article_id": article_id} for article_id in article_ids]


# --- revision notes ----------------------------------------------------------

def test_each_article_carries_its_own_forward_revision():
    items = _items("a1", "a2", "a3")
    revisions = build_revisions(CHAIN)

    assert attach_revision_notes(items, revisions) == 2

    assert items[0]["revision_note"].endswith("다시 보도되었습니다.")
    assert items[0]["revision_target_article_id"] == "a2"
    assert "사후 보고서로" in items[1]["revision_note"]
    # The newest report has nothing after it.
    assert items[2]["revision_note"] == ""
    assert items[2]["revision_target_article_id"] == ""


def test_every_item_gets_the_fields_even_when_empty():
    items = _items("a3")
    attach_revision_notes(items, build_revisions(CHAIN))
    # A client reading the field unconditionally must not see it missing.
    assert "revision_note" in items[0]
    assert "revision_target_article_id" in items[0]


def test_an_article_outside_the_chain_gets_nothing():
    items = _items("unrelated")
    assert attach_revision_notes(items, build_revisions(CHAIN)) == 0
    assert items[0]["revision_note"] == ""


def test_malformed_items_are_left_alone():
    items = [None, "nope", {"article_id": ""}]
    assert attach_revision_notes(items, build_revisions(CHAIN)) == 0


def test_no_revisions_means_no_notes():
    items = _items("a1")
    assert attach_revision_notes(items, ()) == 0


# --- chain progress ----------------------------------------------------------

def test_progress_counts_only_what_arrived_after_saving():
    note = chain_progress_note(
        saved_at=SAVED,
        joined_at=[SAVED - timedelta(days=2), SAVED + timedelta(days=1)],
    )
    assert note == "보관 이후 관련 보도 1건이 추가되었습니다."


def test_an_unmoved_incident_says_nothing():
    # Reopening a saved article that has not moved should feel unchanged.
    assert chain_progress_note(saved_at=SAVED, joined_at=[SAVED - timedelta(days=1)]) == ""
    assert chain_progress_note(saved_at=SAVED, joined_at=[]) == ""


def test_an_unsaved_article_says_nothing():
    assert chain_progress_note(saved_at=None, joined_at=[SAVED + timedelta(days=1)]) == ""


def test_a_correction_outranks_a_count():
    note = chain_progress_note(
        saved_at=SAVED,
        joined_at=[SAVED + timedelta(days=1)],
        revision_note="이 기사의 수치는 이후 사후 보고서로 갱신되었습니다.",
    )
    # The correction is the more useful thing to say.
    assert note.startswith("이 기사의 수치는")


def test_attaching_progress_uses_the_latest_save():
    items = _items("a1")
    items[0]["revision_note"] = ""
    attached = attach_chain_progress(
        items,
        saved_at={"a1": SAVED},
        chain_joins={"a1": [SAVED + timedelta(hours=1), SAVED + timedelta(days=2)]},
    )
    assert attached == 1
    assert items[0]["chain_progress_note"] == "보관 이후 관련 보도 2건이 추가되었습니다."


def test_resaving_restarts_the_baseline():
    # A reader who saved it again has already seen what was there.
    items = _items("a1")
    later_save = SAVED + timedelta(days=3)
    attach_chain_progress(
        items,
        saved_at={"a1": later_save},
        chain_joins={"a1": [SAVED + timedelta(days=1)]},
    )
    assert items[0]["chain_progress_note"] == ""


def test_progress_field_is_always_present():
    items = _items("a1")
    attach_chain_progress(items, saved_at={}, chain_joins={})
    assert items[0]["chain_progress_note"] == ""


def test_malformed_items_are_skipped_for_progress():
    items = [None, {"article_id": ""}]
    assert attach_chain_progress(items, saved_at={}, chain_joins={}) == 0


def test_the_two_notes_compose_on_one_item():
    items = _items("a2")
    attach_revision_notes(items, build_revisions(CHAIN))
    attach_chain_progress(
        items,
        saved_at={"a2": SAVED},
        chain_joins={"a2": [SAVED + timedelta(days=1)]},
    )
    # One line at the point of entry, and it is the correction.
    assert items[0]["chain_progress_note"] == items[0]["revision_note"]


# --- the app contract --------------------------------------------------------

def test_ios_decodes_all_three_fields_with_empty_defaults():
    swift = IOS_MODELS.read_text(encoding="utf-8")
    for key in (
        'case revisionNote = "revision_note"',
        'case revisionTargetArticleID = "revision_target_article_id"',
        'case chainProgressNote = "chain_progress_note"',
    ):
        assert key in swift
    assert swift.count('forKey: .revisionNote) ?? ""') == 1
    assert 'forKey: .chainProgressNote\n        ) ?? ""' in swift


@pytest.mark.parametrize(
    "banned", ["틀린", "오보", "잘못", "허위", "신뢰", "실시간", "긴급"]
)
def test_no_note_template_judges_or_alarms(banned):
    from app.chain_notes import CHAIN_PROGRESS_SENTENCE
    from app.claim_diff import REVISION_SENTENCES

    for template in (CHAIN_PROGRESS_SENTENCE, *REVISION_SENTENCES.values()):
        assert banned not in template
