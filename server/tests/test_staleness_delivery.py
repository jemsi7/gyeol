"""Getting the note onto the right scene, and onto the app (I-13205-b).

The annotation is computed per request because it states what a number is now,
so the work is a bulk lookup at the response boundary rather than a lookup per
article. What it must never do is change the article: the scene prose is left
exactly as written and the note travels beside it.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.market_data import MarketValue
from app.staleness import attach_staleness_notes, collect_staleness_subjects


NOW = datetime(2026, 8, 12, tzinfo=UTC)
OLD = (NOW - timedelta(days=3)).isoformat()
ETH_TVL = ("tvl", "chain", "ethereum")

IOS_MODELS = (
    Path(__file__).resolve().parents[2]
    / "ios/ArticleFeedKit/Sources/ArticleFeedKit/Models.swift"
)
IOS_READER = (
    Path(__file__).resolve().parents[2]
    / "ios/TextShortsApp/TextShortsApp/Screens/ReaderScreen.swift"
)


def _atom(claim_id="CL-001", subject_id="ethereum", atom="480억 달러") -> dict:
    return {
        "claim_id": claim_id,
        "atom": atom,
        "tag": "tvl",
        "subject_kind": "chain",
        "subject_id": subject_id,
        "as_of": OLD,
    }


def _item(atoms=None, scenes=None) -> dict:
    return {
        "map": {
            "tagged_atoms": atoms if atoms is not None else [_atom()],
            "scenes": scenes
            if scenes is not None
            else [
                {"id": "S-01", "body": "본문 하나", "claim_refs": ["CL-002"]},
                {"id": "S-02", "body": "본문 둘", "claim_refs": ["CL-001"]},
            ],
        }
    }


def _value(value=3.91e10) -> MarketValue:
    return MarketValue(
        value=value,
        unit="USD",
        observed_at=NOW,
        provider="defillama",
        attribution="DefiLlama",
    )


# --- collecting subjects -----------------------------------------------------

def test_subjects_are_collected_across_the_whole_response():
    items = [_item(), _item([_atom(subject_id="solana")])]
    assert collect_staleness_subjects(items) == [
        ETH_TVL,
        ("tvl", "chain", "solana"),
    ]


def test_repeated_subjects_are_collected_once():
    # One bulk lookup per response, not one per article.
    assert collect_staleness_subjects([_item(), _item()]) == [ETH_TVL]


@pytest.mark.parametrize(
    "items",
    [[], [None], [{}], [{"map": None}], [{"map": {"tagged_atoms": ["nope"]}}]],
)
def test_malformed_items_collect_nothing(items):
    assert collect_staleness_subjects(items) == []


def test_incomplete_subjects_are_skipped():
    atom = _atom()
    atom["subject_id"] = ""
    assert collect_staleness_subjects([_item([atom])]) == []


# --- attaching notes ---------------------------------------------------------

def test_the_note_lands_on_the_scene_that_carries_the_claim():
    items = [_item()]

    assert attach_staleness_notes(items, {ETH_TVL: _value()}, now=NOW) == 1

    scenes = items[0]["map"]["scenes"]
    assert scenes[0]["staleness_note"] == ""
    assert scenes[1]["staleness_note"].startswith("기사 시점 480억 달러 · 지금 391억 달러")


def test_every_scene_gets_the_field_even_when_empty():
    items = [_item()]
    attach_staleness_notes(items, {}, now=NOW)
    # A client that reads the field unconditionally must not see it missing.
    assert all("staleness_note" in scene for scene in items[0]["map"]["scenes"])


def test_scene_prose_is_never_touched():
    items = [_item()]
    before = [scene["body"] for scene in items[0]["map"]["scenes"]]

    attach_staleness_notes(items, {ETH_TVL: _value()}, now=NOW)

    after = [scene["body"] for scene in items[0]["map"]["scenes"]]
    assert before == after


def test_a_claim_with_no_scene_attaches_nothing():
    items = [_item(scenes=[{"id": "S-01", "claim_refs": ["CL-999"]}])]
    assert attach_staleness_notes(items, {ETH_TVL: _value()}, now=NOW) == 0


def test_two_notes_do_not_share_one_scene():
    items = [
        _item(
            atoms=[_atom(), _atom(claim_id="CL-001", atom="100 billion")],
            scenes=[{"id": "S-01", "claim_refs": ["CL-001"]}],
        )
    ]

    attached = attach_staleness_notes(items, {ETH_TVL: _value()}, now=NOW)

    # The first note takes the scene; the second has nowhere left to go.
    assert attached == 1


def test_an_unavailable_value_leaves_every_scene_bare():
    items = [_item()]
    assert attach_staleness_notes(items, {}, now=NOW) == 0
    assert all(scene["staleness_note"] == "" for scene in items[0]["map"]["scenes"])


def test_malformed_payloads_are_left_alone():
    items = [None, {}, {"map": {"scenes": "nope"}}]
    assert attach_staleness_notes(items, {ETH_TVL: _value()}, now=NOW) == 0


# --- the app contract --------------------------------------------------------

def test_ios_decodes_the_note_and_defaults_it_empty():
    swift = IOS_MODELS.read_text(encoding="utf-8")
    assert 'case stalenessNote = "staleness_note"' in swift
    assert "forKey: .stalenessNote\n        ) ?? \"\"" in swift


def test_ios_renders_nothing_when_the_note_is_empty():
    swift = IOS_READER.read_text(encoding="utf-8")
    assert "if !store.scene.stalenessNote.isEmpty {" in swift
    # Reuses the existing caption style rather than introducing a badge, panel
    # or tab: the note is a sentence beside the prose.
    block = swift.split("private var stalenessNote")[1][:600]
    assert ".font(.system(size: 10))" in block
    assert "sheet" not in block
    assert "Button" not in block
